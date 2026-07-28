"""LLM review-synthesis — one call that writes the reasoning fields a human
needs to triage a screened opportunity: brief description, focus areas, the top
risk, and a decision rationale.

WHEN it runs: only for candidates that PASS the eligibility gate (i.e. land in
rfp_submissions as Decline / Park / Proceed) — never for rejected candidates
(no point spending tokens on something we dropped). Wired into the screened
insert path + a backfill script.

Vendor-neutral: reuses the same OpenAI-compatible endpoint as core.llm_judge
(Ollama gpt-oss by default). Disabled (returns None) when no endpoint is set, so
the pipeline degrades to the copied brief / blank risk fields.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

_BRIEF_MAX = 1000          # hard cap on the synthesised brief (chars)
# RFP body sent to the model. A flat prefix slice still misses eligibility/funding/
# duration sections that live past this many chars on long RFPs (40+ page PDFs folded
# into _page_text by deep_read.py) — see _build_excerpt(), which anchors on those
# sections instead of just taking the opening. Env-configurable so a higher-spend
# deployment (e.g. Taadom's premium tier) can raise quality without a code fork.
_MAX_INPUT_CHARS = int(os.environ.get("LLM_SYNTH_MAX_INPUT_CHARS", "20000"))
_OPENING_CHARS = int(os.environ.get("LLM_SYNTH_OPENING_CHARS", "6000"))     # always-included lead
_ANCHOR_WINDOW = int(os.environ.get("LLM_SYNTH_ANCHOR_WINDOW", "1200"))    # chars kept per anchor
_MAX_OUTPUT_TOKENS = 2200  # reasoning model needs head-room for reasoning + JSON
_CACHE: dict[str, dict] = {}


def is_enabled() -> bool:
    # Only the BASE URL is required. A local Ollama endpoint IGNORES the API key, so
    # requiring a non-empty key silently disabled synthesis (and the judge) whenever the
    # key env var was unset — the exact "the LLM didn't kick in" symptom. The key is
    # defaulted to a placeholder in the client below; a provider that truly needs one
    # (Ollama Cloud) will surface a visible auth error instead of going dark.
    base = os.environ.get("LLM_SYNTH_BASE_URL") or os.environ.get("LLM_JUDGE_BASE_URL")
    if not base:
        return False
    try:
        import openai  # noqa: F401
        return True
    except ImportError:
        return False


def _sublabels() -> list[str]:
    try:
        from core import program_area_classifier as _pa
        return [s for subs in _pa.TAXONOMY.values() for s in subs]
    except Exception:
        return []


def _money(v: Any) -> str:
    try:
        return f"${float(v):,.0f}" if v not in (None, "", 0, "0") else "—"
    except (TypeError, ValueError):
        return "—"


# Keyword anchors for sections the label regexes below don't cover but that
# still matter for a good brief/checklist on a long RFP.
_EXTRA_ANCHOR_RE = re.compile(
    r"how\s+to\s+apply|application\s+process|selection\s+criteria|"
    r"evaluation\s+criteria|review\s+process|scoring\s+criteria|"
    r"budget\s+(?:narrative|template|guidelines)|funding\s+available|"
    r"award\s+(?:size|amount|ceiling)|total\s+(?:funding|budget)",
    re.IGNORECASE,
)


def _build_excerpt(body: str, candidate: dict[str, Any]) -> str:
    """Text sent to the model: the opening _OPENING_CHARS (purpose/scope usually
    lead the document) PLUS windows anchored on sections that matter most and are
    otherwise likely to fall past a flat truncation on long RFPs — eligibility,
    duration, deadline, funding amount, how-to-apply / selection-criteria. Falls
    back to a plain slice when body already fits, or when no anchors are found
    past the opening (e.g. the backfill script often only has the short
    brief_description, not the full page text, on older rows)."""
    n = len(body)
    if n <= _MAX_INPUT_CHARS:
        return body
    try:
        from core.scraper import (
            _DEADLINE_LABEL_RE, _ELIGIBILITY_LABEL_RE, _DUR_RANGE_RE, _DUR_SINGLE_RE,
        )
        from core.deep_read import _AMOUNT_RE
    except Exception:
        return body[:_MAX_INPUT_CHARS]

    opening_end = min(_OPENING_CHARS, n, _MAX_INPUT_CHARS)
    half = _ANCHOR_WINDOW // 2
    spans: list[tuple[int, int]] = []
    for rx in (_DEADLINE_LABEL_RE, _ELIGIBILITY_LABEL_RE, _DUR_RANGE_RE,
               _DUR_SINGLE_RE, _AMOUNT_RE, _EXTRA_ANCHOR_RE):
        for m in rx.finditer(body):
            center = (m.start() + m.end()) // 2
            if center < opening_end:
                continue   # already covered by the opening slice
            spans.append((max(opening_end, center - half), min(n, center + half)))

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

    parts = [body[:opening_end]] + [body[s:e] for s, e in kept]
    return "\n[...]\n".join(parts)


# Negation phrases the model sometimes writes when a fact isn't in its (truncated)
# excerpt, even though regex extraction found it in the FULL source text — the bug
# class this guards against ("No fixed project duration" for a call that stated
# "12-18 months"). Only stripped when the candidate actually carries a grounded
# value for that field, so a genuinely undated/unstated call is left alone.
_NEG_PATTERNS: dict[str, re.Pattern] = {
    "project_duration": re.compile(
        r"no\s+(?:fixed|set|specified|defined)\s+(?:project\s+)?duration"
        r"|(?:project\s+)?duration\s+(?:is\s+)?(?:not\s+(?:specified|stated|fixed|defined|disclosed)|unspecified)",
        re.IGNORECASE),
    "call_award_value": re.compile(
        r"no\s+(?:fixed|specific)\s+(?:funding|award|grant)\s+amount"
        r"|(?:funding|award|grant)\s+amount\s+(?:is\s+)?(?:not\s+(?:specified|stated|disclosed)|unspecified)",
        re.IGNORECASE),
    "call_submission_deadline": re.compile(
        r"no\s+(?:fixed|specific)\s+deadline"
        r"|deadline\s+(?:is\s+)?(?:not\s+(?:specified|stated)|unspecified)",
        re.IGNORECASE),
}


def _strip_negated_claims(brief: str | None, candidate: dict[str, Any]) -> str | None:
    """Drop any sentence that negates/omits a field we already have a grounded
    (regex, full-text) value for. Second line of defense behind the GROUNDED
    FACTS prompt block — logs so we can see how often the model still tries it."""
    if not brief:
        return brief
    sentences = re.split(r"(?<=[.!?])\s+", brief)
    kept, dropped = [], False
    for sent in sentences:
        hit = any(
            candidate.get(field) not in (None, "", 0, "0") and pat.search(sent)
            for field, pat in _NEG_PATTERNS.items()
        )
        if hit:
            dropped = True
            continue
        kept.append(sent)
    if dropped:
        log.warning("llm_synthesis: stripped negated-but-grounded claim for %s",
                    candidate.get("opportunity_link"))
    return " ".join(kept).strip() or brief


def _grounded_facts_block(candidate: dict[str, Any]) -> str:
    """Facts already regex-extracted from the FULL (untruncated) source text —
    authoritative, and may cover ground the excerpt below does not (its section
    can fall past _MAX_INPUT_CHARS on a long RFP)."""
    facts = []
    dur = candidate.get("project_duration")
    if dur not in (None, "", 0, "0"):
        facts.append(f"- Project duration: {dur} months")
    amt = candidate.get("call_award_value")
    if amt not in (None, "", 0, "0"):
        facts.append(f"- Award value: {_money(amt)} {candidate.get('currency') or ''}".strip())
    dl = candidate.get("call_submission_deadline")
    if dl:
        facts.append(f"- Submission deadline: {dl}")
    if not facts:
        return ""
    return (
        "GROUNDED FACTS (regex-extracted from the FULL source document, which may "
        "be longer than the excerpt below) — trust these over your own reading of "
        "the excerpt. If a fact is listed here you MUST reflect it in the brief and "
        "must NEVER state it is absent, unspecified, or not fixed:\n"
        + "\n".join(facts) + "\n\n"
    )


def _org_block(org: dict) -> str:
    if not org:
        return "(org profile unavailable)"
    pa = org.get("org_priority_areas") or org.get("org_domain_expertise") or []
    funders = org.get("org_funder_history") or []
    return (
        f"- Operates in: {', '.join(org.get('org_operating_countries') or []) or '—'}\n"
        f"- Annual budget managed: {_money(org.get('org_annual_budget'))}/yr; "
        f"largest single grant ever: {_money(org.get('org_largest_grant'))}; "
        f"# grants managed: {org.get('org_grants_count') or '—'}; "
        f"founded: {org.get('org_founding_year') or '—'}\n"
        f"- Preferred award range: {_money(org.get('org_min_target'))}–"
        f"{_money(org.get('org_max_target'))} (sweet spot {_money(org.get('org_mid_target'))})\n"
        f"- Co-financing capacity: {org.get('org_cofinancing_capacity') or '—'}\n"
        f"- Priority areas: {', '.join(str(x) for x in pa[:12]) or '—'}\n"
        f"- Past/known funders: {', '.join(str(x) for x in funders[:12]) or '—'}"
    )


def synthesize(candidate: dict[str, Any], org: dict[str, Any],
               auto_recommendation: str | None,
               criteria: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return {brief_description, program_areas, key_risks, decision_rationale}
    or None if disabled / failed. brief is capped at 1000 chars; program_areas
    are bare sub-labels from the canonical taxonomy."""
    if not is_enabled():
        return None
    title = (candidate.get("opportunity_title") or "").strip()
    body = (candidate.get("_page_text") or candidate.get("raw_text")
            or candidate.get("brief_description") or "")
    body = _build_excerpt(str(body).strip(), candidate)
    if not (title or body):
        return None

    chosen = (os.environ.get("LLM_SYNTH_MODEL") or os.environ.get("LLM_JUDGE_MODEL")
              or "gpt-oss:120b")
    ckey = hashlib.sha1(
        ("synth|" + chosen + "|" + (auto_recommendation or "") + "|" + title
         + "|" + body).encode("utf-8")).hexdigest()
    if ckey in _CACHE:
        return _CACHE[ckey]

    crit = criteria or {}
    crit_line = ", ".join(f"{k}={crit.get(k)}" for k in (
        "qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing",
        "funding_quality", "funder_relationship", "competitiveness", "bid_effort")
        if crit.get(k) is not None) or "(not available)"
    options = _sublabels()
    system = (
        "You are a grants analyst writing concise, factual review notes for a "
        "funding-opportunity screener. Return ONE JSON object, no prose, no "
        "markdown fences. Be specific and grounded in the text + org context; "
        "never invent facts."
    )
    user = (
        "ORG CONTEXT:\n" + _org_block(org) + "\n\n"
        f"OPPORTUNITY:\n- Title: {title}\n"
        f"- Funder: {candidate.get('funding_agency') or '—'}\n"
        f"- Geography: {candidate.get('call_geographic_scope') or '—'}; "
        f"Deadline: {candidate.get('call_submission_deadline') or '—'}; "
        f"Value: {_money(candidate.get('call_award_value'))} "
        f"{candidate.get('currency') or ''}\n\n"
        + _grounded_facts_block(candidate)
        + "- FULL TEXT (excerpt — opening + sections anchored on eligibility/"
        "duration/funding/deadline/how-to-apply; may omit parts of a long RFP):\n"
        f"<<<\n{body}\n>>>\n\n"
        "SYSTEM ASSESSMENT (already computed — EXPLAIN it, do not recompute):\n"
        f"- Auto-decision: {auto_recommendation or '—'}\n"
        f"- Criteria: {crit_line}\n\n"
        "Return a JSON object with EXACTLY these keys:\n"
        '  "brief_description": a rich, DESCRIPTIVE synthesis of THIS specific RFP '
        "(6-9 sentences, 700-1000 characters — use the space) that a reviewer could "
        "read INSTEAD of the source page. At a high level spell out: the PURPOSE and "
        "the problem it addresses; the OBJECTIVES / what work it funds; the SCOPE "
        "(activities, themes, target populations or regions) — when naming the target "
        "GEOGRAPHY, state the call's ELIGIBLE / TARGET region (e.g. 'sub-Saharan "
        "Africa'), even if stated only implicitly, and do NOT present countries that "
        "appear merely as examples/statistics/background ('such as', 'including', "
        "'classified as') as if they were the eligible set; WHO may apply "
        "(eligibility); the FUNDING amount allocated or range and any award "
        "structure; the project DURATION — state the exact length or RANGE wherever the "
        "text gives one (e.g. '12-18 months', 'up to 24 months'; if several tracks list "
        "different lengths, give the range), and if no duration is stated anywhere simply "
        "OMIT it — NEVER assert there is 'no fixed duration'; and the deadline / how the "
        "call runs. Write flowing "
        "prose, NOT a template or bullet list, and VARY the wording and the opening "
        "for each RFP so it never reads like a robotic fill-in-the-blank. Ground every "
        "statement in the text; if a detail (e.g. the amount) is not stated, OMIT it "
        "rather than inventing one. MAX 1000 characters.\n"
        '  "call_award_value_usd": the FUNDING amount per award for THIS call, as a plain '
        "NUMBER in US dollars (no symbols/commas). If the call states a RANGE or several "
        "tracks with different amounts, return the HIGHEST amount; convert to USD if the "
        "call uses another currency; null if the call states no amount. Ground it in the "
        "text — never invent.\n"
        '  "project_duration_months": the project / grant length for THIS call as an '
        "INTEGER number of MONTHS. If a range or multiple tracks, return the HIGHEST; null "
        "if the call states no duration. Never invent.\n"
        '  "call_domain_areas": array of 1-3 best-fit areas chosen ONLY from this '
        f"list (verbatim): {options}\n"
        '  "key_risks": ONE sentence naming the single most material risk of THIS '
        "org pursuing THIS RFP, grounded in the org context — e.g. no prior "
        "relationship with the funder; award size far above the org's track "
        "record; geography/eligibility mismatch; very short runway; co-financing "
        'it cannot meet. If none, "No major risk identified."\n'
        '  "decision_rationale": 1-2 sentences explaining WHY the system reached '
        f'"{auto_recommendation}" for THIS org, citing the deciding criteria.\n'
        '  "how_to_apply": a HIGH-LEVEL step-by-step on how to apply for THIS call '
        "— 3-6 short NUMBERED steps (e.g. register on the portal, prepare a concept "
        "note, submit by the deadline), naming the submission portal / email and any "
        'application URL when stated. Format each step on its own line as "1. …". '
        "AFTER the numbered steps, if the call describes how proposals are EVALUATED / "
        'SELECTED, add one final line beginning "Selection: " summarising the review / '
        "scoring stages, evaluation criteria weights, and who decides (e.g. two-stage "
        "concept-then-full, technical + cost panels, board approval). "
        "null if the page gives no application instructions.\n"
        '  "compliance_requirements": the co-financing / eligibility / compliance '
        "HARD requirements the RFP explicitly states — cost-share or match %, "
        "mandatory partner/consortium, in-country registration, audited financials, "
        "due-diligence, SAM.gov/UEI, tax-exempt status, etc. One per line as "
        '"• …" with the specifics. "None stated" if the call imposes none. This '
        "protects applicants from a hidden hard-gate discovered near the deadline.\n"
        '  "application_checklist": the concrete DELIVERABLES an applicant must submit '
        "for THIS call — e.g. concept note, full proposal, detailed budget, logframe / "
        "results framework, registration certificate, audited financials, CVs, letters "
        "of support / partner MoUs, tax/ legal docs, work plan. One item per line as "
        '"• …", naming page/word limits or templates when stated. "None stated" if the '
        "call lists no required documents. Capture EVERY item the text names.\n"
        '  "eligibility_specifics": concrete, call-SPECIFIC eligibility constraints '
        "BEYOND the generic country/theme fit — e.g. 'Activities must focus on UNESCO "
        "World Heritage Sites', 'Lead applicant must be a registered NGO operating "
        "≥3 years', 'Consortia of 2-4 partners only', 'For-profits ineligible'. One per "
        'line as "• …". "None stated" if the call adds no specific constraints.\n'
        '  "call_compliance_flags": a STRUCTURED object — set a key to true ONLY for each '
        "requirement the RFP EXPLICITLY imposes, choosing from exactly these keys: "
        "cost_sharing_match_required, local_registration_required, "
        "partnership_mandatory, audit_report_required, "
        "audited_financials_required, due_diligence_questionnaire_required, "
        "sam_uei_registration_required, tax_exempt_status_required, "
        "safeguarding_policy_required, authorized_signatory_signoff_required, "
        "partner_mou_required, govt_mou_required, govt_endorsement_letter_required, "
        "local_board_required, funding_platform_registration_required, "
        "state_party_cofinancing_required (true ONLY if the call requires GOVERNMENT / "
        "counterpart / 'state party' co-financing — distinct from an applicant cost-share), "
        "indirect_cost_disallowed (true ONLY if the call states indirect / overhead / "
        "administrative costs are NOT eligible or not reimbursed), "
        "multi_country_encouraged (true ONLY if the call EXPLICITLY encourages "
        "multi-country / multi-geography / regional consortium proposals). "
        "Omit keys that are not required. {} if none. "
        "These FEED the eligibility score, so only flag what the text clearly states.\n"
        '  "must1_requirements": a STRUCTURED object for LEGAL-STATUS / qualification '
        "rules the call EXPLICITLY states — OMIT any key not clearly stated; {} if none. "
        "Allowed keys & values ONLY: "
        "requires_pi ('yes' only if the call requires a named INDIVIDUAL / Principal "
        "Investigator rather than an organisation); "
        "pi_country_scope ('donor_in_scope' if that PI must be based in the implementation "
        "country, 'foreign' if in the donor's or another specified country); "
        "entity_type_required ('grassroot_local' | 'multi_country' | 'individual'); "
        "hq_country_required (the country the applicant must be HEADQUARTERED in, verbatim); "
        "registration_region (where the applicant must be REGISTERED, e.g. 'LMIC', "
        "'Sub-Saharan Africa', or a country); "
        "prior_beneficiary_rule ('eligible' if prior grantees are welcome; "
        "'ineligible_current' if CURRENT grantees are barred; 'ineligible_previous' if "
        "PAST grantees are barred; 'ineligible_any' if both); "
        "experience_required ('significant' if the call seeks orgs with substantial / "
        "long / deep / extensive experience in the domain; 'moderate' if it seeks "
        "demonstrated / relevant prior experience; OMIT if it welcomes early-stage / "
        "startups / any applicant); "
        "org_stage_required ('early-stage' ONLY if the call funds early-stage / startups / "
        "new organisations EXCLUSIVELY; 'established' ONLY if it requires established orgs; "
        "OMIT if open to any stage). Ground every value in the text; never infer.\n"
    )
    try:
        from openai import OpenAI
        client = OpenAI(
            base_url=(os.environ.get("LLM_SYNTH_BASE_URL")
                      or os.environ.get("LLM_JUDGE_BASE_URL")),
            # Placeholder key for endpoints that ignore it (local Ollama); a provider
            # that requires a real key surfaces a visible auth error, never a silent skip.
            api_key=(os.environ.get("LLM_SYNTH_API_KEY")
                     or os.environ.get("LLM_JUDGE_API_KEY") or "ollama"),
            timeout=float(os.environ.get("LLM_JUDGE_TIMEOUT", "60") or 60),
            max_retries=0,
        )
        resp = client.chat.completions.create(
            model=chosen,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            # Mild temperature so the prose VARIES per RFP (not robotic); the
            # strong "ground every statement / never invent" instructions keep the
            # factual fields (compliance_flags etc.) accurate.
            temperature=0.4, max_tokens=_MAX_OUTPUT_TOKENS)
        raw = (resp.choices[0].message.content or "") if resp.choices else ""
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        completion_tokens = getattr(usage, "completion_tokens", None) if usage else None
        if usage:
            log.info("llm_synthesis usage for %s: prompt=%s completion=%s total=%s",
                      candidate.get("opportunity_link"), prompt_tokens, completion_tokens,
                      getattr(usage, "total_tokens", None))
    except Exception as exc:
        log.warning("llm_synthesis failed for %s: %s: %s",
                    candidate.get("opportunity_link"), type(exc).__name__, exc)
        return None

    parsed = _parse_json(raw)
    if not parsed:
        log.info("llm_synthesis non-JSON for %s: %r",
                 candidate.get("opportunity_link"), raw[:160])
        return None

    valid = set(options)
    pas = [p for p in (parsed.get("call_domain_areas") or []) if p in valid][:3]
    out = {
        "brief_description": _clip(
            _strip_negated_claims(parsed.get("brief_description"), candidate), _BRIEF_MAX),
        "call_domain_areas": pas or None,
        "key_risks": _clip(parsed.get("key_risks"), 300),
        "decision_rationale": _clip(parsed.get("decision_rationale"), 400),
        "how_to_apply": _clip(parsed.get("how_to_apply"), 1800),
        "compliance_requirements": _clip(parsed.get("compliance_requirements"), 1200),
        "application_checklist": (_clip(_as_lines(parsed.get("application_checklist")), 1500)
                                  or _regex_checklist(body)),
        "eligibility_specifics": _clip(_as_lines(parsed.get("eligibility_specifics")), 1200),
        "call_compliance_flags": (parsed.get("call_compliance_flags")
                             if isinstance(parsed.get("call_compliance_flags"), dict) else {}),
        "_llm_model": chosen,
        "_prompt_tokens": prompt_tokens,
        "_completion_tokens": completion_tokens,
    }
    # Structured award value + duration the LLM read from the (possibly RANGED) call. These
    # FILL the row only when the regex/scraper extractor left them blank (see the merge in
    # core.scan_pipeline), so PREFER-6 / MUST-3 can size a call that states its award or
    # length only as a range — the LLM returns the HIGHEST of any range per the prompt.
    def _num_or_none(v):
        try:
            n = float(str(v).replace(",", "").replace("$", "").strip())
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None
    _av = _num_or_none(parsed.get("call_award_value_usd"))
    if _av:
        out["call_award_value"] = _av
    _pd = _num_or_none(parsed.get("project_duration_months"))
    if _pd:
        out["project_duration"] = int(round(_pd))
    # Fold the grounded MUST-1 requirement signals INTO compliance_flags so they ride
    # the existing rfp_compliance plumbing (core.criteria_derive._merge_rfp_compliance
    # preserves valued keys; booleans stay boolean).
    _m1 = parsed.get("must1_requirements")
    if isinstance(_m1, dict):
        out["call_compliance_flags"] = {**out["call_compliance_flags"], **_sanitize_must1(_m1)}
    _CACHE[ckey] = out
    return out


