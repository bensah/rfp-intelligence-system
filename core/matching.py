"""Org × Donor × RFP matching engine → composite Proceed / Park / Decline.

Bid Strength = 100% of the 9 weighted eligibility CRITERIA (owner 2026-06-29). The
former 20% "donor-org extras" (thematic / geographic / route / relationship)
DUPLICATED MUST-2 / MUST-4 / MUST-5-route / PREFER-7, so it was dropped to stop
double-counting; those funder signals still count, inside the criteria where they
belong. The hard MUST gate is preserved — any MUST scored "No" forces Decline
regardless of the composite. (`donor_org_extras` is retained for diagnostics/display
only; it no longer affects the composite.)

Field map (air-tight):
  criteria_score  = alignment_score over rfp's 9 criteria (qualification…bid_effort)
  donor_thematic  = org.priority_areas∪domains  ↔ donor.priority_program_areas   (program-area taxonomy overlap)
  donor_geographic= org.org_operating_countries  ↔ donor.donor_geographic_scope (geo overlap, regions expanded)
  donor_route     = org.legal_type / org_has_local_board ↔ donor route/eligibility flags

Pure + best-effort: unknown/missing inputs score 0.5 (neutral) rather than 0,
so a sparse profile never fabricates a Decline. Returns a breakdown for the
Review visual. Weights + thresholds are module constants (tunable).
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from core import geographies as _geo
from core import program_area_classifier as _pa
from core.scorer import alignment_score, criterion_score

CRITERIA_WEIGHT = 1.00      # Bid Strength = the 9 weighted criteria (100%)
EXTRAS_WEIGHT = 0.00        # donor-org extras dropped (duplicated the criteria)
PROCEED_AT = 70.0          # composite ≥ → Proceed
PARK_AT = 45.0             # composite ≥ → Park, else Decline

CRITERIA = (
    "qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing",
    "funding_quality", "funder_relationship", "competitiveness", "bid_effort",
)
_MUST = ("qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing")

# Real donor_intel columns (migration 020) that, when truthy, require a local
# board / local registration — penalised if the org lacks one.
_LOCAL_BOARD_FLAGS = ("donor_local_board_required", "donor_local_registration_required")


def _as_list(v: Any) -> list[str]:
    """Coerce a donor jsonb/text/list field to a list of strings."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    if s[:1] in "[{":
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except (ValueError, TypeError):
            pass
    return [p.strip() for p in s.split(",") if p.strip()]


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "required")


def _ratings_map(v: Any) -> dict[str, int]:
    """Coerce a program_area_ratings value (dict or JSON text) to {key: int 0-5}."""
    if isinstance(v, str):
        try:
            v = json.loads(v or "{}")
        except (ValueError, TypeError):
            v = {}
    out: dict[str, int] = {}
    if isinstance(v, dict):
        for k, val in v.items():
            try:
                out[str(k)] = max(0, min(5, int(val)))
            except (TypeError, ValueError):
                continue
    return out


def _priority_vector(selection: Any, ratings: Any) -> dict[str, float]:
    """Build a {child key: 0–5} priority vector from a selection (canonical child
    keys and/or whole parent categories) + explicit ratings. Parent/child-aware:

      * an explicit child rating wins;
      * a SELECTED child with no rating counts as 5 (selected ⇒ a real priority,
        so an ungraded ↔ ungraded pick scores as a 5-vs-5 exact match);
      * a SELECTED whole parent category fills every child of that category with
        the AVERAGE of that category's explicitly-rated children — or 5 when none
        of its children are rated.

    So org and donor compare on the same child-key space whether either side
    captured parent categories, specific sub-areas, or a mix."""
    sel = _as_list(selection)
    rmap = _ratings_map(ratings)
    vec: dict[str, float] = {}
    for k, v in rmap.items():                       # explicit child grades
        if k in _pa.PROGRAM_AREA_KEYWORDS:
            vec[k] = float(v)
    for s in sel:                                   # selected child, ungraded → 5
        if s in _pa.PROGRAM_AREA_KEYWORDS:
            vec.setdefault(s, 5.0)
    for s in sel:                                   # whole-category pick
        if s in _pa.CATEGORIES:
            children = _pa.expand([s])
            rated = [vec[c] for c in children if c in vec]
            parent = (sum(rated) / len(rated)) if rated else 5.0
            for c in children:
                vec.setdefault(c, parent)
    return vec


def _cosine(a: dict, b: dict) -> float | None:
    """Cosine similarity (0..1) of two non-negative vectors; None if either empty."""
    if not a or not b:
        return None
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(a.get(k, 0.0) ** 2 for k in keys))
    nb = math.sqrt(sum(b.get(k, 0.0) ** 2 for k in keys))
    return (dot / (na * nb)) if (na and nb) else 0.0


