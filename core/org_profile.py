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
  cofinancing          cofinancing_capacity, prefinance_capacity
  funder_relationship  funder_history
  bid_effort           proposal_languages

Humans answer the form today; the profile is the reference a responder checks
against (and, once an LLM extractor lands, what the RFP requirements are matched
against automatically). It also makes the model genuinely per-organization.
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any

from core.settings import get_setting, set_setting

# get_profile() is the app's hottest read: it runs on Entity/Screen/Review/Records/Org-setup,
# TWICE per call inside policies.get_policies(), and several times PER ROW during live
# scoring — each one previously a fresh `tenants.org_profile` round-trip (~0.35s). Cache the
# RAW OVERLAY per tenant for a few seconds; the merge onto DEFAULT_PROFILE still happens on
# every call so callers keep getting their own mutable dict (the Org-setup editor mutates the
# result — handing out a shared merged object would let one caller poison the cache).
# Mirrors the module-cache pattern in core/settings.py. set_profile() invalidates.
_PROFILE_CACHE: dict[str, tuple[float, Any]] = {}
_PROFILE_TTL = 30.0


def _clear_profile_cache(tid: str | None = None) -> None:
    if tid is None:
        _PROFILE_CACHE.clear()
    else:
        _PROFILE_CACHE.pop(str(tid), None)

ORG_PROFILE_KEY = "org_profile"


# Defaults sketch a typical global-health implementing NGO (the reference
# deployment). An admin fills the real values in Admin > Settings once.
DEFAULT_PROFILE: dict[str, Any] = {
    # --- identity ---
    # (org name / short / country / logo / US-entity / local-board live in the
    # existing branding record via core.settings.get_org — not duplicated here.)
    "org_founding_year": None,                  # int — track-record length (strategic_fit)

    # --- qualification (can we formally apply?) ---
    "org_legal_type": "nonprofit",              # canonical bucket (see core.auto_scorer
                                            # applicant buckets): nonprofit / government /
                                            # higher_ed / for_profit / individual / tribal
    "org_entity_type": "",                      # MUST-1 item B — grassroot_local |
                                            # multi_country | individual. SINGLE source of
                                            # truth; on save it derives the legacy
                                            # org_is_grassroot / org_is_multi_country settings.
                                            # Validation: legal_type=individual ⇒ individual.
    "org_donor_registrations": [],              # e.g. "SAM.gov", "EU PADOR/PIC", "UNGM"
    "org_registered_countries": [],             # jurisdictions where legally registered

    # --- capacity (can we deliver?) — multi-factorial MUST-3 inputs ---
    "org_annual_budget": None,              # number — org size / financial-capacity bar
    "org_largest_grant": None,              # number — biggest grant ever managed
    "org_lowest_grant": None,               # number — smallest grant managed (range awareness)
    "org_grants_count": None,       # int — track-record depth (raises the stretch)
    # (founding_year + org_stage, defined elsewhere, also feed the capacity stretch)

    # --- funding_quality (PREFER 6) — org's preferred award-size band (USD) ---
    "org_min_target": None,             # floor of interest
    "org_mid_target": None,             # sweet spot
    "org_max_target": None,             # ceiling of interest
    # Bands use GEOMETRIC midpoints: cut1=sqrt(low*mid), cut2=sqrt(mid*max);
    # RFP value <=cut1 Low(0) / <=cut2 Moderate(1) / >cut2 High(2).

    # --- qualification (MUST 1) structural facts matched to donor conditions ---
    "org_is_independent_entity": True,      # not a branch/affiliate of a larger INGO
    "org_has_sam_uei": False,               # holds SAM.gov / UEI registration
    "org_tax_exempt": False,                # tax-exempt (501c3 or non-US equivalent)
    "org_stage": "established",             # "early-stage" | "established"
    "org_has_established_pi": False,            # MUST-1 item E — has a well-established
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
    # DERIVED, not edited: program areas the tenant's own users named on their accounts.
    # Filled by get_profile() from core.user_program_areas and worth the top of the band
    # in MUST-2, because one colleague saying "this is what I work on" is harder evidence
    # than a profile row nobody has revisited. Never persisted by set_profile.
    "org_user_declared_areas": [],

    # --- geographic_fit (presence) ---
    "org_operating_countries": [],           # where we operate directly
    # Partners we can apply / form a consortium with, split by type:
    "trusted_partners": [],                 # non-profit: bilaterals / multilaterals
                                            # / INGOs / philanthropies (core.partners)
    "trusted_for_profit_partners": [],      # for-profit firms (free-add, codified)
    "trusted_academic_institutions": [],    # universities / research orgs (free-add, codified)

    # --- cofinancing & compliance (MUST 5) ---
    # MUST-5 indirect cost — our own overhead rate as a % of project cost, matched
    # against the maximum a call/funder reimburses (see criteria_derive indirect_cost).
    "org_indirect_cost_rate": None,         # number 0-100, e.g. 15 for 15%
    "org_cofinancing_capacity": "limited",      # none | limited | strong
    # PRE-FINANCING is a DIFFERENT capability from co-financing (owner 2026-08-10):
    # co-financing = we commit our OWN funds alongside the award; pre-financing = we can
    # carry the grant's costs up front and be reimbursed later. Scoring one against the
    # other is what MUST-5 used to do. Blank = not recorded, so the component stays
    # unscored and out of the denominator rather than borrowing the co-financing answer.
    "org_prefinance_capacity": None,            # none | limited | strong
    # Hard pre-acquire compliance credentials the org ALREADY holds — each gates a
    # donor requirement of the same name (org lacks it + donor requires it -> 0).
    "org_has_audited_financials": False,        # recent independently audited financials
    "org_has_audit_report": False,              # a formal external audit report on file
    "org_has_safeguarding_policy": False,       # safeguarding / PSEA policy in place
    "org_has_partner_mou": False,               # signed MOU(s) with implementing partner(s)
    "org_has_govt_mou": False,                  # signed MOU with host-government authority
    "org_has_govt_endorsement": False,          # can secure a government endorsement letter
    # Donors we have ALREADY obtained an authorized-signatory sign-off from — matched
    # by name to a call's donor when it requires authorized-signatory sign-off.
    "org_authorized_signatory_donors": [],
    # Funding ROUTES the org can RECEIVE through (tokens: grant | procurement | loan |
    # subrecipient | govt_ccm | direct). Matched (≥1 overlap) to the call/donor routes.
    "org_funding_routes": ["grant", "subrecipient"],

    # --- funder_relationship ---
    "org_funder_history": [],                   # funders we are/were funded by
    # MUST-1 item I — donors CURRENTLY funding us (a current-grant exclusion in a
    # call disqualifies us; distinct from funder_history = past/previous grants).
    "org_active_donors": [],
    # Donors we've ENGAGED with (meetings, concept notes, EOIs) but have NOT yet been
    # funded by — a warm relationship weaker than a past grant. Feeds PREFER-7 as the
    # "Donor engaged" component (a call from one of these scores 1).
    "org_engaged_donors": [],

    # --- bid_effort ---
    "proposal_languages": ["English"],      # languages we can write a competitive bid in
}

