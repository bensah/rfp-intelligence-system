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
_MAX_INPUT_CHARS = 9000    # RFP body sent to the model
_MAX_OUTPUT_TOKENS = 2200  # reasoning model needs head-room for reasoning + JSON
_CACHE: dict[str, dict] = {}


def is_enabled() -> bool:
    base = os.environ.get("LLM_SYNTH_BASE_URL") or os.environ.get("LLM_JUDGE_BASE_URL")
    key = os.environ.get("LLM_SYNTH_API_KEY") or os.environ.get("LLM_JUDGE_API_KEY")
    if not (base and key):
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
    body = str(body).strip()[:_MAX_INPUT_CHARS]
    if not (title or body):
        return None

    chosen = (os.environ.get("LLM_SYNTH_MODEL") or os.environ.get("LLM_JUDGE_MODEL")
              or "gpt-oss:120b")
    ckey = hashlib.sha1(
        ("synth|" + chosen + "|" + (auto_recommendation or "") + "|" + title
         + "|" + body[:_MAX_INPUT_CHARS]).encode("utf-8")).hexdigest()
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
        f"{candidate.get('currency') or ''}\n"
        f"- FULL TEXT:\n<<<\n{body}\n>>>\n\n"
        "SYSTEM ASSESSMENT (already computed — EXPLAIN it, do not recompute):\n"
        f"- Auto-decision: {auto_recommendation or '—'}\n"
        f"- Criteria: {crit_line}\n\n"
        "Return a JSON object with EXACTLY these keys:\n"
        '  "brief_description": a rich, DESCRIPTIVE synthesis of THIS specific RFP '
        "(6-9 sentences, 700-1000 characters — use the space) that a reviewer could "
        "read INSTEAD of the source page. At a high level spell out: the PURPOSE and "
        "the problem it addresses; the OBJECTIVES / what work it funds; the SCOPE "
        "(activities, themes, target populations or regions); WHO may apply "
        "(eligibility); the FUNDING amount allocated or range and any award "
        "structure; the duration; and the deadline / how the call runs. Write flowing "
        "prose, NOT a template or bullet list, and VARY the wording and the opening "
        "for each RFP so it never reads like a robotic fill-in-the-blank. Ground every "
        "statement in the text; if a detail (e.g. the amount) is not stated, OMIT it "
        "rather than inventing one. MAX 1000 characters.\n"
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
        "null if the page gives no application instructions.\n"
        '  "compliance_requirements": the co-financing / eligibility / compliance '
        "HARD requirements the RFP explicitly states — cost-share or match %, "
        "mandatory partner/consortium, in-country registration, audited financials, "
        "due-diligence, SAM.gov/UEI, tax-exempt status, etc. One per line as "
        '"• …" with the specifics. "None stated" if the call imposes none. This '
        "protects applicants from a hidden hard-gate discovered near the deadline.\n"
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
            base_url=os.environ.get("LLM_SYNTH_BASE_URL") or os.environ["LLM_JUDGE_BASE_URL"],
            api_key=os.environ.get("LLM_SYNTH_API_KEY") or os.environ["LLM_JUDGE_API_KEY"],
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
        "brief_description": _clip(parsed.get("brief_description"), _BRIEF_MAX),
        "call_domain_areas": pas or None,
        "key_risks": _clip(parsed.get("key_risks"), 300),
        "decision_rationale": _clip(parsed.get("decision_rationale"), 400),
        "how_to_apply": _clip(parsed.get("how_to_apply"), 1500),
        "compliance_requirements": _clip(parsed.get("compliance_requirements"), 1200),
        "call_compliance_flags": (parsed.get("call_compliance_flags")
                             if isinstance(parsed.get("call_compliance_flags"), dict) else {}),
        "_llm_model": chosen,
    }
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
