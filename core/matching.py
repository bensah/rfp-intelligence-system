"""Org × Donor × RFP matching engine → composite Proceed / Park / Decline.

Combines the 9 eligibility CRITERIA (primary, 80%) with DONOR-ORG relationship
dimensions NOT captured by the criteria (20%): donor thematic fit, donor
geographic fit, donor route eligibility. The hard MUST gate is preserved — any
MUST scored "No" forces Decline regardless of the composite.

Field map (air-tight):
  criteria_score  = alignment_score over rfp's 9 criteria (qualification…bid_effort)
  donor_thematic  = org.priority_areas∪domains  ↔ donor.priority_program_areas   (program-area taxonomy overlap)
  donor_geographic= org.countries_of_operation  ↔ donor.funding_scope_geographic (geo overlap, regions expanded)
  donor_route     = org.legal_type / org_has_local_board ↔ donor route/eligibility flags

Pure + best-effort: unknown/missing inputs score 0.5 (neutral) rather than 0,
so a sparse profile never fabricates a Decline. Returns a breakdown for the
Review visual. Weights + thresholds are module constants (tunable).
"""
from __future__ import annotations

import json
from typing import Any

from core import geographies as _geo
from core import program_area_classifier as _pa
from core.scorer import alignment_score, criterion_score

CRITERIA_WEIGHT = 0.80
EXTRAS_WEIGHT = 0.20
PROCEED_AT = 70.0          # composite ≥ → Proceed
PARK_AT = 45.0             # composite ≥ → Park, else Decline

CRITERIA = (
    "qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing",
    "funding_quality", "funder_relationship", "competitiveness", "bid_effort",
)
_MUST = ("qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing")

# Real donor_intel columns (migration 020) that, when truthy, require a local
# board / local registration — penalised if the org lacks one.
_LOCAL_BOARD_FLAGS = ("local_board_required", "local_registration_required")


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


def _thematic_fit(org: dict, donor: dict) -> float:
    org_pa = (org.get("priority_areas") or []) + (org.get("domains") or [])
    don_pa = _as_list(donor.get("priority_program_areas"))
    if not org_pa or not don_pa:
        return 0.5
    return 1.0 if (_pa.expand(org_pa) & _pa.expand(don_pa)) else 0.0


def _geographic_fit(org: dict, donor: dict) -> float:
    org_geo = org.get("countries_of_operation") or []
    don_geo = _as_list(donor.get("funding_scope_geographic"))
    if not org_geo or not don_geo:
        return 0.5
    ea, eb = set(_geo.expand(list(org_geo))), set(_geo.expand(list(don_geo)))
    return 1.0 if (ea & eb) else 0.0


def _route_fit(org: dict, donor: dict, org_settings: dict) -> float:
    """1.0 by default; 0.0 only when the donor clearly requires a local board /
    registration the org doesn't have. Neutral 0.5 when the donor is unknown."""
    if not donor:
        return 0.5
    needs_board = any(_truthy(donor.get(f)) for f in _LOCAL_BOARD_FLAGS)
    if not needs_board:
        return 1.0
    has_board = str((org_settings or {}).get("org_has_local_board", "")).lower() == "yes"
    return 1.0 if has_board else 0.0


def donor_org_extras(org: dict | None, donor: dict | None,
                     org_settings: dict | None = None) -> dict[str, float]:
    """The donor-org relationship sub-scores (each 0.0 / 0.5 / 1.0)."""
    org = org or {}
    donor = donor or {}
    return {
        "donor_thematic_fit": _thematic_fit(org, donor),
        "donor_geographic_fit": _geographic_fit(org, donor),
        "donor_route_fit": _route_fit(org, donor, org_settings or {}),
    }


def composite_match(rfp: dict, org: dict | None = None, donor: dict | None = None,
                    org_settings: dict | None = None) -> dict[str, Any]:
    """Composite org×donor×rfp match → decision + breakdown.

    decision: hard MUST gate first (any MUST='No' → Decline), else composite
    thresholds (≥70 Proceed / ≥45 Park / else Decline)."""
    crit_vals = {k: (rfp or {}).get(k) for k in CRITERIA}
    crit_score = alignment_score(crit_vals)                 # 0–100
    extras = donor_org_extras(org, donor, org_settings)
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
