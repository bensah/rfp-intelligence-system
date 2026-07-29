"""Admin-configurable eligibility & scoring policies.

Stored as a single JSON blob in `app_settings.value` under key `scan_policies`.
Used by:
  * core.scan_pipeline — country + theme filter applied BEFORE inserting a
    candidate (out-of-scope RFPs never enter the database).
  * core.auto_scorer — per-criterion rigor + keyword bags drive the auto
    assignment of MUST/PREFER values, alignment_score, auto_recommendation.

Designed so the same app can be re-pointed at a different organisation by
editing policies in Admin > Settings (no code change).
"""
from __future__ import annotations

import copy
import json
from typing import Any

from core.settings import get_setting, set_setting

POLICIES_KEY = "scan_policies"


# Sensible defaults for a typical global-health implementing-NGO
# deployment. Admin can tune from the UI per deploying org.
DEFAULT_POLICIES: dict[str, Any] = {
    "countries": {
        # Exact countries the deploying org works in. RFPs naming one of these
        # are eligible. Pick from the country list in Admin > Settings.
        "eligible": ["Cameroon", "Mali"],
        # Broad geographies (high-level UN regions / income tiers) that ALSO
        # admit a call — each matches via synonyms + member countries
        # (core.geographies). Leave EMPTY for strict country-only matching.
        # Canonical labels (see geographies.BROAD_GEOGRAPHIES); free text still
        # works but the dropdown keeps these consistent with donor scope.
        "broad_terms": [
            "Sub-Saharan Africa",
            "Low- and middle-income countries (LMICs)",
        ],
        # If True: when the RFP says nothing about geography, treat as
        # eligible (permissive). If False: reject geography-silent RFPs.
        "permissive_when_silent": True,
    },
    "themes": {
        # Candidate must mention at least one of these to be admitted. Broad
        # health vocabulary — terms, synonyms and related concepts — so health
        # procurement/supply and broadly-titled calls aren't missed. Matched
        # word-aware (see auto_scorer._theme_hit): short tokens/acronyms need a
        # whole-word boundary, longer terms match as a stem prefix.
        "required_any": [
            # health systems & care delivery
            "health", "healthcare", "health care", "health system",
            "health systems", "health service", "health services",
            "health facility", "public health", "global health", "primary care",
            "universal health coverage", "UHC", "health financing",
            "health workforce", "health worker", "community health",
            "digital health", "one health", "medical", "medicine", "medicines",
            "clinical", "clinic", "hospital", "nursing", "physician",
            "surgical", "surgery", "pharmaceutical", "medical equipment",
            "medical supplies", "medical device", "laboratory", "point-of-care",
            "biomedical", "health research",
            # infectious disease
            "disease", "infection", "infectious", "epidemic", "pandemic",
            "outbreak", "HIV", "AIDS", "antiretroviral", "tuberculosis", "TB",
            "malaria", "hepatitis", "cholera", "ebola", "measles", "polio",
            "dengue", "COVID", "coronavirus", "influenza", "meningitis",
            "pneumonia", "diarrhoeal", "diarrheal", "sepsis", "schistosomiasis",
            "neglected disease", "neglected tropical", "NTD", "tropical",
            "sexually transmitted",
            # NCDs & mental health
            "NCD", "non-communicable", "chronic disease", "cancer", "oncology",
            "cardiovascular", "hypertension", "diabetes", "mental health",
            # MNCH / SRHR / nutrition
            "maternal", "newborn", "child health", "child mortality",
            "maternal mortality", "paediatric", "pediatric", "adolescent health",
            "reproductive health", "sexual health", "SRHR", "family planning",
            "contraception", "antenatal", "obstetric", "nutrition",
            "malnutrition", "stunting",
            # prevention / cross-cutting
            "vaccine", "vaccination", "immuni", "immunization", "immunisation",
            "AMR", "antimicrobial", "diagnostic", "treatment", "therapeutic",
            "therapy", "essential medicine", "surveillance", "epidemiolog",
            "water and sanitation", "sanitation and hygiene", "safe water",
        ],
        # Hard reject at scan time if ANY of these appear in title/body.
        # Implementing-NGO deployments typically don't pursue early-phase
        # clinical trials or basic research, so they're excluded by default.
        # Admin can edit in Settings.
        "excluded_any": [
            "clinical trial",
            "phase i clinical",
            "phase ii clinical",
            "phase iii clinical",
            "in vitro",
            "preclinical",
            "basic research",
            # Out-of-capability terms relocated from the (retired) Feasibility
            # criterion — these hard-reject at scan time via the theme gate.
            "high risk",
            "highly experimental",
        ],
    },
    # Opportunity-TYPE opt-outs (title-based hard rejects). Defaults suit an
    # implementing org like CHAI that wants project grants/awards. An org that
    # DOES pursue training programs or loans turns the flag off in Settings.
    "exclusions": {
        "reject_training_only": True,   # "X Training Center / Education Program"
        "reject_loans": True,           # loans / concessional debt (not grants)
        "reject_consultancies": True,   # individual consultant / contractor RFPs
                                        # (org seeks project grants, not gigs)
        "reject_reimbursement": True,   # "X Reimbursement Program" — reimburses
                                        # named existing providers/grantees for
                                        # incurred costs; almost always a closed
                                        # domestic scheme, not an open grant.
    },
    # Who the deploying org IS, matched against what each RFP says it accepts.
    # When a call publishes an explicit eligible-applicant list that has NO open
    # type AND none of the org's types, it's structurally out of scope → reject.
    "eligibility": {
        # The deploying org's own applicant type(s), as canonical buckets:
        #   nonprofit | government | school_district | higher_ed |
        #   for_profit | individual | tribal
        # A typical implementing NGO is a nonprofit. Edit in Admin > Settings.
        "org_applicant_types": ["nonprofit"],
        # When True: reject a call whose published eligible-applicant list is
        # explicit, carries no "Unrestricted / open to any / Others" type, and
        # admits none of org_applicant_types. Calls with no published list, or
        # an open type, are never rejected on this basis (conservative).
        "reject_applicant_type_mismatch": True,
    },
    # Per-criterion KEYWORD ASSIST for the crawl (2026-06-17). Criteria are
    # primarily AUTO-DERIVED from the org profile (core/criteria_derive); these
    # admin-tunable terms supplement that when crawling RFP text:
    #   * a POSITIVE term found  → confirm the criterion ("Yes") when the
    #                              derivation couldn't determine it from profile data,
    #   * a NEGATIVE term found  → red flag → force the criterion to "No"
    #                              (for a MUST that screens the RFP out as Decline).
    # No terms for a criterion → the derivation / default classification stands.
    # Terms are aligned to each criterion's QUESTION (no Feasibility; no rigor —
    # the 2/1/0 scale is fixed). Feasibility's hard-reject terms live in
    # themes.excluded_any.
    "criteria": {
        # MUST 1 — Do we formally qualify? (org type / domestic-vs-global
        # registration / local board / donor registration / consortium-lead).
        # POSITIVE = RFP language matching our org TYPE or global registration;
        # NEGATIVE = local-only / disqualifying language → No.
        "qualification": {
            "positive": ["non-profit", "nonprofit", "not-for-profit", "charity",
                         "ngo", "non-governmental organization", "non-governmental",
                         "international organization", "international ngo",
                         "international philanthropy", "development agency",
                         "open to all", "any legal entity"],
            "negative": ["local organizations only", "local organisations only",
                         "grassroots", "grassroot", "local board of trustees",
                         "board of trustees required", "registered locally",
                         "local registration required", "community-based organization",
                         "national ngos only", "domestic applicants only",
                         "u.s. applicants only", "government agencies only",
                         "for-profit only", "individuals only"],
        },
        # MUST 2 — Fits our strategic priorities AND track record? POSITIVE = our
        # program/strategy language; NEGATIVE = off-strategy (research-only) work.
        "strategic_fit": {
            "positive": ["health system strengthening", "health systems",
                         "primary health care", "service delivery", "implementation",
                         "scale up", "disease prevention", "treatment", "diagnostics",
                         "access to medicines", "supply chain"],
            "negative": ["basic research", "laboratory study", "preclinical",
                         "fundamental science", "drug discovery", "bench science"],
        },
        # MUST 3 — Can we deliver at the award size/scope? POSITIVE = implementing-
        # org language; NEGATIVE = scope/size that excludes an established INGO.
        "capacity": {
            "positive": ["technical assistance", "implementing partner",
                         "implementation partner", "established organization",
                         "proven track record", "at scale", "prime recipient"],
            "negative": ["start-ups only", "startups only", "small grants only",
                         "micro-grant", "micro grants", "seed funding only",
                         "individuals only"],
        },
        # MUST 4 — Funder geography ↔ our presence / partner? POSITIVE = our
        # regions; NEGATIVE = geographies where we have no presence.
        "geographic_fit": {
            "positive": ["sub-saharan africa", "low- and middle-income", "lmic",
                         "developing countries", "global", "africa", "global south"],
            "negative": ["high-income countries only", "oecd countries only",
                         "domestic only", "united states only", "europe only"],
        },
        # MUST 5 — Co-financing/match + compliance? POSITIVE = none required;
        # NEGATIVE = match / cost-share required → No.
        "cofinancing": {
            "positive": ["no match required", "no cost-share", "no co-financing",
                         "fully funded", "no matching funds"],
            "negative": ["matching funds required", "match required",
                         "cost-share required", "cost sharing required",
                         "co-financing required", "cofinancing required",
                         "in-kind contribution required", "counterpart funding"],
        },
        # PREFER 6 — Funding terms attractiveness. POSITIVE = large/flexible/multi-
        # year/scale; NEGATIVE = small/restricted/one-off.
        "funding_quality": {
            "positive": ["multi-year", "multiyear", "core funding", "unrestricted",
                         "flexible funding", "long-term", "large grant", "at scale"],
            "negative": ["one-time only", "highly restricted", "seed funding",
                         "small grant", "one year only", "short-term"],
        },
        # PREFER 7 — Relationship with the funder (mostly org-side: funder_history /
        # donor registrations / trusted partners). RFP cues only here.
        "funder_relationship": {
            "positive": ["existing grantees", "current partners", "by invitation",
                         "current grantees eligible"],
            "negative": ["first-time applicants only", "new applicants only",
                         "not currently funded"],
        },
        # PREFER 8 — How well-positioned to win? POSITIVE = limited field / edge;
        # NEGATIVE = wide-open competition.
        "competitiveness": {
            "positive": ["by invitation", "invitation only", "limited competition",
                         "sole source", "restricted competition", "pre-qualified",
                         "incumbent"],
            "negative": ["highly competitive", "open competition",
                         "open to all applicants", "large number of applicants"],
        },
        # PREFER 9 — Proposal feasible in time/resources? POSITIVE = generous
        # runway; NEGATIVE = heavy/urgent process.
        "bid_effort": {
            "positive": ["rolling deadline", "rolling basis", "no deadline",
                         "applications accepted on an ongoing basis", "open call"],
            "negative": ["two-stage application", "full proposal required upfront",
                         "extensive documentation"],
        },
    },

    # =====================================================================
    # SCORING RULES — the per-criterion keyword bags + override layer were
    # RETIRED (2026-06-17); criteria are now objectively DERIVED from the org
    # profile (core/criteria_derive). Only the funding-quality value tiers
    # remain — read by derive_funding_quality for its High/Moderate/Low
    # thresholds (FX-converted to USD via core.dropdowns.usd_rate).
    # =====================================================================
    "scoring_rules": {
        # Amount-based funding quality. Picks the highest tier the
        # estimated_value satisfies. Always converted to USD first via the
        # FX layer (core.dropdowns.usd_rate) so mixed-currency grants
        # compare fairly.
        "funding_quality_tiers": {
            "enabled": True,
            # Ordered HIGH → LOW. The first tier whose threshold_usd is
            # met by estimated_value wins.
            "tiers": [
                {"threshold_usd": 2_000_000, "value": "Yes"},
                {"threshold_usd":   500_000, "value": "Partial"},
                {"threshold_usd":         0, "value": "No"},
            ],
        },
    },
}