# Field order for stable UI rendering / iteration.
PROFILE_FIELDS: tuple[str, ...] = tuple(DEFAULT_PROFILE.keys())

# Free-text "tag list" fields (one value per line in the UI).
LIST_FIELDS: tuple[str, ...] = (
    "org_donor_registrations", "org_registered_countries", "org_operating_countries",
    "trusted_partners", "trusted_for_profit_partners",
    "trusted_academic_institutions", "org_domain_expertise", "org_priority_areas",
    "org_funder_history", "org_active_donors", "org_engaged_donors",
    "proposal_languages", "org_authorized_signatory_donors", "org_funding_routes",
)

# Three levels, mapping 1:1 onto the three component scores (owner 2026-08-10):
#   none -> 0.0 "Not met" · limited -> 0.5 "Partial, with effort" · strong -> 1.0 "Yes,
#   fully met". "moderate" was dropped because it scored 1.0, exactly like "strong" — a
#   third label for a second outcome. Profiles saved with it still score 1.0 (see
#   criteria_derive._capacity_score), so no stored answer changes meaning.
COFINANCING_LEVELS: tuple[str, ...] = ("none", "limited", "strong")

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
    # Award / funding (axis 3)
    "funding_target_low": "org_min_target",
    "funding_target_mid": "org_mid_target",
    "funding_target_max": "org_max_target",
    "largest_grant_usd": "org_largest_grant",
    "annual_budget_usd": "org_annual_budget",
    "lowest_grant_usd": "org_lowest_grant",
    "number_of_grants_managed": "org_grants_count",
    # Legal status & eligibility (axis 4)
    "legal_type": "org_legal_type",
    "entity_type": "org_entity_type",
    "has_established_pi": "org_has_established_pi",
    "active_donors": "org_active_donors",
    "funder_history": "org_funder_history",
    # Cofinancing & compliance (axis 5)
    "cofinancing_capacity": "org_cofinancing_capacity",
    "has_audited_financials": "org_has_audited_financials",
    "has_audit_report": "org_has_audit_report",
    "has_safeguarding_policy": "org_has_safeguarding_policy",
    "has_partner_mou": "org_has_partner_mou",
    "has_govt_mou": "org_has_govt_mou",
    "has_govt_endorsement": "org_has_govt_endorsement",
    "authorized_signatory_donors": "org_authorized_signatory_donors",
    # Relationship / competitiveness / bid-effort (axis 6)
    "donor_registrations": "org_donor_registrations",
    "founding_year": "org_founding_year",
}


def _migrate_keys(overlay: dict) -> dict:
    """Rename legacy org-profile keys to their current names (see _RENAMED_KEYS)."""
    for old, new in _RENAMED_KEYS.items():
        if old in overlay and new not in overlay:
            overlay[new] = overlay.pop(old)
    return overlay


