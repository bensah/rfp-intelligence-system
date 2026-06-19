"""Objective derivation of the 9 eligibility criteria for the AUTO-SCAN.

Each criterion is computed from ORG × RFP (× DONOR) facts — factoring the FULL
criterion definition — and returns the canonical response LABEL
(core.scorer.CRITERION_RESPONSES). None = "Not sure" (can't determine from the
data → treated as missing, never a fabricated 0). Human review can still
override any value; this only runs in auto_score for the auto-scan path.

Definitions encoded (see the bid/no-bid questionnaire):
  qualification        passed the hard gate ⇒ formally eligible
  strategic_fit        org STRATEGY (org.priority_areas + program_area_ratings)
                       correlated with the DONOR's graded priorities → Strongly
                       aligns / Limited priority / Off-strategy (experience excluded)
  capacity             rfp.estimated_value vs org.largest_grant_usd / annual_budget_usd
  geographic_fit       rfp.geographic_scope vs org.countries_of_operation / trusted_partners
  cofinancing          rfp cost-share requirement vs org.cofinancing_capacity
  funding_quality      rfp.estimated_value tiers
  funder_relationship  rfp.funding_agency in org.funder_history
  competitiveness      org TRACK RECORD (org.domains + domain_ratings) on the RFP's
                       exact program area + donor-requirement fit (board/grassroots/…)
  bid_effort           days-to-deadline × org_has_bd_team (core.scorer.bid_effort_label)
"""
from __future__ import annotations

import math
import re
from typing import Any

from core import geographies as _geo
from core import program_area_classifier as _pa
from core.partners import clean_portal_url
from core.scorer import bid_effort_label, days_until

_PA_KEYS = set(_pa.PROGRAM_AREA_KEYWORDS)


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _num(v: Any) -> float | None:
    try:
        f = float(str(v).replace(",", "").replace("$", "")) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _usd(rfp: dict) -> float | None:
    val = _num(rfp.get("estimated_value"))
    if not val:
        return None
    try:
        from core.dropdowns import usd_rate
        return val * float(usd_rate(rfp.get("currency") or "USD"))
    except Exception:
        return val


def _rfp_program_keys(rfp: dict) -> set[str]:
    """Canonical program-area keys for an RFP — empty if generic ('Health')."""
    pa = _as_list(rfp.get("program_area"))
    if not pa or not any(p in _PA_KEYS for p in pa):
        return set()                       # generic / unclassified → can't judge
    return _pa.expand(pa)


# --- per-criterion derivations (return a CRITERION_RESPONSES label or None) ---
def derive_strategic_fit(org: dict, rfp: dict, donor: dict | None = None) -> str | None:
    """STRATEGIC FIT (MUST-2): does this funder fit our *strategy*? Correlate the
    org's graded strategic priority areas with the DONOR's graded priorities:
      ≥60% → Strongly aligns · 20–<60% → Limited priority · <20% → Off-strategy.
    Falls back to org-priorities-vs-RFP-program-area overlap when the donor has no
    graded priorities; None ('Not sure') when there's no signal either way.
    NOTE: experience/track-record now lives in competitiveness, not here."""
    from core.matching import strategic_fit_score      # local import (no cycle)
    score = strategic_fit_score(
        org.get("priority_areas"), org.get("program_area_ratings"),
        (donor or {}).get("priority_program_areas"),
        (donor or {}).get("program_area_ratings"))
    if score is not None:
        if score >= 0.60:
            return "Strongly aligns"
        if score >= 0.20:
            return "Limited priority"
        return "Off-strategy"
    # Fallback: org priorities vs the RFP's own program area (binary overlap).
    rfp_keys = _rfp_program_keys(rfp)
    org_pri = org.get("priority_areas") or []
    if not rfp_keys or not org_pri:
        return None
    return "Strongly aligns" if (_pa.expand(org_pri) & rfp_keys) else "Off-strategy"