def strategic_fit_score(org_selection: Any, org_ratings: Any,
                        donor_selection: Any, donor_ratings: Any) -> float | None:
    """Cosine (0..1) of the org's vs the donor's 0–5 priority vectors, parent/
    child-aware (see `_priority_vector`). None when either side has no
    program-area signal (caller then falls back to set overlap)."""
    return _cosine(_priority_vector(org_selection, org_ratings),
                   _priority_vector(donor_selection, donor_ratings))


def _thematic_fit(org: dict, donor: dict) -> float:
    # Graded path: correlate the org's vs the donor's priority vectors (handles
    # parent categories, specific sub-areas, ungraded=5, broad=avg-of-children).
    graded = strategic_fit_score(
        org.get("org_priority_areas"), org.get("org_priority_ratings"),
        donor.get("donor_priority_areas"), donor.get("donor_priority_ratings"))
    if graded is not None:
        return graded
    # Fallback: binary overlap on the shared taxonomy (no priority signal one side).
    org_pa = (org.get("org_priority_areas") or []) + (org.get("org_domain_expertise") or [])
    don_pa = _as_list(donor.get("donor_priority_areas"))
    if not org_pa or not don_pa:
        return 0.5
    return 1.0 if (_pa.expand(org_pa) & _pa.expand(don_pa)) else 0.0


def _geographic_fit(org: dict, donor: dict, rfp: dict | None = None) -> float:
    org_geo = org.get("org_operating_countries") or []
    # The funder's geography for THIS opportunity = the donor's profile scope PLUS
    # the RFP's own stated geography (the RFP is the most specific signal, e.g.
    # "Africa, Latin America, the Caribbean").
    scope = (_as_list(donor.get("donor_geographic_scope"))
             + _as_list((rfp or {}).get("call_geographic_scope")))
    if not org_geo or not scope:
        return 0.5
    if set(_geo.expand(list(org_geo))) & set(_geo.expand(scope)):
        return 1.0                       # direct / region-member overlap (SSA→Cameroon)
    # Inclusive tier (LMIC / global / developing) that covers the org's countries.
    try:
        from core.criteria_derive import _is_inclusive_geo
        if _is_inclusive_geo(scope):
            return 1.0
    except Exception:
        pass
    return 0.0


def _route_fit(org: dict, donor: dict, org_settings: dict) -> float:
    """CAN-WE-BE-FUNDED (2026-06-25): can this org RECEIVE money through the
    donor's funding mechanism? org-type × donor instrument/route × recipient
    eligibility — NOT the application process (that's "How to apply") nor
    co-financing timing (that's MUST-5).
      1.0 = directly fundable (NGO/local-org eligible, grant route);
      0.5 = only as a sub-recipient / via a partner, or a local-board barrier we
            can clear with a partner;
      0.0 = channel the org can't access (explicitly NGO-ineligible with no
            sub-route, or sovereign-only loan/dev-finance);
      0.5 = donor unknown.
    """
    if not donor:
        return 0.5
    org_np = str(org.get("org_legal_type") or "nonprofit").lower() in (
        "nonprofit", "non-profit", "ngo", "charity")
    ngo_elig = donor.get("donor_ngo_eligible")
    direct = _truthy(donor.get("donor_direct_local_org_eligible"))
    sub_only = _truthy(donor.get("donor_subrecipient_partner_possible"))
    grant = _truthy(donor.get("donor_grant_route"))
    loan = _truthy(donor.get("donor_loan_dev_finance_route"))
    proc = _truthy(donor.get("donor_procurement_tender_route"))
    has_partner = bool(org.get("partners") or org.get("trusted_partners"))

    # Explicitly NGO-ineligible and we're a nonprofit → only via a direct-local
    # exception or a partner; otherwise not accessible.
    if org_np and ngo_elig is not None and not _truthy(ngo_elig):
        if direct:
            return 1.0
        return 0.5 if (sub_only and has_partner) else 0.0
    # Directly eligible to receive (NGO / local org) → fully accessible.
    if _truthy(ngo_elig) or direct:
        return 1.0
    # Sovereign loan / dev-finance only (no grant or procurement route) → an NGO
    # can't take it directly; partner-able at best.
    if loan and not grant and not proc:
        return 0.5 if (sub_only and has_partner) else 0.0
    # Only a sub-recipient pathway flagged (no direct grant route) → via a partner.
    if sub_only and not grant:
        return 0.5 if has_partner else 0.0
    # A local board/registration the org lacks — clearable with a local partner.
    if any(_truthy(donor.get(f)) for f in _LOCAL_BOARD_FLAGS):
        has_board = str((org_settings or {}).get("org_has_local_board", "")).lower() == "yes"
        if not has_board:
            return 1.0 if has_partner else 0.0
    # Default: a grant-making donor with no explicit barrier → accessible.
    return 1.0


