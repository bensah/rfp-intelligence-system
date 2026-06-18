"""Organization profile — the one-time structured record of WHO the deploying
org is, compared against each RFP's requirements to answer the bid/no-bid
questions objectively (ML Phase 3).

Stored as a single JSON blob in `app_settings.value` under key `org_profile`
(same pattern as core.policies). Per-DEPLOYMENT, so it's multi-tenant-ready:
each tenant's Supabase carries its own profile; if tenants are ever pooled into
one DB, key the blob by org_id instead.

Every field is tagged with the bid/no-bid criterion it feeds:

  qualification        legal_type, donor_registrations, countries_registered
  strategic_fit        founding_year, domains, priority_areas
  capacity             annual_budget_usd, largest_grant_usd
  geographic_fit       countries_of_operation, trusted_partners
  cofinancing          cofinancing_capacity
  funder_relationship  funder_history
  bid_effort           proposal_languages

Humans answer the form today; the profile is the reference a responder checks
against (and, once an LLM extractor lands, what the RFP requirements are matched
against automatically). It also makes the model genuinely per-organization.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from core.settings import get_setting, set_setting

ORG_PROFILE_KEY = "org_profile"


# Defaults sketch a typical global-health implementing NGO (the reference
# deployment). An admin fills the real values in Admin > Settings once.
DEFAULT_PROFILE: dict[str, Any] = {
    # --- identity ---
    # (org name / short / country / logo / US-entity / local-board live in the
    # existing branding record via core.settings.get_org — not duplicated here.)
    "founding_year": None,                  # int — track-record length (strategic_fit)

    # --- qualification (can we formally apply?) ---
    "legal_type": "nonprofit",              # canonical bucket (see core.auto_scorer
                                            # applicant buckets): nonprofit / government /
                                            # higher_ed / for_profit / individual / tribal
    "donor_registrations": [],              # e.g. "SAM.gov", "EU PADOR/PIC", "UNGM"
    "countries_registered": [],             # jurisdictions where legally registered

    # --- capacity (can we deliver?) ---
    "annual_budget_usd": None,              # number — org size / financial-capacity bar
    "largest_grant_usd": None,              # number — absorptive capacity for award size

    # --- strategic_fit (priorities + experience) ---
    "domains": [],                          # areas of demonstrated expertise / experience
    "priority_areas": [],                   # declared strategic priorities

    # --- geographic_fit (presence) ---
    "countries_of_operation": [],           # where we operate directly
    # Partners we can apply / form a consortium with, split by type:
    "trusted_partners": [],                 # non-profit: bilaterals / multilaterals
                                            # / INGOs / philanthropies (core.partners)
    "trusted_for_profit_partners": [],      # for-profit firms (free-add, codified)
    "trusted_academic_institutions": [],    # universities / research orgs (free-add, codified)

    # --- cofinancing ---
    "cofinancing_capacity": "limited",      # none | limited | moderate | strong

    # --- funder_relationship ---
    "funder_history": [],                   # funders we are/were funded by

    # --- bid_effort ---
    "proposal_languages": ["English"],      # languages we can write a competitive bid in
}

# Field order for stable UI rendering / iteration.
PROFILE_FIELDS: tuple[str, ...] = tuple(DEFAULT_PROFILE.keys())

# Free-text "tag list" fields (one value per line in the UI).
LIST_FIELDS: tuple[str, ...] = (
    "donor_registrations", "countries_registered", "countries_of_operation",
    "trusted_partners", "trusted_for_profit_partners",
    "trusted_academic_institutions", "domains", "priority_areas",
    "funder_history", "proposal_languages",
)

COFINANCING_LEVELS: tuple[str, ...] = ("none", "limited", "moderate", "strong")


def _deep_merge(base: dict, overlay: dict) -> dict:
    """base merged with overlay (overlay wins; lists replace wholesale)."""
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_profile() -> dict[str, Any]:
    """Active org profile (admin overrides merged onto defaults)."""
    raw = get_setting(ORG_PROFILE_KEY)
    if not raw:
        return copy.deepcopy(DEFAULT_PROFILE)
    try:
        overlay = json.loads(raw)
        if isinstance(overlay, dict):
            return _deep_merge(DEFAULT_PROFILE, overlay)
    except (ValueError, TypeError):
        pass
    return copy.deepcopy(DEFAULT_PROFILE)


def set_profile(profile: dict[str, Any], updated_by: str | None = None) -> None:
    """Persist the FULL profile blob."""
    set_setting(ORG_PROFILE_KEY, json.dumps(profile, indent=2), updated_by=updated_by)


def reset_to_defaults(updated_by: str | None = None) -> None:
    set_profile(copy.deepcopy(DEFAULT_PROFILE), updated_by=updated_by)


def is_configured() -> bool:
    """True once an admin has filled in enough to drive org-fit (at least one
    country of operation AND one domain or priority). Used to nudge setup
    before relying on the matching profile."""
    p = get_profile()
    return bool((p.get("countries_of_operation") or [])
                and ((p.get("domains") or []) or (p.get("priority_areas") or [])))
