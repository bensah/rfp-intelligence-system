"""LLM-assisted extraction layer — vendor-neutral (OpenAI-compatible first).

What this is for
----------------
The regex/PDF/Playwright pipeline in `core/scraper.py` handles the common
shape of donor RFP pages — a labelled deadline, plain-text eligibility, a
clean PDF guide. It cannot read:

  * date-window banners that live inside images
    (e.g. Fondation Pierre Fabre's "FROM OCT 9TH TO NOV 7TH 2025")
  * deadlines inside table layouts in PDFs that pypdf flattens
  * cross-site companion pages (donor links out to a partner site)
  * non-English page bodies that an English regex misses

For those cases this module sends the page text to an LLM and parses a strict
JSON response with the fields we care about: `submission_deadline`,
`eligibility_text`, `brief_description`, `confidence`.

Provider configuration (vendor-neutral)
---------------------------------------
Prefers the SAME OpenAI-compatible endpoint as `core.llm_judge` so ONE config
(e.g. free/cheap Ollama Cloud `gpt-oss:120b`) powers both extraction and
adjudication — no separate Anthropic key required:

    LLM_JUDGE_BASE_URL=https://ollama.com/v1     # shared with the judge
    LLM_JUDGE_API_KEY=...                          # shared with the judge
    LLM_JUDGE_MODEL=gpt-oss:120b                    # shared with the judge

Per-feature overrides are optional (fall back to the LLM_JUDGE_* values):
    LLM_EXTRACT_BASE_URL / LLM_EXTRACT_API_KEY / LLM_EXTRACT_MODEL
    LLM_EXTRACT_TIMEOUT  (default 60s — reasoning models answer in ~15s)
    LLM_EXTRACT_MAX_CALLS (per-process cap; default 200)

Legacy Anthropic fallback: if no OpenAI-compatible endpoint is set but
`ANTHROPIC_API_KEY` is, it uses Claude (`LLM_EXTRACT_MODEL` or a haiku default).

If nothing is configured, `is_enabled()` is False and every public function
returns None — callers fall back to the regex pipeline, so the LLM layer is
fully optional.

Hard guardrails
---------------
  * No retries on auth/rate-limit errors — fail fast, fall back to regex.
  * Hard timeout per call; per-process call cap (no runaway scans).
  * JSON-only response; non-JSON → log + return None.
  * Only called when the regex pipeline left key fields blank, so the LLM is
    purely additive (never overrides a confident regex result).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# Read defaults at call time (not import) so a .env change during a Streamlit
# hot-reload is picked up without a restart.
_DEFAULT_OPENAI_MODEL = "gpt-oss:120b"
_DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"
_DEFAULT_TIMEOUT = 60          # reasoning models (gpt-oss) answer in ~15s
_MAX_INPUT_CHARS = 6000        # ~1.5K tokens — keeps cost bounded
# Reasoning models burn completion tokens on an internal pass BEFORE emitting
# content; a tight cap gets eaten and the JSON never finishes. 2000 leaves room
# for reasoning + the small JSON. Harmless for plain models (they stop early).
_MAX_OUTPUT_TOKENS = 2000

_calls = 0   # per-process counter (reset on restart) — bounded by _max_calls()


def _openai_cfg() -> tuple[str | None, str | None]:
    """(base_url, api_key) for the OpenAI-compatible endpoint, sharing the
    judge's config unless extract-specific overrides are set."""
    base = (os.environ.get("LLM_EXTRACT_BASE_URL")
            or os.environ.get("LLM_JUDGE_BASE_URL"))
    key = (os.environ.get("LLM_EXTRACT_API_KEY")
           or os.environ.get("LLM_JUDGE_API_KEY"))
    return base, key


def _openai_model() -> str:
    return (os.environ.get("LLM_EXTRACT_MODEL")
            or os.environ.get("LLM_JUDGE_MODEL") or _DEFAULT_OPENAI_MODEL)


def _max_calls() -> int:
    try:
        return int(os.environ.get("LLM_EXTRACT_MAX_CALLS", "200") or 200)
    except ValueError:
        return 200


def _timeout() -> float:
    try:
        return float(os.environ.get("LLM_EXTRACT_TIMEOUT")
                     or os.environ.get("LLM_JUDGE_TIMEOUT") or _DEFAULT_TIMEOUT)
    except ValueError:
        return _DEFAULT_TIMEOUT


def is_enabled() -> bool:
    """True iff an OpenAI-compatible endpoint (shared with the judge) OR a legacy
    Anthropic key is configured AND the matching SDK is installed. Cheap probe —
    safe to call on every candidate."""
    base, key = _openai_cfg()
    if base and key:
        try:
            import openai  # noqa: F401, WPS433
            return True
        except ImportError:
            pass
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401, WPS433
            return True
        except ImportError:
            pass
    return False


def _system_prompt() -> str:
    return (
        "You are a careful information extractor. Return ONLY valid JSON "
        "matching the requested schema, nothing else — no prose, no markdown "
        "fences. When unsure, return null rather than guessing."
    )