def derive_capacity(org: dict, rfp: dict) -> str | None:
    val = _usd(rfp)
    largest = _num(org.get("largest_grant_usd"))
    annual = _num(org.get("annual_budget_usd"))
    if not val or (not largest and not annual):
        return None
    if largest and val <= largest:
        return "Yes, comfortably"
    if annual and val <= annual:
        return "Yes, but a stretch"
    if largest and not annual:
        return "Yes, but a stretch" if val <= 3 * largest else "No, beyond us"
    return "No, beyond us"


def derive_geographic_fit(org: dict, rfp: dict) -> str | None:
    rfp_geo = _as_list(rfp.get("geographic_scope"))
    own = org.get("countries_of_operation") or []
    if not rfp_geo:
        return None                        # geography silent → can't judge
    if own and (set(_geo.expand(list(own))) & set(_geo.expand(rfp_geo))):
        return "Yes, our own presence"
    if org.get("trusted_partners"):
        return "Yes, via a partner"        # have partners who may cover it
    if own:
        return "No presence there"
    return None                            # no org geography on file


_COST_SHARE_RE = re.compile(r"cost[\s-]*shar\w*\s*(?:required)?\s*[:=]\s*([^|]+)", re.I)


def _cost_share_required(rfp: dict) -> bool | None:
    """True/False if the RFP states a cost-share, None if not mentioned."""
    notes = f"{rfp.get('notes') or ''} {rfp.get('brief_description') or ''}"
    m = _COST_SHARE_RE.search(notes)
    if not m:
        return None
    v = m.group(1).strip().lower()
    return False if v[:2] in ("no", "0%", "0 ") or v.startswith(("none", "not")) else True


def derive_cofinancing(org: dict, rfp: dict, donor: dict | None = None) -> str | None:
    # Authoritative signal first: the donor profile's cost-share / prefinance
    # requirement (donor_intel). Fall back to parsing the RFP text.
    required = None
    if donor and any(_truthy(donor.get(f)) for f in
                     ("cost_sharing_match_required", "prefinance_required")):
        required = True
    if required is None:
        required = _cost_share_required(rfp)
    if not required:                       # None (not stated) or False → assume none
        return "Yes / none required"
    cap = str(org.get("cofinancing_capacity") or "limited").lower()
    if cap in ("strong", "moderate"):
        return "Yes / none required"
    if cap == "limited":
        return "Partial, with effort"
    return "No"


def derive_funding_quality(rfp: dict, org: dict | None = None,
                           policies: dict | None = None) -> str | None:
    """ORG-RELATIVE attractiveness of the award SIZE. Bands the RFP value against
    the org's preferred targets (low/mid/max) using GEOMETRIC midpoints
    (money is multiplicative): cut1=sqrt(low*mid), cut2=sqrt(mid*max).
      value <= cut1 -> Low(0) · <= cut2 -> Moderate(1) · > cut2 -> High(2).
    Falls back to absolute tiers when the org hasn't set targets."""
    val = _usd(rfp)
    if not val:
        return None
    org = org or {}
    lo = _num(org.get("funding_target_low"))
    mid = _num(org.get("funding_target_mid"))
    mx = _num(org.get("funding_target_max"))
    if lo and mid and mx and lo <= mid <= mx:
        cut1, cut2 = math.sqrt(lo * mid), math.sqrt(mid * mx)
        if val > cut2:
            return "High"
        if val > cut1:
            return "Moderate"
        return "Low"
    # Fallback: absolute tiers (default hi $2M / mid $500K), tunable via policies.
    hi, mid2 = 2_000_000.0, 500_000.0
    try:
        tiers = ((policies or {}).get("scoring_rules", {})
                 .get("funding_quality_tiers", {}).get("tiers") or [])
        ths = sorted((float(t["threshold_usd"]) for t in tiers if t.get("threshold_usd")),
                     reverse=True)
        if len(ths) >= 2:
            hi, mid2 = ths[0], ths[1]
    except Exception:
        pass
    if val >= hi:
        return "High"
    if val >= mid2:
        return "Moderate"
    return "Low"


