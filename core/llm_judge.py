"""LLM judge — OpenAI-compatible, model-swappable, vendor-neutral.

What this is
------------
A thin semantic layer that reads a scraped candidate (title + body) plus the
org's eligibility policy and returns ONE structured verdict: is this an open
solicitation, what type, deadline, amount, geographic scope, is the org's
country eligible, is it on-theme. It is the "perception" layer of the hybrid
pipeline (regex pre-filter → THIS → ML decision); it never decides Proceed/
Park/Decline — it extracts + adjudicates eligibility and hands typed fields on.

Why OpenAI-compatible (no vendor lock-in)
-----------------------------------------
It talks to ANY OpenAI-compatible chat endpoint via the `openai` SDK's
`base_url` override. Swap providers/models with env vars only — no code change:

    # OpenAI
    LLM_JUDGE_BASE_URL=https://api.openai.com/v1
    LLM_JUDGE_API_KEY=sk-...
    LLM_JUDGE_MODEL=gpt-4o-mini

    # OpenRouter (cheap hosted open-weight; switch model freely)
    LLM_JUDGE_BASE_URL=https://openrouter.ai/api/v1
    LLM_JUDGE_API_KEY=sk-or-...
    LLM_JUDGE_MODEL=qwen/qwen-2.5-7b-instruct

    # Together / Groq / DeepInfra — same shape, different base_url + model

    # Local Ollama (free, for dev) — pull a model first: `ollama pull llama3.1`
    LLM_JUDGE_BASE_URL=http://localhost:11434/v1
    LLM_JUDGE_API_KEY=ollama          # any non-empty string; Ollama ignores it
    LLM_JUDGE_MODEL=llama3.1

If the SDK isn't installed or the env vars aren't set, `is_enabled()` is False
and `judge()` returns None — callers fall back to the deterministic regex gates,
so the app works with or without it (and degrades gracefully on Streamlit Cloud).

Guardrails: temperature 0, hard timeout, bounded input/output tokens, JSON-only
response (tolerant parse), per-run content-hash cache so re-scans don't re-pay.
Returns null fields rather than guessing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any

from core import type_detect as _td

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "gpt-4o-mini"
# Per-call timeout. Hosted models answer in 1-3s; a local CPU model (Ollama)
# can take 30-90s, so make it tunable via LLM_JUDGE_TIMEOUT (seconds).
_DEFAULT_TIMEOUT = 60
_MAX_INPUT_CHARS = 9000     # ~2.25K tokens of body — bounded, but big enough that an
                            # amount-anchored excerpt keeps the funding section (see
                            # _body_excerpt) even on a long RFP whose "Award Structure /
                            # tiers" table sits well past the opening.
_OPENING_CHARS = 3000       # always-included lead (purpose/scope usually lead)
_ANCHOR_WINDOW = 1000       # chars kept around each money/section anchor
# Reasoning models (gpt-oss, deepseek-r1, o-series) spend completion tokens on
# an internal reasoning pass BEFORE emitting `content`; a tight cap gets eaten by
# reasoning and the JSON never finishes (finish_reason="length", empty content).
# 2000 leaves room for reasoning + the ~250-token JSON. Harmless for plain models
# (gpt-4o-mini stops at "stop" ~250 tokens — this is only a ceiling, not a target).
_MAX_OUTPUT_TOKENS = 2000

# Per-process cache: key = sha1(model + text) -> Judgment. Avoids re-paying for
# the same page within/across scans in one worker. Cleared on restart.
_CACHE: dict[str, dict[str, Any]] = {}

# Per-process LLM call cap — bounds extraction time. gpt-oss:120b runs ~7-15s/call,
# so an uncapped scan with hundreds of gap-candidates can run 20+ minutes on the LLM
# alone (and brush the subprocess timeout). After the cap, judge() returns None →
# the caller falls back to regex. Cache hits do NOT count. Tunable via env.
try:
    _MAX_CALLS = int(os.environ.get("LLM_JUDGE_MAX_CALLS", "60") or 60)
except ValueError:
    _MAX_CALLS = 60
_calls = 0

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_enabled() -> bool:
    """True iff an endpoint + the openai SDK are available. Cheap probe.

    Only the BASE URL is required — a local Ollama endpoint ignores the API key, and
    requiring a non-empty key silently disabled the judge whenever the key var was
    unset. The key is defaulted to a placeholder in the client; a provider that truly
    needs one surfaces a visible auth error instead of going dark."""
    if not os.environ.get("LLM_JUDGE_BASE_URL"):
        return False
    try:
        import openai  # noqa: F401, WPS433
    except ImportError:
        return False
    return True


def _org_context(policies: dict[str, Any]) -> str:
    """Compact, human-readable summary of the org's eligibility policy, injected
    into the prompt so the model adjudicates against the SAME single source of
    truth the deterministic gates use (no logic duplicated in the prompt)."""
    countries = (policies or {}).get("countries", {}) or {}
    eligible = ", ".join(c for c in (countries.get("eligible") or []) if c) or "(none set)"
    broad = ", ".join(b for b in (countries.get("broad_terms") or []) if b) or "(none)"
    themes = ((policies or {}).get("themes", {}) or {}).get("required_any") or []
    theme_sample = ", ".join(themes[:18]) + ("…" if len(themes) > 18 else "")
    return (
        f"ORG ELIGIBLE COUNTRIES: {eligible}\n"
        f"ORG BROAD REGIONS/TIERS: {broad}\n"
        f"ORG THEME FOCUS (any of): {theme_sample}\n"
    )


def _body_excerpt(candidate: dict[str, Any]) -> str:
    """Body text sent to the judge. A flat [:_MAX_INPUT_CHARS] slice dropped the
    funding/eligibility section on long RFPs — the "Award Structure and Funding
    Level" tier table (up to US$300,000 … US$800,000) sits past the opening, so the
    judge never saw the tiers and returned no funding_tiers. This keeps the opening
    PLUS windows anchored on money expressions and eligibility/deadline labels, so
    the figures survive within the same bounded budget."""
    body = str(candidate.get("brief_description") or candidate.get("_page_text") or "").strip()
    if len(body) <= _MAX_INPUT_CHARS:
        return body
    try:
        from core.deep_read import _AMOUNT_RE
    except Exception:
        return body[:_MAX_INPUT_CHARS]
    n = len(body)
    opening_end = min(_OPENING_CHARS, _MAX_INPUT_CHARS)
    half = _ANCHOR_WINDOW // 2
    spans: list[list[int]] = []
    for m in _AMOUNT_RE.finditer(body):
        c = (m.start() + m.end()) // 2
        if c < opening_end:
            continue
        spans.append([max(opening_end, c - half), min(n, c + half)])
    if not spans:
        return body[:_MAX_INPUT_CHARS]
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    budget = max(0, _MAX_INPUT_CHARS - opening_end)
    kept: list[tuple[int, int]] = []
    for s, e in merged:
        if budget <= 0:
            break
        take = min(e - s, budget)
        kept.append((s, s + take))
        budget -= take
    return "\n[...]\n".join([body[:opening_end]] + [body[s:e] for s, e in kept])


def _build_messages(candidate: dict[str, Any], policies: dict[str, Any]) -> list[dict]:
    title = (candidate.get("opportunity_title") or "")[:300]
    url = candidate.get("opportunity_link") or ""
    body = _body_excerpt(candidate)
    system = (
        "You are a precise grants/RFP screening analyst. You read a web page and "
        "the funding org's eligibility policy, then return ONE JSON object — no "
        "prose, no markdown fences. When unsure, use null / false rather than "
        "guessing. Judge GEOGRAPHY by whether the org's country qualifies: a call "
        "open to a region or development tier that CONTAINS the org's country is "
        "eligible; a call scoped to a different region/country is not."
    )
    user = (
        f"{_org_context(policies)}\n"
        f"PAGE TITLE: {title}\n"
        f"PAGE URL: {url}\n"
        f"PAGE TEXT (truncated):\n<<<\n{body}\n>>>\n\n"
        "Return a JSON object with EXACTLY these keys:\n"
        '  "is_open_call": true if this is an OPEN funding solicitation an org can '
        "apply to (RFP/CFP/EOI/NOFO/grant/tender/procurement notice). false for "
        "news, grantee profiles, awarded-grant lists, guidance, donor-investment "
        "pages, closed/past calls.\n"
        '  "solicitation_type": one of NOFO, RFP, RFA, CFP, CFA, CfCN, EOI, LOI, RFI, '
        "RFQ, Tender, Bid, ITB, Procurement notice, Unsolicited, Challenge, Other — "
        "or null. NORMALISE any wording/acronym to the canonical type using these "
        "synonyms: " + _td.synonym_hint("solicitation") + ".\n"
        '  "instrument_type": one of Grant, Cooperative Agreement, Contract, Loan, '
        "Equity/Investment, Prize/Award, Fellowship, Scholarship, Seed fund, "
        "In-kind/TA, Other — or null. NORMALISE using these synonyms: "
        + _td.synonym_hint("instrument") + ".\n"
        '  "call_submission_deadline": final closing date as "YYYY-MM-DD", or null if '
        "none / rolling / clearly past.\n"
        '  "is_closed": true if the page says the call is closed/concluded/past.\n'
        '  "call_award_value": numeric award amount (no symbols), or null. READ '
        "amounts written in prose too — e.g. 'up to $200,000', 'grants of EUR 1–3 "
        "million', 'maximum award US$2M'. For staged calls use the LARGEST tier.\n"
        '  "currency": ISO code (USD/EUR/GBP…) or null.\n'
        '  "funding_tiers": array of {"stage","amount_min","amount_max","currency"} '
        "when the call funds in STAGES with different ceilings (e.g. 'Proof of "
        "Concept up to $200,000' + 'Transition to Scale up to $2,000,000' -> two "
        "objects). [] for a single-amount call. amounts numeric, no symbols.\n"
        '  "call_geographic_scope": array of the call\'s ELIGIBLE / TARGET geography '
        "ONLY. CRITICAL — distinguish eligibility from mere mentions: do NOT list "
        "countries that appear only as EXAMPLES, STATISTICS, or BACKGROUND (cue words "
        "'such as', 'including', 'e.g.', 'for example', 'classified as', 'ranked', "
        "'countries like') — e.g. a sentence naming '8 countries classified by WHO as "
        "ML3' is background, NOT the eligible list. When the call's scope is a REGION "
        "stated even implicitly (e.g. 'cooperation in SSA', 'across sub-Saharan Africa', "
        "'to strengthen systems in the region'), return that REGION (e.g. \"Sub-Saharan "
        "Africa\") rather than guessing a country list. List individual COUNTRIES only "
        "when the call actually ENUMERATES them as eligible; include both a region label "
        "and its listed eligible countries when both are genuinely given. Prefer the "
        "broad region/tier when the scope is regional. Use the term as written; [] if "
        "the call states no eligible geography.\n"
        '  "country_eligible": true if the org\'s eligible country qualifies under '
        "the call's geography, else false, or null if the page states NO eligibility "
        "geography at all. DECISION RULES, in order: (1) SPECIFIC COUNTRIES WIN OVER A "
        "BROAD REGION — if the call names specific eligible country(ies), the org "
        "qualifies ONLY when one of the ORG ELIGIBLE COUNTRIES is among them; a broad "
        "region label like 'Sub-Saharan Africa' that also appears does NOT make the org "
        "eligible (e.g. a call for the DRC that also says 'Sub-Saharan Africa' is NOT "
        "eligible for a Cameroon org). (2) Only when NO specific eligible countries are "
        "named does a containing region/tier (Sub-Saharan Africa, Africa, LMICs, Global "
        "South, worldwide/international) make the org eligible. (3) IGNORE incidental "
        "geography — timezones, a country named only as an example/statistic/background, "
        "or the funder's own location — when deciding eligibility.\n"
        '  "theme_relevant": true if the call fits the org THEME FOCUS above '
        "(consider the whole text, not just the title).\n"
        '  "matched_areas": array of the org theme terms the call matches; [].\n'
        '  "confidence": "high" | "medium" | "low".\n'
        '  "reason": one short sentence justifying is_open_call / eligibility.\n'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def judge(candidate: dict[str, Any], policies: dict[str, Any],
          model: str | None = None) -> dict[str, Any] | None:
    """Return a structured Judgment dict, or None if disabled / failed (caller
    then falls back to the deterministic gates). Cached per (model, content)."""
    if not is_enabled():
        return None
    title = candidate.get("opportunity_title") or ""
    body = candidate.get("brief_description") or candidate.get("_page_text") or ""
    if not (title or body):
        return None
    chosen = model or os.environ.get("LLM_JUDGE_MODEL", _DEFAULT_MODEL)
    ckey = hashlib.sha1(
        (chosen + "|" + title + "|" + _body_excerpt(candidate)).encode("utf-8")
    ).hexdigest()
    if ckey in _CACHE:
        return _CACHE[ckey]

    global _calls
    if _calls >= _MAX_CALLS:          # per-process budget exhausted → regex fallback
        return None
    _calls += 1

    try:
        from openai import OpenAI  # noqa: WPS433 (lazy, optional dep)
        try:
            timeout = float(os.environ.get("LLM_JUDGE_TIMEOUT", _DEFAULT_TIMEOUT))
        except ValueError:
            timeout = _DEFAULT_TIMEOUT
        client = OpenAI(
            base_url=os.environ["LLM_JUDGE_BASE_URL"],
            # Placeholder key for endpoints that ignore it (local Ollama); a provider
            # that requires a real key surfaces a visible auth error, never a silent skip.
            api_key=os.environ.get("LLM_JUDGE_API_KEY") or "ollama",
            timeout=timeout,
            max_retries=0,          # one shot — we fall back to regex on failure
        )
        from core.llm_client import model_chain, chat_with_fallback
        models = [model] if model else model_chain("LLM_JUDGE_MODEL", _DEFAULT_MODEL)
        raw, used_model, _resp = chat_with_fallback(
            client, models,
            messages=_build_messages(candidate, policies),
            temperature=0,
            max_tokens=_MAX_OUTPUT_TOKENS,
        )
    except Exception as exc:
        log.warning("llm_judge failed (%s): %s: %s",
                    candidate.get("opportunity_link"), type(exc).__name__, exc)
        return None

    parsed = _parse_json_block(raw)
    if not parsed:
        log.info("llm_judge non-JSON for %s: %r",
                 candidate.get("opportunity_link"), raw[:200])
        return None

    out = _normalise(parsed, used_model or chosen)
    _CACHE[ckey] = out
    return out


# ---------------------------------------------------------------------------
# Parsing / normalisation
# ---------------------------------------------------------------------------
def _normalise(p: dict[str, Any], model: str) -> dict[str, Any]:
    dl = p.get("call_submission_deadline")
    if not (isinstance(dl, str) and _ISO_DATE_RE.match(dl)):
        dl = None
    val = p.get("call_award_value")
    try:
        val = float(val) if val is not None and str(val) != "" else None
    except (TypeError, ValueError):
        val = None
    scope = p.get("call_geographic_scope")
    scope = [str(s) for s in scope if s] if isinstance(scope, list) else []
    areas = p.get("matched_areas")
    areas = [str(s) for s in areas if s] if isinstance(areas, list) else []
    tiers = p.get("funding_tiers")
    tiers = [t for t in tiers if isinstance(t, dict)] if isinstance(tiers, list) else []
    return {
        "is_open_call": bool(p.get("is_open_call")),
        "is_closed": bool(p.get("is_closed")),
        "solicitation_type": _s(p.get("solicitation_type")),
        "instrument_type": _s(p.get("instrument_type")),
        "call_submission_deadline": dl,
        "call_award_value": val,
        "currency": _s(p.get("currency")),
        "funding_tiers": tiers,
        "call_geographic_scope": scope,
        "country_eligible": _tri(p.get("country_eligible")),
        "theme_relevant": bool(p.get("theme_relevant")),
        "matched_areas": areas,
        "confidence": (_s(p.get("confidence")) or "low").lower(),
        "reason": _s(p.get("reason")),
        "_llm_model": model,
    }


def _parse_json_block(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _s(v: Any) -> str | None:
    if v is None:
        return None
    out = str(v).strip()
    return out or None


def _tri(v: Any) -> bool | None:
    """Tri-state bool: True/False/None (None = 'page states no geography')."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    if t in ("true", "yes", "eligible"):
        return True
    if t in ("false", "no", "ineligible"):
        return False
    return None