def _build_prompt(title: str, url: str, page_text: str) -> str:
    """User prompt — kept in one place so it's easy to tune without touching
    SDK boilerplate."""
    snippet = (page_text or "").strip()[:_MAX_INPUT_CHARS]
    return (
        "You are extracting structured facts from a donor RFP / call-for-"
        "proposals page.\n\n"
        f"TITLE: {title}\n"
        f"URL: {url}\n"
        f"PAGE TEXT (truncated to {_MAX_INPUT_CHARS} chars):\n"
        "<<<\n"
        f"{snippet}\n"
        ">>>\n\n"
        "Return ONE JSON object — no prose, no markdown fences — with "
        "these keys:\n"
        '  "submission_deadline": ISO date "YYYY-MM-DD" of the FINAL\n'
        "    application closing date. If the page describes a window\n"
        '    like "FROM Oct 9 TO Nov 7 2025", return the END date\n'
        "    (Nov 7 = 2025-11-07). If no concrete deadline appears,\n"
        "    return null. Do NOT guess from publication / cohort years.\n"
        '  "eligibility_text": one-sentence summary of who can apply\n'
        '    (e.g. "Open to non-profit organizations in sub-Saharan\n'
        '    Africa working on digital health"). null if unclear.\n'
        '  "brief_description": one or two sentences explaining what\n'
        "    the donor is funding. Plain prose, no marketing fluff.\n"
        '    null if the page is too vague.\n'
        '  "confidence": "high" if you found explicit deadline /\n'
        '    eligibility text; "medium" if inferred from context;\n'
        '    "low" if you mostly guessed.\n\n'
        "If the page is clearly NOT an RFP (it's a generic landing\n"
        "page, a search results list, a grantee profile, etc.), return\n"
        "all four fields as null and set confidence to \"low\"."
    )


_DEADLINE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def extract(
    *,
    title: str,
    url: str,
    page_text: str,
    model: str | None = None,
) -> dict[str, Any] | None:
    """Call the configured LLM to extract deadline + eligibility + description.

    Returns dict (submission_deadline, eligibility_text, brief_description,
    confidence, _llm_model) or None if the call failed / LLM is disabled /
    the per-process cap is exhausted. Date is an ISO string ("YYYY-MM-DD").
    """
    if not is_enabled():
        return None
    if not page_text or len(page_text.strip()) < 50:
        return None  # not enough signal to bother

    global _calls
    if _calls >= _max_calls():        # per-process budget exhausted → regex fallback
        return None
    _calls += 1

    base, key = _openai_cfg()
    raw = ""
    used_model = model or "(llm)"
    if base and key:
        used_model = model or _openai_model()
        raw = _call_openai(title, url, page_text, model, base, key)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        used_model = (model or os.environ.get("LLM_EXTRACT_MODEL")
                      or _DEFAULT_ANTHROPIC_MODEL)
        raw = _call_anthropic(title, url, page_text, model)
    if not raw:
        return None

    parsed = _parse_json_block(raw)
    if not parsed:
        log.info("LLM extractor non-JSON for %s: %r", url, raw[:200])
        return None

    dl = parsed.get("submission_deadline")
    if dl and not (isinstance(dl, str) and _DEADLINE_ISO_RE.match(dl)):
        dl = None
    return {
        "submission_deadline": dl,
        "eligibility_text": _str_or_none(parsed.get("eligibility_text")),
        "brief_description": _str_or_none(parsed.get("brief_description")),
        "confidence": _str_or_none(parsed.get("confidence")) or "low",
        "_llm_model": used_model,
    }


def _call_openai(title: str, url: str, page_text: str,
                 model: str | None, base: str, key: str) -> str:
    """OpenAI-compatible chat completion (Ollama / OpenRouter / Groq / …)."""
    try:
        from openai import OpenAI  # noqa: WPS433 (lazy, optional dep)
        client = OpenAI(base_url=base, api_key=key, timeout=_timeout(),
                        max_retries=0)
        resp = client.chat.completions.create(
            model=model or _openai_model(),
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _build_prompt(title, url, page_text)},
            ],
            temperature=0,
            max_tokens=_MAX_OUTPUT_TOKENS,
        )
        return (resp.choices[0].message.content or "") if resp.choices else ""
    except Exception as exc:
        log.warning("LLM extractor (openai) failed for %s: %s: %s",
                    url, type(exc).__name__, exc)
        return ""


def _call_anthropic(title: str, url: str, page_text: str,
                    model: str | None) -> str:
    """Legacy Anthropic path — used only if no OpenAI-compatible endpoint set."""
    try:
        import anthropic  # noqa: WPS433 (lazy, optional dep)
        client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"], timeout=_timeout())
        chosen = (model or os.environ.get("LLM_EXTRACT_MODEL")
                  or _DEFAULT_ANTHROPIC_MODEL)
        resp = client.messages.create(
            model=chosen,
            max_tokens=400,
            system=_system_prompt(),
            messages=[{"role": "user",
                       "content": _build_prompt(title, url, page_text)}],
        )
        out = ""
        for block in resp.content or []:
            if getattr(block, "type", "") == "text":
                out += getattr(block, "text", "")
        return out
    except Exception as exc:
        log.warning("LLM extractor (anthropic) failed for %s: %s: %s",
                    url, type(exc).__name__, exc)
        return ""


def _parse_json_block(raw: str) -> dict[str, Any] | None:
    """Tolerant JSON parser — strips markdown fences, picks the first object."""
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i = s.find("{")
    j = s.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        return json.loads(s[i : j + 1])
    except json.JSONDecodeError:
        return None


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