# Allowed enum values for the LLM-extracted MUST-1 requirements (anything else is
# dropped — grounded, no fabrication).
_MUST1_ENUMS = {
    "donor_pi_country_scope": {"donor_in_scope", "foreign"},
    "donor_entity_type_required": {"grassroot_local", "multi_country", "individual"},
    "donor_prior_beneficiary_rule": {"eligible", "ineligible_current",
                               "ineligible_previous", "ineligible_any"},
    "experience_required": {"significant", "moderate"},   # MUST-3 capacity
    "org_stage_required": {"early-stage", "established"},  # MUST-3 org stage (only if RESTRICTED)
}


def _sanitize_must1(d: dict) -> dict:
    """Keep only valid, grounded MUST-1 requirement keys/values from the LLM output;
    coerce requires_pi to a boolean flag; bound free-text country/region length."""
    clean: dict[str, Any] = {}
    if str(d.get("donor_requires_pi") or "").strip().lower() in ("yes", "true", "1"):
        clean["donor_requires_pi"] = True
    for key, allowed in _MUST1_ENUMS.items():
        v = str(d.get(key) or "").strip().lower()
        if v in allowed:
            clean[key] = v
    hq = str(d.get("donor_hq_country_required") or "").strip()
    if hq and len(hq) <= 60:
        clean["donor_hq_country_required"] = hq
    rr = d.get("donor_registration_region")
    if isinstance(rr, list):
        rr = ", ".join(str(x).strip() for x in rr if str(x).strip())
    rr = str(rr or "").strip()
    if rr and len(rr) <= 120:
        clean["donor_registration_region"] = rr
    return clean