# Ordered list of criterion keys for stable iteration (matches scorer.CRITERIA
# + feasibility leading).
CRITERION_KEYS: tuple[str, ...] = (
    "feasibility",
    "qualification",
    "strategic_fit",
    "capacity",
    "geographic_fit",
    "cofinancing",
    "funding_quality",
    "funder_relationship",
    "competitiveness",
    "bid_effort",
)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return base merged with overlay (overlay wins; lists replace wholesale)."""
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _blank_policies() -> dict[str, Any]:
    """Permissive policy for a NEW tenant that hasn't configured one: NO country or theme
    restriction (so calls populate broadly and the scorer flags Decline when nothing
    matches the tenant's minimal profile), while keeping the opportunity-NATURE hard gates
    (training/loan/consultancy exclusions) and applicant defaults. The tenant sharpens
    matching by filling its profile + Scan Preferences. This is the Option-C behaviour:
    the less a tenant configures, the more it sees (mostly Decline)."""
    p = copy.deepcopy(DEFAULT_POLICIES)
    p["countries"] = {"eligible": [], "broad_terms": [], "permissive_when_silent": True}
    _themes = p.get("themes") or {}
    _themes["required_any"] = []            # no theme gate → every sector populates
    p["themes"] = _themes
    return p


def _profile_geo_eligible() -> list[str]:
    """The tenant's OWN geographies — registered + operating countries from its org
    profile, canonicalised + deduped. Used to seed the geo gate when the tenant hasn't set
    an explicit scan policy, so a CONFIGURED org still hard-rejects off-geo calls (its
    profile IS its stated preference). Empty profile → [] → stays permissive (Option C).
    Best-effort: any error → []."""
    try:
        from core import org_profile as _op
        from core import geographies as _geo
        prof = _op.get_profile() or {}
        raw = (list(prof.get("org_registered_countries") or [])
               + list(prof.get("org_operating_countries") or []))
        out: list[str] = []
        seen: set[str] = set()
        for c in raw:
            cc = _geo.canonical_geo(c) or c
            k = str(cc).strip().lower()
            if cc and k not in seen:
                seen.add(k)
                out.append(cc)
        return out
    except Exception:
        return []


def _seed_geo_from_profile(pol: dict[str, Any]) -> dict[str, Any]:
    """If a policy has NO explicit eligible countries, seed them from the tenant's org
    profile so a configured org still geo-gates. No-op when eligible is already set (an
    explicit admin choice wins) or the profile has no countries (stays permissive)."""
    try:
        c = pol.get("countries") or {}
        if c.get("eligible") or c.get("broad_terms"):
            return pol                       # explicit geo scope (countries OR regions) set
        prof_geo = _profile_geo_eligible()
        if prof_geo:
            pol = copy.deepcopy(pol)
            pol.setdefault("countries", {})
            pol["countries"]["eligible"] = prof_geo
    except Exception:
        pass
    return pol


def _profile_theme_keywords() -> list[str]:
    """Theme keywords for the tenant's own program-area CATEGORIES (from its profile) —
    used to seed the theme gate when no explicit theme policy exists. Derives at the
    CATEGORY level (every sub-area sharing the org's prefixes) PLUS a broad umbrella term,
    so a call anywhere in the org's categories still matches; only clearly OFF-category
    calls (e.g. money-laundering for a health org) fail the gate. Deliberately errs toward
    KEEP (broad terms) — a theme gate must not hide a relevant call. Empty program areas →
    [] (permissive). Best-effort: any error → []."""
    try:
        from core import org_profile as _op
        from core.program_area_classifier import PROGRAM_AREA_KEYWORDS as _PAK
        prof = _op.get_profile() or {}
        codes: set[str] = set()
        for f in ("org_domain_expertise", "org_priority_areas"):
            for a in (prof.get(f) or []):
                if str(a).strip():
                    codes.add(str(a).strip())
        for f in ("org_domain_ratings", "org_priority_ratings"):
            r = prof.get(f)
            if isinstance(r, dict):
                codes.update(str(k).strip() for k in r if str(k).strip())
        if not codes:
            return []
        prefixes = {c.split(" - ", 1)[0].strip() for c in codes if " - " in c}
        kws: set[str] = set()
        for area, bag in _PAK.items():
            if area.split(" - ", 1)[0].strip() in prefixes:
                kws.update(str(k) for k in (bag or []))
        # Broad umbrella so a generically-worded same-family call still matches.
        if prefixes & {"WCH", "NCDs", "IDs", "HSS", "Cross-cutting"}:
            kws.update({"health", "global health", "public health", "healthcare"})
        if "EDU" in prefixes:
            kws.update({"education", "school", "learning", "teacher"})
        return sorted(k for k in kws if k)
    except Exception:
        return []


def _seed_themes_from_profile(pol: dict[str, Any]) -> dict[str, Any]:
    """If a policy has NO required theme keywords, seed them from the tenant's org-profile
    program-area categories so a configured org still theme-gates clearly off-category
    calls (symmetric to _seed_geo_from_profile). No-op when required_any is already set (an
    explicit admin choice wins) or the profile has no program areas (stays permissive)."""
    try:
        t = pol.get("themes") or {}
        if t.get("required_any"):
            return pol
        kws = _profile_theme_keywords()
        if kws:
            pol = copy.deepcopy(pol)
            pol.setdefault("themes", {})
            pol["themes"]["required_any"] = kws
    except Exception:
        pass
    return pol


def get_policies() -> dict[str, Any]:
    """Return the active policies (admin overrides merged onto defaults)."""
    raw = get_setting(POLICIES_KEY)
    if not raw:
        # No configured policy. A FRESH tenant (multi-tenant session OR a headless cron
        # per-tenant override) starts PERMISSIVE (populate + Decline); single-tenant keeps
        # the shipped CHAI defaults.
        pol = None
        try:
            from auth.tenant_context import (multitenant_enabled, current_tenant_id,
                                             override_tenant_id)
            if current_tenant_id() and (multitenant_enabled() or override_tenant_id()):
                pol = _blank_policies()
        except Exception:
            pol = None
        if pol is None:
            pol = copy.deepcopy(DEFAULT_POLICIES)
        # A tenant with NO configured scan policy still gates on its profile —
        # geographies (registered + operating) + program-area categories — restoring the
        # hard geo + theme rejects that multi-tenant blank policies inadvertently relaxed.
        # ONLY the unconfigured/blank branch is seeded: an EXPLICITLY-saved policy (below)
        # is honoured verbatim — including a deliberate region-only scope (broad_terms set,
        # eligible empty), which seeding would otherwise collapse to country-only.
        return _seed_themes_from_profile(_seed_geo_from_profile(pol))
    try:
        overlay = json.loads(raw)
        pol = (_deep_merge(DEFAULT_POLICIES, overlay)
               if isinstance(overlay, dict) else copy.deepcopy(DEFAULT_POLICIES))
    except (ValueError, TypeError):
        pol = copy.deepcopy(DEFAULT_POLICIES)
    return pol


def set_policies(policies: dict[str, Any], updated_by: str | None = None) -> None:
    """Persist policies. Stores the FULL blob (not a delta) for simplicity."""
    set_setting(POLICIES_KEY, json.dumps(policies, indent=2), updated_by=updated_by)


def reset_to_defaults(updated_by: str | None = None) -> None:
    set_policies(copy.deepcopy(DEFAULT_POLICIES), updated_by=updated_by)
