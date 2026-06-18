"""Objective derivation of the 9 eligibility criteria for the AUTO-SCAN.

Each criterion is computed from ORG × RFP (× DONOR) facts — factoring the FULL
criterion definition — and returns the canonical response LABEL
(core.scorer.CRITERION_RESPONSES). None = "Not sure" (can't determine from the
data → treated as missing, never a fabricated 0). Human review can still
override any value; this only runs in auto_score for the auto-scan path.

Definitions encoded (see the bid/no-bid questionnaire):
  qualification        passed the hard gate ⇒ formally eligible
  strategic_fit        priorities (org.priority_areas) AND experience (org.domains)
                       vs rfp.program_area
  capacity             rfp.estimated_value vs org.largest_grant_usd / annual_budget_usd
  geographic_fit       rfp.geographic_scope vs org.countries_of_operation / trusted_partners
  cofinancing          rfp cost-share requirement vs org.cofinancing_capacity
  funding_quality      rfp.estimated_value tiers
  funder_relationship  rfp.funding_agency in org.funder_history
  competitiveness      no objective source yet → None (human picks)
  bid_effort           days-to-deadline × org_has_bd_team (core.scorer.bid_effort_label)
"""
from __future__ import annotations

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
def derive_strategic_fit(org: dict, rfp: dict) -> str | None:
    rfp_keys = _rfp_program_keys(rfp)
    org_pri = org.get("priority_areas") or []
    org_dom = org.get("domains") or []
    if not rfp_keys or (not org_pri and not org_dom):
        return None
    pri = bool(_pa.expand(org_pri) & rfp_keys) if org_pri else False
    exp = bool(_pa.expand(org_dom) & rfp_keys) if org_dom else False
    if pri and exp:
        return "Strong - priorities + experience"
    if pri:
        return "Priority area, limited experience"
    if exp:
        return "Experienced but off-strategy"
    return "Neither"


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
    required = _cost_share_required(rfp)
    if not required:                       # None (not stated) or False → assume none
        return "Yes / none required"
    cap = str(org.get("cofinancing_capacity") or "limited").lower()
    if cap in ("strong", "moderate"):
        return "Yes / none required"
    if cap == "limited":
        return "Partial, with effort"
    return "No"


def derive_funding_quality(rfp: dict, policies: dict | None = None) -> str | None:
    val = _usd(rfp)
    if not val:
        return None
    hi, mid = 2_000_000.0, 500_000.0
    try:
        tiers = ((policies or {}).get("scoring_rules", {})
                 .get("funding_quality_tiers", {}).get("tiers") or [])
        ths = sorted((float(t["threshold_usd"]) for t in tiers if t.get("threshold_usd")),
                     reverse=True)
        if len(ths) >= 2:
            hi, mid = ths[0], ths[1]
    except Exception:
        pass
    if val >= hi:
        return "High"
    if val >= mid:
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


# Donor-requirement flag names (best-effort — donor_intel may add/rename these;
# absent flags simply skip that factor). "assumed backside donor intel".
_GRASSROOT_FLAGS = ("requires_local_org", "local_org_required", "grassroots_only",
                    "local_ngo_only", "requires_local_presence")
_BOARD_FLAGS = ("requires_local_board", "local_board_required")
_COFIN_FLAGS = ("requires_cofinancing", "cofinancing_required",
                "cost_share_required", "requires_cost_share")
_MULTI_FLAGS = ("requires_multi_country", "multi_country_required",
                "multi_country_preferred", "regional_presence_required")


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


def derive_criteria(rfp: dict, org: dict | None = None, donor: dict | None = None,
                    org_settings: dict | None = None,
                    policies: dict | None = None) -> dict[str, str | None]:
    """All 9 derived labels (None where not determinable). qualification defaults
    to 'Yes, fully' because the hard gate already rejects clear ineligibility;
    competitiveness has no objective source yet (left to the reviewer)."""
    org = org or {}
    return {
        "qualification": "Yes, fully",
        "strategic_fit": derive_strategic_fit(org, rfp),
        "capacity": derive_capacity(org, rfp),
        "geographic_fit": derive_geographic_fit(org, rfp),
        "cofinancing": derive_cofinancing(org, rfp, donor),
        "funding_quality": derive_funding_quality(rfp, policies),
        "funder_relationship": derive_funder_relationship(org, rfp, donor),
        "competitiveness": derive_competitiveness(org, rfp, donor, org_settings),
        "bid_effort": derive_bid_effort(rfp, org_settings),
    }