def _registered_on_portal(org: dict, rfp: dict, donor: dict | None) -> bool:
    """True when the org has registered on the donor's / the call's portal —
    org.donor_registrations (clean hosts) ∩ {donor.website, rfp link} host."""
    regs = {clean_portal_url(r) for r in (org.get("donor_registrations") or []) if r}
    if not regs:
        return False
    portals = {clean_portal_url(x) for x in
               ((donor or {}).get("website"), rfp.get("opportunity_link")) if x}
    return bool(regs & {p for p in portals if p})


def derive_funder_relationship(org: dict, rfp: dict, donor: dict | None = None) -> str | None:
    """Past grantee (org.funder_history ∋ donor) → strongest; else registered on
    their portal (familiar with the application process) → "Some contact"; else
    None when we hold no relationship data at all."""
    hist = [h.lower() for h in (org.get("funder_history") or []) if h]
    fa = (rfp.get("funding_agency") or "").strip().lower()
    if hist and fa and any(fa in h or h in fa for h in hist):
        return "Current/past grantee"
    if _registered_on_portal(org, rfp, donor):
        return "Some contact"
    if not hist and not (org.get("donor_registrations") or []):
        return None                    # no relationship data on file → Not sure
    return "None"


def derive_bid_effort(rfp: dict, org_settings: dict | None = None) -> str | None:
    bd = str((org_settings or {}).get("org_has_bd_team", "false")).lower() == "true"
    return bid_effort_label(days_until(rfp.get("submission_deadline")), bd)


# Real donor_intel requirement columns (migration 020). Values are text
# (Yes/No/Required/…) → _truthy. Absent/blank flags simply skip that factor.
_GRASSROOT_FLAGS = ("local_registration_required", "local_partner_required")
_BOARD_FLAGS = ("local_board_required",)
_COFIN_FLAGS = ("cost_sharing_match_required", "prefinance_required")
_MULTI_FLAGS = ("global_multi_country_scope",)


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "required", "x")


def _flag(donor: dict, names) -> bool:
    return any(_truthy(donor.get(n)) for n in names)


def derive_competitiveness(org: dict, rfp: dict, donor: dict | None = None,
                           org_settings: dict | None = None) -> str | None:
    """Composite "how well-positioned are we to win?" — org age (older=stronger)
    plus, per the donor's requirements: grassroots/local-org, local board,
    co-financing, multi-country presence, and HQ-country match. None when there's
    no signal at all (reviewer picks). Each factor degrades gracefully if its
    donor flag is absent."""
    from datetime import date
    org = org or {}
    org_settings = org_settings or {}
    score, signals = 0.0, 0

    # Track record on the RFP's EXACT program area — the strongest competitiveness
    # signal. Build the org's 0–5 domain (experience) vector and read the best
    # rating across the call's program keys: strong record = an edge; none = wide open.
    rfp_keys = _rfp_program_keys(rfp)
    if rfp_keys and (org.get("domains") or org.get("domain_ratings")):
        from core.matching import _priority_vector       # local import (no cycle)
        dvec = _priority_vector(org.get("domains"), org.get("domain_ratings"))
        if dvec:
            signals += 1
            strength = max((dvec.get(k, 0.0) for k in rfp_keys), default=0.0)  # 0–5
            score += 1.5 if strength >= 4 else (0.5 if strength >= 2 else -1.0)

    fy = _num(org.get("founding_year"))
    if fy:
        signals += 1
        age = date.today().year - int(fy)
        score += 1.0 if age >= 20 else (0.5 if age >= 10 else 0.0)

    # Portal familiarity — registered on the donor's / call's portal = an edge
    # (knows the application process). Positive-only (no penalty if not).
    if _registered_on_portal(org, rfp, donor):
        signals += 1
        score += 0.5

    if donor:
        if _flag(donor, _GRASSROOT_FLAGS):
            signals += 1
            score += 0.5 if _truthy(org_settings.get("org_is_grassroot")) else -1.0
        if _flag(donor, _BOARD_FLAGS):
            signals += 1
            has_board = str(org_settings.get("org_has_local_board", "")).lower() == "yes"
            score += 0.5 if has_board else -1.0
        if _flag(donor, _COFIN_FLAGS):
            signals += 1
            strong = str(org.get("cofinancing_capacity") or "").lower() == "strong"
            score += 0.5 if strong else -0.5
        if _flag(donor, _MULTI_FLAGS):
            signals += 1
            score += 1.0 if _truthy(org_settings.get("org_is_multi_country")) else -0.5
        dhq = (donor.get("hq_country") or "").strip().lower()
        ohq = str(org_settings.get("org_hq_country")
                  or org_settings.get("org_country") or "").strip().lower()
        if dhq and ohq:
            signals += 1
            if dhq == ohq:
                score += 1.0

    if signals == 0:
        return None
    if score >= 1.0:
        return "Strong (limited field / incumbent / clear edge)"
    if score <= -0.5:
        return "Weak (wide-open)"
    return "Moderate"