def _clip(v: Any, n: int) -> str | None:
    if v is None:
        return None
    s = " ".join(str(v).split()).strip()
    if not s:
        return None
    return s[: n - 1].rstrip() + "…" if len(s) > n else s


def _as_lines(v: Any) -> Any:
    """The LLM is asked for one-item-per-line text, but sometimes returns an array.
    Join arrays into "• …" lines so storage + display stay uniform (text columns)."""
    if isinstance(v, (list, tuple)):
        items = [str(x).strip().lstrip("•-–* ").strip() for x in v if str(x).strip()]
        return "\n".join(f"• {x}" for x in items) if items else None
    return v


# Common required-submission documents — a deterministic fallback for the application
# checklist when the LLM is disabled or returns nothing (the user asked for "LLM from
# donor docs + regex"). label -> case-insensitive pattern.
_CHECKLIST_PATTERNS: list[tuple[str, str]] = [
    ("Concept note", r"concept note"),
    ("Full / technical proposal", r"full proposal|technical proposal|project proposal"),
    ("Detailed budget", r"detailed budget|budget (?:template|narrative|breakdown)|line[- ]item budget"),
    ("Logframe / results framework", r"log[- ]?frame|logical framework|results framework"),
    ("Work / implementation plan", r"work\s?plan|implementation plan"),
    ("Theory of change", r"theory of change"),
    ("Registration certificate", r"registration certificate|certificate of (?:registration|incorporation)|legal registration"),
    ("Audited financials", r"audited (?:financial|account)"),
    ("CVs / key personnel", r"\bcvs?\b|curriculum vitae|team bios|key personnel"),
    ("Letters of support / partner MoU", r"letter[s]? of (?:support|commitment|endorsement)|partner(?:ship)? (?:mou|agreement)|\bmou\b"),
    ("Cover letter / application form", r"cover letter|cover sheet|application form"),
    ("Tax / legal documents", r"tax (?:certificate|exempt|clearance)|legal status document"),
]


def _regex_checklist(body: str | None) -> str | None:
    """Deterministic fallback: scan the call text for common required documents."""
    blob = (body or "").lower()
    if not blob:
        return None
    hits = [label for label, pat in _CHECKLIST_PATTERNS if re.search(pat, blob)]
    return "\n".join(f"• {h}" for h in hits) if hits else None


def _parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    s = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", raw.strip()))
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None
