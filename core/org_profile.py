"""Organization profile — the one-time structured record of WHO the deploying
org is, compared against each RFP's requirements to answer the bid/no-bid
questions objectively (ML Phase 3).

Stored as a single JSON blob in `app_settings.value` under key `org_profile`
(same pattern as core.policies). Per-DEPLOYMENT, so it's multi-tenant-ready:
each tenant's Supabase carries its own profile; if tenants are ever pooled into
one DB, key the blob by org_id instead.

Every field is tagged with the bid/no-bid criterion it feeds:

  qualification        legal_type, donor_registrations, org_registered_countries
  strategic_fit        founding_year, domains, priority_areas
  capacity             annual_budget_usd, largest_grant_usd
  geographic_fit       org_operating_countries, trusted_partners
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
    "entity_type": "",                      # MUST-1 item B — grassroot_local |
                                            # multi_country | individual. SINGLE source of
                                            # truth; on save it derives the legacy
                                            # org_is_grassroot / org_is_multi_country settings.
                                            # Validation: legal_type=individual ⇒ individual.
    "donor_registrations": [],              # e.g. "SAM.gov", "EU PADOR/PIC", "UNGM"
    "org_registered_countries": [],             # jurisdictions where legally registered

    # --- capacity (can we deliver?) — multi-factorial MUST-3 inputs ---
    "annual_budget_usd": None,              # number — org size / financial-capacity bar
    "largest_grant_usd": None,              # number — biggest grant ever managed
    "lowest_grant_usd": None,               # number — smallest grant managed (range awareness)
    "number_of_grants_managed": None,       # int — track-record depth (raises the stretch)
    # (founding_year + org_stage, defined elsewhere, also feed the capacity stretch)

    # --- funding_quality (PREFER 6) — org's preferred award-size band (USD) ---
    "funding_target_low": None,             # floor of interest
    "funding_target_mid": None,             # sweet spot
    "funding_target_max": None,             # ceiling of interest
    # Bands use GEOMETRIC midpoints: cut1=sqrt(low*mid), cut2=sqrt(mid*max);
    # RFP value <=cut1 Low(0) / <=cut2 Moderate(1) / >cut2 High(2).

    # --- qualification (MUST 1) structural facts matched to donor conditions ---
    "org_is_independent_entity": True,      # not a branch/affiliate of a larger INGO
    "org_has_sam_uei": False,               # holds SAM.gov / UEI registration
    "org_tax_exempt": False,                # tax-exempt (501c3 or non-US equivalent)
    "org_stage": "established",             # "early-stage" | "established"
    "has_established_pi": False,            # MUST-1 item E — has a well-established
                                            # Principal Investigator (satisfies an
                                            # in-scope-country PI requirement)
    # Partners WITH type + country (for named-partner conditions, e.g. NIHR -> UK
    # academic). List of {name, type, country}. Complements the flat trusted_* lists.
    "partners": [],

    # --- strategic_fit + competitiveness (priorities vs track record) ---
    "org_domain_expertise": [],                          # areas of demonstrated expertise / experience
    "org_domain_ratings": {},                   # {child key: 0-5} TRACK-RECORD strength per
                                            # domain — feeds COMPETITIVENESS (how well-placed
                                            # we are to win an RFP in that exact area)
    "org_priority_areas": [],                   # declared strategic priorities (may have no footprint yet)
    "org_priority_ratings": {},             # {child key: 0-5} STRATEGY priority per area —
                                            # feeds STRATEGIC FIT (MUST-2), correlated with
                                            # donor_intel.program_area_ratings

    # --- geographic_fit (presence) ---
    "org_operating_countries": [],           # where we operate directly
    # Partners we can apply / form a consortium with, split by type:
    "trusted_partners": [],                 # non-profit: bilaterals / multilaterals
                                            # / INGOs / philanthropies (core.partners)
    "trusted_for_profit_partners": [],      # for-profit firms (free-add, codified)
    "trusted_academic_institutions": [],    # universities / research orgs (free-add, codified)

    # --- cofinancing & compliance (MUST 5) ---
    "cofinancing_capacity": "limited",      # none | limited | moderate | strong
    # Hard pre-acquire compliance credentials the org ALREADY holds — each gates a
    # donor requirement of the same name (org lacks it + donor requires it -> 0).
    "has_audited_financials": False,        # recent independently audited financials
    "has_audit_report": False,              # a formal external audit report on file
    "has_safeguarding_policy": False,       # safeguarding / PSEA policy in place
    "has_partner_mou": False,               # signed MOU(s) with implementing partner(s)
    "has_govt_mou": False,                  # signed MOU with host-government authority
    "has_govt_endorsement": False,          # can secure a government endorsement letter
    # Donors we have ALREADY obtained an authorized-signatory sign-off from — matched
    # by name to a call's donor when it requires authorized-signatory sign-off.
    "authorized_signatory_donors": [],
    # Funding ROUTES the org can RECEIVE through (tokens: grant | procurement | loan |
    # subrecipient | govt_ccm | direct). Matched (≥1 overlap) to the call/donor routes.
    "org_funding_routes": ["grant", "subrecipient"],

    # --- funder_relationship ---
    "funder_history": [],                   # funders we are/were funded by
    # MUST-1 item I — donors CURRENTLY funding us (a current-grant exclusion in a
    # call disqualifies us; distinct from funder_history = past/previous grants).
    "active_donors": [],

    # --- bid_effort ---
    "proposal_languages": ["English"],      # languages we can write a competitive bid in
}

# Field order for stable UI rendering / iteration.
PROFILE_FIELDS: tuple[str, ...] = tuple(DEFAULT_PROFILE.keys())

# Free-text "tag list" fields (one value per line in the UI).
LIST_FIELDS: tuple[str, ...] = (
    "donor_registrations", "org_registered_countries", "org_operating_countries",
    "trusted_partners", "trusted_for_profit_partners",
    "trusted_academic_institutions", "org_domain_expertise", "org_priority_areas",
    "funder_history", "active_donors", "proposal_languages",
    "authorized_signatory_donors", "org_funding_routes",
)

COFINANCING_LEVELS: tuple[str, ...] = ("none", "limited", "moderate", "strong")

# Canonical legal_type buckets -> human-readable labels. Stored value stays the
# canonical code (matched by the scan/scorer); the form dropdown and the
# read-only Organization view show the label via legal_type_label().
LEGAL_TYPE_LABELS: dict[str, str] = {
    "nonprofit": "Non-profit organization",
    "government": "Government",
    "higher_ed": "Higher Education",
    "for_profit": "For-profit company",
    "individual": "Individual",
    "tribal": "Tribal organization",
}


def legal_type_label(code: Any) -> str:
    """Readable label for a stored legal_type code; unknown codes are
    title-cased with underscores stripped, empty -> em dash."""
    s = str(code or "").strip()
    if not s:
        return "—"
    return LEGAL_TYPE_LABELS.get(s, s.replace("_", " ").title())


# MUST-1 item B — entity-registration type (replaces the org_is_grassroot /
# org_is_multi_country checkboxes). Stored value is the canonical code; the form
# shows the label via entity_type_label(). "" = not specified.
ENTITY_TYPE_LABELS: dict[str, str] = {
    "": "(not specified)",
    "grassroot_local": "Grassroot / Local Organization",
    "multi_country": "Multi-country Organization",
    "individual": "Individual",
}


def entity_type_label(code: Any) -> str:
    """Readable label for a stored entity_type code; empty -> '(not specified)'."""
    s = str(code or "").strip()
    return ENTITY_TYPE_LABELS.get(s, s.replace("_", " ").title())


def _deep_merge(base: dict, overlay: dict) -> dict:
    """base merged with overlay (overlay wins; lists replace wholesale)."""
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# Data-model rename ledger (owner 2026-06-29). The org profile is a stored JSON blob,
# so renamed KEYS are migrated on read (old → new) — a profile saved before the rename
# still loads correctly, and set_profile() then persists the new keys. Append one entry
# per renamed org key as each axis migrates.
_RENAMED_KEYS = {
    # Geography (axis 1)
    "countries_registered": "org_registered_countries",
    "countries_of_operation": "org_operating_countries",
    # Program areas (axis 2)
    "priority_areas": "org_priority_areas",
    "program_area_ratings": "org_priority_ratings",
    "domains": "org_domain_expertise",
    "domain_ratings": "org_domain_ratings",
}


def _migrate_keys(overlay: dict) -> dict:
    """Rename legacy org-profile keys to their current names (see _RENAMED_KEYS)."""
    for old, new in _RENAMED_KEYS.items():
        if old in overlay and new not in overlay:
            overlay[new] = overlay.pop(old)
    return overlay


def get_profile() -> dict[str, Any]:
    """Active org profile (admin overrides merged onto defaults)."""
    raw = get_setting(ORG_PROFILE_KEY)
    if not raw:
        return copy.deepcopy(DEFAULT_PROFILE)
    try:
        overlay = json.loads(raw)
        if isinstance(overlay, dict):
            return _deep_merge(DEFAULT_PROFILE, _migrate_keys(overlay))
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
    return bool((p.get("org_operating_countries") or [])
                and ((p.get("org_domain_expertise") or []) or (p.get("org_priority_areas") or [])))