def _norm_name(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _names_overlap(a_ids: set[str], b_ids: set[str]) -> bool:
    """True if any normalised name in a matches one in b — exact, or substring
    either way (min length 4 to avoid spurious short-token hits)."""
    for a in a_ids:
        for b in b_ids:
            if a and b and (a == b or (len(a) >= 4 and len(b) >= 4 and (a in b or b in a))):
                return True
    return False


def _relationship_fit(org: dict, donor: dict, org_settings: dict) -> float:
    """1.0 when the applicant org has an existing tie to the donor — it appears in
    the donor's Funders & Collaborators (or a partner it lists does), or it has
    already been funded by this donor. Neutral 0.5 when there's no signal."""
    funders = {_norm_name(x) for x in _as_list(donor.get("funders_collaborators"))}
    funders.discard("")
    donor_names = {_norm_name(donor.get(f)) for f in ("donor", "donor_short")}
    donor_names.discard("")
    os = org_settings or {}
    org_ids = {_norm_name(os.get("org_name")), _norm_name(os.get("org_short"))}
    # partners/funders the org already works with count as "us or our consortium".
    org_ids |= {_norm_name(x) for x in (org.get("trusted_partners") or [])}
    org_ids.discard("")
    if funders and org_ids and _names_overlap(org_ids, funders):
        return 1.0   # our org (or a listed partner) is a funder/collaborator of this donor
    org_hist = {_norm_name(x) for x in (org.get("org_funder_history") or [])}
    if donor_names and org_hist and _names_overlap(org_hist, donor_names):
        return 1.0   # we have previously been funded by this donor
    return 0.5


def donor_org_extras(org: dict | None, donor: dict | None,
                     org_settings: dict | None = None,
                     rfp: dict | None = None) -> dict[str, float]:
    """The donor-org relationship sub-scores (each 0.0 / 0.5 / 1.0)."""
    org = org or {}
    donor = donor or {}
    return {
        "donor_thematic_fit": _thematic_fit(org, donor),
        "donor_geographic_fit": _geographic_fit(org, donor, rfp),
        "donor_route_fit": _route_fit(org, donor, org_settings or {}),
        "donor_relationship_fit": _relationship_fit(org, donor, org_settings or {}),
    }


def composite_match(rfp: dict, org: dict | None = None, donor: dict | None = None,
                    org_settings: dict | None = None) -> dict[str, Any]:
    """Composite org×donor×rfp match → decision + breakdown.

    decision: hard MUST gate first (any MUST='No' → Decline), else composite
    thresholds (≥70 Proceed / ≥45 Park / else Decline)."""
    crit_vals = {k: (rfp or {}).get(k) for k in CRITERIA}
    crit_score = alignment_score(crit_vals)                 # 0–100
    extras = donor_org_extras(org, donor, org_settings, rfp)
    extras_score = (sum(extras.values()) / len(extras)) if extras else 0.5  # 0–1
    composite = CRITERIA_WEIGHT * crit_score + EXTRAS_WEIGHT * extras_score * 100.0

    must_no = [m for m in _MUST if criterion_score(crit_vals.get(m)) == 0]
    if must_no:
        decision = "Decline"
    elif composite >= PROCEED_AT:
        decision = "Proceed"
    elif composite >= PARK_AT:
        decision = "Park"
    else:
        decision = "Decline"

    return {
        "composite": round(composite, 1),
        "criteria_score": round(crit_score, 1),
        "extras_score": round(extras_score * 100.0, 1),
        "extras": extras,
        "must_no": must_no,
        "decision": decision,
        "weights": {"criteria": CRITERIA_WEIGHT, "extras": EXTRAS_WEIGHT},
    }
