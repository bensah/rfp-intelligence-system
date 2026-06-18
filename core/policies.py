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
        # Candidate must mention at least one of these to be admitted.
        "required_any": [
            "health", "disease", "infection", "epidemic", "pandemic",
            "HIV", "AIDS", "tuberculosis", "TB", "malaria",
            "vaccine", "immuni", "AMR", "antimicrobial",
            "maternal", "newborn", "child health", "nutrition",
            "global health", "primary care", "NCD", "non-communicable",
            "diagnostic", "treatment", "therapeutic", "outbreak",
            "essential medicine", "tropical", "neglected disease",
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
        ],
    },
    # Opportunity-TYPE opt-outs (title-based hard rejects). Defaults suit an
    # implementing org like the organisation that wants project grants/awards. An org that
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
    # Per-criterion rigor (0-5) + keyword bags. Rigor controls how strict
    # the keyword match must be to score the criterion as Yes / Partial / No.
    "criteria": {
        "feasibility": {
            "rigor": 2,
            "positive": ["feasible", "implementable", "ready", "established", "proven"],
            # Negative keywords on FEASIBILITY are scan-time HARD REJECTS,
            # not just "Score=No" nudges. Use this for phrases that mean
            # "we cannot do this kind of work" — e.g. clinical trials.
            "negative": ["clinical trial", "high risk", "highly experimental"],
        },
        "qualification": {
            # MUST 1 = alignment with the TARGET COUNTRY's national health
            # priorities (NOT "is government an eligible applicant"). Most
            # LMIC global-health calls map to national strategies, so this
            # DEFAULTS to Yes (see scoring_rules.criterion_defaults) and only
            # drops to No when a research-only negative keyword hits.
            "rigor": 3,
            "positive": [
                "government", "ministry of health", "country-led",
                "national strategy", "national plan", "national priorit",
                "policy", "health system", "public sector", "ministry",
                "surveillance", "observatory", "digital health", "e-health",
            ],
            # Donor-country priorities (not the deploying country's): research-only calls.
            # Flip MUST 1 to No unless the call clearly leads to scale-up.
            "negative": [
                "independent academic", "researcher-led only",
                "clinical trial", "randomized controlled", "randomised controlled",
                "drug development", "drug discovery", "vaccine development",
                "basic research", "purely academic", "preclinical",
            ],
        },
        "strategic_fit": {
            # Matches the deploying org's strategic program areas + health
            # system strengthening / digital health. One hit → Yes (rigor 1).
            "rigor": 1,
            "positive": [
                "health systems strengthening", "health system",
                "implementation", "scale up", "primary care", "primary health care",
                "service delivery", "supply chain", "access to medicine",
                "access to healthcare", "access to quality healthcare",
                "digital health", "e-health", "ehealth", "mhealth",
                "telemedicine", "telehealth", "health information",
                "surveillance", "observatory",
                "HIV", "AIDS", "tuberculosis", "malaria", "nutrition",
                "maternal", "newborn", "child health", "non-communicable",
                "NCD", "diagnostic", "treatment",
            ],
            "negative": [],
        },
        "capacity": {
            # An LMIC/Africa-targeted health call that cleared the country gate
            # is implementable by default (see criterion_defaults). Geography +
            # field-implementation keywords reinforce it; "pilot only" flips No.
            "rigor": 1,
            "positive": [
                "technical assistance", "deploy", "rollout", "scale",
                "implementation", "operational", "in the field",
                "field implementation", "africa", "sub-saharan", "asia",
                "developing countr", "low- and middle-income", "lmic",
            ],
            "negative": ["pilot only", "feasibility study only"],
        },
        "geographic_fit": {
            "rigor": 2,
            "positive": ["compliance", "regulatory", "approved", "ethical"],
            "negative": [],
        },
        "cofinancing": {
            "rigor": 2,
            "positive": ["funded", "budget", "support package", "partnership", "co-funded"],
            "negative": ["matching funds required", "self-funded only"],
        },
        "funding_quality": {
            "rigor": 2,
            "positive": [
                "multi-year", "flexible", "core funding",
                "unrestricted", "long-term",
            ],
            "negative": ["one-time only", "highly restricted"],
        },
        "funder_relationship": {
            "rigor": 2,
            "positive": [
                "monitoring", "evaluation", "M&E",
                "indicators", "metrics", "data",
            ],
            "negative": [],
        },
        "competitiveness": {
            "rigor": 2,
            "positive": ["partner", "collaboration", "consortium", "coalition"],
            "negative": ["sole bidder", "single applicant"],
        },
        "bid_effort": {
            # National / multi-district / regional reach = scale → Yes. A
            # national observatory counts. Single-site work with no scale
            # roadmap stays No (small pilot).
            "rigor": 1,
            "positive": [
                "scale", "scale up", "scale-up", "national", "nationwide",
                "country-wide", "system-wide", "population", "population-level",
                "observatory", "multi-district", "multiple districts",
                "regions", "regional", "national programme", "national program",
            ],
            "negative": ["small pilot only"],
        },
    },

    # =====================================================================
    # SCORING RULES — applied AFTER the keyword-based criterion scoring as
    # an override layer. Captures domain knowledge that can't be reduced to
    # keyword bags: funder identity (USG → admin burden + HQ reluctance),
    # amount-based funding quality, and default values for criteria where
    # human judgement matters more than RFP text (Partnership, Monitorable).
    # Each rule is admin-tunable from Admin → Settings → policies.
    # =====================================================================
    "scoring_rules": {
        # US-gov funders trigger reluctance + admin-burden flags. The
        # patterns match against funding_agency case-insensitively. When
        # any pattern hits, geographic_fit + cofinancing are forced
        # to "Partial" (a yellow flag for reviewers), regardless of what
        # the keyword scorer returned.
        "usg_funders": {
            "enabled": True,
            "patterns": [
                "USAID", "U.S. Agency for International Development",
                "Department of Defense", "DoD", "DOD-",
                "DHAPP", "PEPFAR",
                "Centers for Disease Control", "CDC",
                "National Institutes of Health", "NIH",
                "Health and Human Services", "HHS",
                "Department of State", "U.S. Department of",
                "Dept. of the Army", "Dept. of the Navy",
                "Dept. of the Air Force", "USAMRAA",
                "Department of Defense HIV", "Defense Health",
                "Grants.gov", "U.S. federal",
            ],
            "forced_values": {
                "geographic_fit": "Partial",
                "cofinancing": "Partial",
            },
        },

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

        # Large-amount → Resourcing burden = Partial (regardless of funder).
        # Stacks with usg_funders (both → Partial, no conflict).
        "resourcing_large_amount": {
            "enabled": True,
            "threshold_usd": 1_000_000,
            "forced_value": "Partial",
        },

        # Per-criterion default values applied when:
        #  (a) the keyword scorer returns "No" AND there's no positive text
        #      signal at all (i.e. we're confessing ignorance, not rejecting)
        # Used to encode "default-true unless explicit barrier" (Monitorable)
        # and "default-false unless reviewer confirms" (Partnership).
        "criterion_defaults": {
            "qualification": {
                "enabled": True,
                "default_value": "Yes",
                # LMIC global-health calls map to national health priorities
                # by default. Drops to "No" only when a research-only negative
                # keyword hits (clinical trial / drug or vaccine development /
                # basic research) — donor-country priorities, not the deploying country's.
                "respect_negative_keywords": True,
            },
            "capacity": {
                "enabled": True,
                "default_value": "Yes",
                # An LMIC-health call past the country gate is implementable
                # by default; "pilot only" / "feasibility study only" → No.
                "respect_negative_keywords": True,
            },
            "geographic_fit": {
                "enabled": True,
                "default_value": "Partial",
                # PLACEHOLDER until the donor_requirements matrix is wired.
                # Default Partial (→ review) rather than No, so a missing
                # compliance signal doesn't auto-Decline a valid RFP. The
                # matrix will set this True/Partial/False per donor.
                "respect_negative_keywords": True,
            },
            "cofinancing": {
                "enabled": True,
                "default_value": "Yes",
                # Default resourceable — timeline is usually fine for a
                # freshly-posted call. Your MUST 5 = timeline + requirements:
                # the resourcing_large_amount rule already nudges big budgets
                # to Partial; the deadline<2-weeks + document-package-weight
                # logic refines this once the donor matrix is wired.
                # "matching funds required" / "self-funded only" → No.
                "respect_negative_keywords": True,
            },
            "funder_relationship": {
                "enabled": True,
                "default_value": "Yes",
                # Even with no text match, default to Yes — most modern
                # grants assume M&E by default. Only set to No if negative
                # keywords explicitly hit (e.g. "no monitoring permitted").
                "respect_negative_keywords": True,
            },
            "competitiveness": {
                "enabled": True,
                "default_value": "No",
                # Partnership advantage is a reviewer-confirmed signal
                # (do we actually have a named partner?), not something
                # the RFP text can tell us. Default to No so the reviewer
                # actively flips it when a partner is lined up.
                "respect_positive_keywords": False,
            },
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


def get_policies() -> dict[str, Any]:
    """Return the active policies (admin overrides merged onto defaults)."""
    raw = get_setting(POLICIES_KEY)
    if not raw:
        return copy.deepcopy(DEFAULT_POLICIES)
    try:
        overlay = json.loads(raw)
        if isinstance(overlay, dict):
            return _deep_merge(DEFAULT_POLICIES, overlay)
    except (ValueError, TypeError):
        pass
    return copy.deepcopy(DEFAULT_POLICIES)


def set_policies(policies: dict[str, Any], updated_by: str | None = None) -> None:
    """Persist policies. Stores the FULL blob (not a delta) for simplicity."""
    set_setting(POLICIES_KEY, json.dumps(policies, indent=2), updated_by=updated_by)


def reset_to_defaults(updated_by: str | None = None) -> None:
    set_policies(copy.deepcopy(DEFAULT_POLICIES), updated_by=updated_by)
