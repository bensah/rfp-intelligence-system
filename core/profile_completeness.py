"""Pure helpers for the Entity profile-completeness bar (app_pages/organization.py).

No Streamlit / DB imports, so the scoring, the red-amber-green band, and the
"screening-ready" gap are unit-testable in isolation (see tests/test_profile_completeness.py).
"""
from __future__ import annotations

from typing import Any


def _filled(v: Any) -> bool:
    """True when a profile field carries a real value. 0 counts as UNSET for the numeric
    money/target fields (an org never has a $0 budget)."""
    if v is None:
        return False
    if isinstance(v, (list, tuple, dict, str)):
        return len(v) > 0
    if isinstance(v, (int, float)):
        return v not in (0,)
    return bool(v)


def completeness(org: dict, prof: dict) -> tuple[float, list[str]]:
    """Weighted profile-completeness in [0,1] + the labels of the still-missing fields.
    Geography + program areas weigh most (they are the load-bearing gate + fit signals);
    identity/capacity fields fill in the picture. (label, value, weight)."""
    org = org or {}
    prof = prof or {}
    checks = [
        ("countries of operation", prof.get("org_operating_countries"), 3),
        ("countries of registration", prof.get("org_registered_countries"), 2),
        ("domains / areas of expertise", prof.get("org_domain_expertise"), 3),
        ("strategic priority areas", prof.get("org_priority_areas"), 2),
        ("legal type", prof.get("org_legal_type"), 1),
        ("founding year", prof.get("org_founding_year"), 1),
        ("primary country", org.get("org_country"), 1),
        ("HQ country", org.get("org_hq_country") or prof.get("org_hq_country"), 1),
        ("annual budget", prof.get("org_annual_budget"), 1),
        ("largest grant managed", prof.get("org_largest_grant"), 1),
        ("co-financing capacity", prof.get("org_cofinancing_capacity"), 1),
        ("organization stage", prof.get("org_stage"), 1),
        ("funding target band", prof.get("org_max_target"), 1),
        ("funders won from", prof.get("org_funder_history"), 1),
        ("donor registrations", prof.get("org_donor_registrations"), 1),
        ("proposal languages", prof.get("proposal_languages"), 1),
    ]
    total = sum(w for _, _, w in checks) or 1
    got = sum(w for _, v, w in checks if _filled(v))
    missing = [label for label, v, w in checks if not _filled(v)]
    return got / total, missing


def rag_band(pct: float) -> str:
    """Red-amber-green band for the completeness bar (product rule 2026-08-01):
    'red' at ≤50%, 'amber' in (50%, 80%), 'green' at ≥80%."""
    if pct >= 0.80:
        return "green"
    if pct > 0.50:
        return "amber"
    return "red"


# Fill colour per band — one source of truth shared by the bar renderer.
RAG_COLOR = {"red": "#d1343b", "amber": "#e08a1e", "green": "#1e8e3e"}


def readiness_gap(prof: dict) -> list[str]:
    """The pieces STILL MISSING before the tenant is 'screening-ready' — ≥1 country of
    operation AND ≥1 program area (domain or priority). Empty list ⇒ ready. Names ONLY
    what's actually missing, so a user with a country of operation already set is asked
    only for the program area (not both)."""
    prof = prof or {}
    gap: list[str] = []
    if not prof.get("org_operating_countries"):
        gap.append("at least one country of operation")
    if not (prof.get("org_domain_expertise") or prof.get("org_priority_areas")):
        gap.append("at least one program area")
    return gap
