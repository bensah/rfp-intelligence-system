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


# Sensible defaults seeded from the CHAI BDT reference deployment —
# admin can tune from the UI per deploying org.
DEFAULT_POLICIES: dict[str, Any] = {
    "countries": {
        # Primary countries the deploying org works in. RFPs explicitly
        # mentioning these are eligible. Edit in Admin > Settings.
        "eligible": ["Cameroon", "Mali"],
        # Broad-geography terms that imply our countries are in scope.
        "broad_terms": [
            "LMIC", "low- and middle-income", "low and middle income",
            "developing countr", "Africa", "Sub-Saharan", "sub-Saharan",
            "global", "worldwide", "international", "low-income",
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
        "must_1_govt_alignment": {
            "rigor": 3,
            "positive": [
                "government", "ministry of health", "country-led",
                "national strategy", "national plan", "national priorit",
                "policy", "health system", "public sector", "ministry",
            ],
            "negative": ["independent academic", "researcher-led only"],
        },
        "must_2_strategic_fit": {
            "rigor": 3,
            "positive": [
                "health systems strengthening", "implementation",
                "scale up", "primary care", "service delivery",
                "supply chain", "access to medicine",
            ],
            "negative": [],
        },
        "must_3_implementable": {
            "rigor": 2,
            "positive": [
                "technical assistance", "deploy", "rollout", "scale",
                "implementation", "operational",
            ],
            "negative": ["pilot only", "feasibility study only"],
        },
        "must_4_compliant": {
            "rigor": 2,
            "positive": ["compliance", "regulatory", "approved", "ethical"],
            "negative": [],
        },
        "must_5_resourcing": {
            "rigor": 2,
            "positive": ["funded", "budget", "support package", "partnership", "co-funded"],
            "negative": ["matching funds required", "self-funded only"],
        },
        "prefer_6_funding_quality": {
            "rigor": 2,
            "positive": [
                "multi-year", "flexible", "core funding",
                "unrestricted", "long-term",
            ],
            "negative": ["one-time only", "highly restricted"],
        },
        "prefer_7_monitorable": {
            "rigor": 2,
            "positive": [
                "monitoring", "evaluation", "M&E",
                "indicators", "metrics", "data",
            ],
            "negative": [],
        },
        "prefer_8_partnership": {
            "rigor": 2,
            "positive": ["partner", "collaboration", "consortium", "coalition"],
            "negative": ["sole bidder", "single applicant"],
        },
        "prefer_9_scale": {
            "rigor": 2,
            "positive": [
                "scale", "national", "country-wide", "population", "system-wide",
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
        # any pattern hits, must_4_compliant + must_5_resourcing are forced
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
                "must_4_compliant": "Partial",
                "must_5_resourcing": "Partial",
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
            "prefer_7_monitorable": {
                "enabled": True,
                "default_value": "Yes",
                # Even with no text match, default to Yes — most modern
                # grants assume M&E by default. Only set to No if negative
                # keywords explicitly hit (e.g. "no monitoring permitted").
                "respect_negative_keywords": True,
            },
            "prefer_8_partnership": {
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
    "must_1_govt_alignment",
    "must_2_strategic_fit",
    "must_3_implementable",
    "must_4_compliant",
    "must_5_resourcing",
    "prefer_6_funding_quality",
    "prefer_7_monitorable",
    "prefer_8_partnership",
    "prefer_9_scale",
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