# Org legal-type → eligibility bucket; donor eligibility flags per bucket.
_ORG_TYPE_BUCKET = {
    "nonprofit": "ngo", "ngo": "ngo", "charity": "ngo", "not-for-profit": "ngo",
    "government": "govt", "govt": "govt", "public": "govt",
    "higher_ed": "academic", "academic": "academic", "university": "academic",
    "for_profit": "for_profit", "for-profit": "for_profit", "business": "for_profit",
}
_DONOR_TYPE_FLAG = {"ngo": "ngo_eligible", "for_profit": "for_profit_eligible"}

# High-precision RFP-text cues that the award is for an INDIVIDUAL applicant
# (early-career investigator / single PI / fellowship) — an org can't apply.
_INDIVIDUAL_APPLICANT_RE = re.compile(
    r"\b(individual investigators?|single principal investigator|"
    r"co-?principal investigators?\s+are\s+ineligible|"
    r"early[- ]career investigators?|junior faculty|"
    r"post-?doctoral\s+(?:student|fellow|researcher|scholar)s?|"
    r"for individuals(?:\s+not\s+an?\s+organi[sz]ation)?)\b", re.I)


def _partner_match(org: dict, ptype: Any, pcountry: Any) -> bool:
    """True if the org lists a partner matching the required type and/or country."""
    pt = str(ptype or "").strip().lower()
    pc = str(pcountry or "").strip().lower()
    for p in (org.get("partners") or []):
        if not isinstance(p, dict):
            continue
        if pt and pt not in str(p.get("type", "")).strip().lower():
            continue
        if pc and pc != str(p.get("country", "")).strip().lower():
            continue
        return True
    return False