def _tenant_store(tenant_id: str | None = None) -> tuple[Any, str] | None:
    """(service_client, tenant_id) when multi-tenant is ON and a tenant resolves — so the
    org profile is read/written PER TENANT from `tenants.org_profile`. None otherwise
    (single-tenant → legacy app_settings blob). `tenant_id` overrides the session tenant
    (super_user viewing/editing another tenant). Best-effort; any failure falls back to
    the legacy store. Delegates to auth.tenant_context.tenant_store (service client)."""
    try:
        from auth import tenant_context as tc
        return tc.tenant_store(tenant_id)
    except Exception:
        return None


def _coerce_overlay(op: Any) -> dict | None:
    if isinstance(op, dict):
        return op
    if isinstance(op, str) and op.strip():
        try:
            v = json.loads(op)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def get_profile(tenant_id: str | None = None) -> dict[str, Any]:
    """Active org profile (overrides merged onto defaults). PER-TENANT when multi-tenant
    is on (reads the tenant's tenants.org_profile — a brand-new tenant like RFPIS Inc.
    starts blank → just the DEFAULT_PROFILE); the global app_settings blob otherwise.
    `tenant_id` overrides the session tenant (super_user viewing another tenant)."""
    overlay: dict | None = None
    store = _tenant_store(tenant_id)
    if store is not None:
        client, tid = store
        # Keyed on the RESOLVED tid (not the tenant_id argument) so get_profile() and
        # get_profile(tid) share one entry. A cached None/{} is a valid result and is
        # honoured — an org with no profile yet shouldn't re-query every call either.
        _key = str(tid)
        _hit = _PROFILE_CACHE.get(_key)
        if _hit is not None and (time.monotonic() - _hit[0]) < _PROFILE_TTL:
            overlay = _hit[1]
        else:
            try:
                rows = (client.table("tenants").select("org_profile")
                        .eq("id", tid).limit(1).execute().data or [])
                if rows:
                    overlay = _coerce_overlay(rows[0].get("org_profile"))
                _PROFILE_CACHE[_key] = (time.monotonic(), overlay)
            except Exception:
                overlay = None      # transient error → do NOT cache the failure
    if overlay is None and store is None:
        overlay = _coerce_overlay(get_setting(ORG_PROFILE_KEY))
    prof = (copy.deepcopy(DEFAULT_PROFILE) if not overlay
            else _deep_merge(DEFAULT_PROFILE, _migrate_keys(overlay)))
    # Program areas the tenant's own users declared on their accounts. Resolved HERE, not
    # inside scoring, so the criteria stay a pure function of the profile dict they are
    # handed and remain testable without a database. Best-effort: a failure leaves the key
    # empty and the profile alone decides, exactly as before this signal existed.
    try:
        from core.user_program_areas import declared_keys
        prof["org_user_declared_areas"] = declared_keys(tenant_id)
    except Exception:
        prof.setdefault("org_user_declared_areas", [])
    return prof


def set_profile(profile: dict[str, Any], updated_by: str | None = None,
                tenant_id: str | None = None) -> None:
    """Persist the FULL profile blob — to a tenant's tenants.org_profile (multi-tenant)
    or the global app_settings blob (single-tenant). `tenant_id` overrides the session
    tenant (super_user editing another tenant)."""
    # DERIVED keys never persist. `org_user_declared_areas` is computed from the users
    # table on every read; writing it back would freeze one moment's answer into the
    # profile, and it would then go stale the moment somebody edits their own account -
    # while looking for all the world like something an admin had chosen.
    if "org_user_declared_areas" in (profile or {}):
        profile = {k: v for k, v in profile.items()
                   if k != "org_user_declared_areas"}
    store = _tenant_store(tenant_id)
    if store is not None:
        client, tid = store
        # The write MUST land on the tenant record — do NOT swallow a failure into the
        # legacy global blob (that silently writes the wrong place and reads back as "not
        # saved"). Surface the real error so the caller can show it.
        res = client.table("tenants").update({"org_profile": profile}).eq("id", tid).execute()
        if not (getattr(res, "data", None)):
            raise RuntimeError(
                f"tenants.org_profile update for {tid} affected 0 rows — the write did "
                "not persist (check RLS / that SUPABASE_KEY is the service-role key).")
        _clear_profile_cache(tid)         # next read must see the just-saved profile
        return
    set_setting(ORG_PROFILE_KEY, json.dumps(profile, indent=2), updated_by=updated_by)
    _clear_profile_cache()


def reset_to_defaults(updated_by: str | None = None) -> None:
    set_profile(copy.deepcopy(DEFAULT_PROFILE), updated_by=updated_by)


def is_configured() -> bool:
    """True once an admin has filled in enough to drive org-fit (at least one
    country of operation AND one domain or priority). Used to nudge setup
    before relying on the matching profile."""
    p = get_profile()
    return bool((p.get("org_operating_countries") or [])
                and ((p.get("org_domain_expertise") or []) or (p.get("org_priority_areas") or [])))