def derive_qualification(org: dict, rfp: dict, donor: dict | None = None,
                         org_settings: dict | None = None) -> str:
    """MUST-1 — a HARD AND of the eligibility conditions the donor/RFP DOCUMENTS.
    Each check activates only when its condition is present; ANY activated check
    the org fails → 'No, not eligible'. An activated check we can't verify (org
    data missing) → 'Mostly, one item unclear'. Nothing documented → 'Yes, fully'
    (the scan hard gate already drops clearly out-of-scope calls)."""
    org = org or {}
    donor = donor or {}
    os = org_settings or {}
    fails: list[str] = []
    unknowns: list[str] = []
    n = 0

    def check(active: bool, ok) -> None:
        nonlocal n
        if not active:
            return
        n += 1
        if ok is False:
            fails.append("x")
        elif ok is None:
            unknowns.append("x")

    # 0. Individual / single-PI / early-career-investigator award — an
    # ORGANISATION can't apply. Caught from the RFP text (no structured field).
    _qtext = " ".join(str(rfp.get(f) or "") for f in
                      ("opportunity_title", "brief_description", "notes"))
    if _INDIVIDUAL_APPLICANT_RE.search(_qtext):
        is_individual_applicant = (
            str(org.get("legal_type") or "").strip().lower() == "individual")
        check(True, is_individual_applicant)   # an org (non-individual) → fail

    # 1. Applicant type — fail only when the org's bucket is clearly excluded.
    bucket = _ORG_TYPE_BUCKET.get(str(org.get("legal_type") or "").strip().lower(), "")
    flag = _DONOR_TYPE_FLAG.get(bucket)
    if bucket and flag:
        if str(donor.get(flag) or "").strip().lower() == "no":
            check(True, False)
        else:
            admits = {b for b, f in _DONOR_TYPE_FLAG.items()
                      if str(donor.get(f) or "").strip().lower() == "yes"}
            if admits and bucket not in admits and str(donor.get(flag) or "").lower() != "yes":
                check(True, None)
    # 2. No-INGO-affiliate
    check(_truthy(donor.get("independent_entity_required")),
          bool(org.get("org_is_independent_entity", True)))
    # 3. HQ country
    hqreq = str(donor.get("hq_country_required") or "").strip().lower()
    if hqreq:
        ohq = str(os.get("org_hq_country") or os.get("org_country") or "").strip().lower()
        check(True, (ohq == hqreq) if ohq else None)
    # 4. Local registration (org registered in the focus country, or a local partner)
    if _truthy(donor.get("local_registration_required")):
        focus = {s.strip() for s in _as_list(rfp.get("geographic_scope"))}
        reg = org.get("countries_registered") or []
        if not focus:
            check(True, None)
        else:
            ok = bool(set(_geo.expand(list(reg))) & set(_geo.expand(list(focus))))
            ok = ok or any(_partner_match(org, "", c) for c in focus)
            check(True, ok)
    # 5. Local board
    check(_truthy(donor.get("local_board_required")),
          str(os.get("org_has_local_board", "")).strip().lower() == "yes")
    # 6. Mandatory / named partner
    if _truthy(donor.get("partnership_mandatory")) or _truthy(donor.get("local_partner_required")):
        rt, rc = donor.get("required_partner_type"), donor.get("required_partner_country")
        if rt or rc:
            check(True, _partner_match(org, rt, rc))
        else:
            check(True, bool(org.get("partners") or org.get("trusted_partners")
                             or org.get("trusted_academic_institutions")
                             or org.get("trusted_for_profit_partners")))
    # 7. Org stage
    stagereq = str(donor.get("org_stage_required") or "").strip().lower()
    if stagereq and stagereq != "any":
        ostage = str(org.get("org_stage") or "").strip().lower()
        check(True, (ostage == stagereq) if ostage else None)
    # 8. Max annual budget (eligibility ceiling)
    mab = _num(donor.get("max_annual_budget_usd"))
    if mab:
        ab = _num(org.get("annual_budget_usd"))
        check(True, (ab <= mab) if ab else None)
    # 9. Min track record (eligibility floor)
    mtr = _num(donor.get("min_track_record_usd"))
    if mtr:
        lg = _num(org.get("largest_grant_usd"))
        check(True, (lg >= mtr) if lg else None)
    # 10. Welcome / pre-registration
    check(_truthy(donor.get("welcome_registration_required")),
          _registered_on_portal(org, rfp, donor))
    # 11. SAM/UEI
    if _truthy(donor.get("sam_uei_registration_required")):
        has = bool(org.get("org_has_sam_uei")) or any(
            "sam" in str(r).lower() for r in (org.get("donor_registrations") or []))
        check(True, has)
    # 12. Tax-exempt
    check(_truthy(donor.get("tax_exempt_status_required")), bool(org.get("org_tax_exempt")))

    if n == 0:
        return "Yes, fully"
    if fails:
        return "No, not eligible"
    if unknowns:
        return "Mostly, one item unclear"
    return "Yes, fully"


def derive_criteria(rfp: dict, org: dict | None = None, donor: dict | None = None,
                    org_settings: dict | None = None,
                    policies: dict | None = None) -> dict[str, str | None]:
    """All 9 derived labels (None where not determinable)."""
    org = org or {}
    return {
        "qualification": derive_qualification(org, rfp, donor, org_settings),
        "strategic_fit": derive_strategic_fit(org, rfp, donor),
        "capacity": derive_capacity(org, rfp),
        "geographic_fit": derive_geographic_fit(org, rfp),
        "cofinancing": derive_cofinancing(org, rfp, donor),
        "funding_quality": derive_funding_quality(rfp, org, policies),
        "funder_relationship": derive_funder_relationship(org, rfp, donor),
        "competitiveness": derive_competitiveness(org, rfp, donor, org_settings),
        "bid_effort": derive_bid_effort(rfp, org_settings),
    }
