"""The scoring analysis for ONE opportunity, whichever store it came from.

The opportunity page has to answer a decision question — "should this go into our pipeline
or not?" — for calls that have NEVER been screened for this tenant (the shared catalogue
that the Featured card ranks). So the same nine criteria have to be computable from a raw
extraction, not only from an already-screened row.

Everything authoritative is reused, never re-implemented:
  * `criteria_derive.derive_criteria` / `factor_breakdown` / `fatal_decline` — the criteria
  * `matching.composite_match`                                              — Bid Strength
  * `criteria_review.criterion_label` / `count_text`                        — the wording
  * `data_quality`                                                         — confidence
so this page and the Review screen cannot disagree about the same opportunity.

Bid Strength here is COMPUTED LIVE, not read from `alignment_score`. A stored score is a
snapshot from whenever the row was last scanned, and after any scoring fix it drifts from
what Review shows for the same row — which is the class of bug the criteria rework existed
to kill.
"""
from __future__ import annotations

from typing import Any

from core import criteria_derive as _cd
from core import criteria_review as _crev
from core import data_quality as _dq
from core import matching as _matching
from core.scorer import CRITERIA, criterion_score

# Per-criterion weight of Bid Strength: MUST .65 + PREFER .35 = 1.0. Mirrors the Review
# screen's breakdown so the two show the same contribution for the same criterion.
WEIGHTS = {"qualification": .15, "strategic_fit": .15, "capacity": .15,
           "geographic_fit": .10, "cofinancing": .10, "funding_quality": .08,
           "funder_relationship": .08, "competitiveness": .10, "bid_effort": .09}

LABELS = {
    "qualification": "MUST 1 · Legal status & qualification",
    "strategic_fit": "MUST 2 · Strategic fit",
    "capacity": "MUST 3 · Implementation capacity",
    "geographic_fit": "MUST 4 · Geographic fit",
    "cofinancing": "MUST 5 · Cofinancing & compliance",
    "funding_quality": "PREFER 6 · Funding quality",
    "funder_relationship": "PREFER 7 · Donor relationship",
    "competitiveness": "PREFER 8 · Competitiveness",
    "bid_effort": "PREFER 9 · Bid effort",
}


def fit_label(composite: float) -> str:
    """Overall match strength only — it does NOT set the decision."""
    return ("Strong fit" if composite >= 80
            else "Moderate fit" if composite > 50 else "Low fit")


def decide(composite: float, *, fatal: bool = False,
           below_award_floor: bool = False) -> str:
    """Mirror auto_scorer.recommend_from_composite: a fatal gate → Decline; else >=90
    Proceed · 70-89 Park · <70 Decline, with a below-floor award capping Proceed at Park."""
    if fatal:
        return "Decline"
    rec = "Proceed" if composite >= 90 else "Park" if composite >= 70 else "Decline"
    return "Park" if (rec == "Proceed" and below_award_floor) else rec


def analyse(rfp: dict, org: dict | None, donor: dict | None,
            org_settings: dict | None = None, *,
            rfp_compliance: dict | None = None,
            overrides: dict | None = None) -> dict:
    """The full scoring analysis for one opportunity-shaped dict.

    `rfp` may be a screened `rfp_submissions` row OR a candidate built from a raw
    extraction (see opportunity_detail.to_candidate) — the derivation reads the same
    `call_*` fields either way, so an unscreened catalogue call gets a real analysis rather
    than a blank.

    Returns a dict the page renders directly; every number in it is computed here so the
    page holds no scoring logic of its own.
    """
    org = org or {}
    donor_eff = _cd._merge_rfp_compliance(donor, rfp_compliance)
    derived = _cd.derive_criteria(rfp, org, donor_eff, org_settings)
    bd = _cd.factor_breakdown(rfp, org, donor_eff, org_settings,
                              overrides=overrides or {})
    try:
        fatal, trigger = _cd.fatal_decline(org, rfp, donor_eff, org_settings)
    except Exception:
        fatal, trigger = False, None

    criteria: list[dict] = []
    labels: dict[str, Any] = {}
    for key in CRITERIA:
        facts = bd.get(key) or []
        label = _crev.criterion_label(key, facts, derived.get(key))
        labels[key] = label
        unsure = criterion_score(label) is None
        num, total, pct = _crev.criterion_count(key, facts, label)
        sc = criterion_score(label)
        frac = 0.5 if sc is None else sc / 2.0
        criteria.append({
            "key": key,
            "title": LABELS[key],
            "label": label,
            "band": sc,                                  # 2 / 1 / 0 / None
            "count_text": _crev.count_text(key, facts, label, unsure),
            "scored": bool(total),
            "pct": pct,
            "weight": WEIGHTS[key],
            "points": WEIGHTS[key] * frac * 100.0,
            "note": _crev.label_source_note(key, facts, label),
            "components": facts,
            "is_must": key in CRITERIA[:5],
        })

    try:
        match = _matching.composite_match({**rfp, **labels}, org, donor_eff, org_settings)
        composite = round(match["composite"], 1)
    except Exception:
        composite = round(sum(c["points"] for c in criteria), 1)
    try:
        below_floor = _cd.below_award_floor(rfp, org)
    except Exception:
        below_floor = False

    system = decide(composite, fatal=fatal, below_award_floor=below_floor)
    dpct = _dq.donor_completeness(donor)[0]
    cpct = _dq.call_completeness(rfp)[0]
    band, bpct = _dq.confidence_band(dpct, cpct)
    adjusted, conf_note = _dq.confidence_adjusted(system, band)

    return {
        "bid_strength": int(composite + 0.5),            # half-up, as the gauge shows it
        "composite": composite,
        "fit": fit_label(composite),
        "system_decision": system,
        "suggested_decision": adjusted,
        "confidence_note": conf_note,
        "fatal": bool(fatal),
        "fatal_trigger": trigger,
        "below_award_floor": below_floor,
        "confidence": {
            "band": band, "pct": bpct, "call_pct": cpct,
            "donor_pct": dpct, "donor_matched": _dq.donor_matched(donor),
        },
        "criteria": criteria,
        "labels": labels,
        "blockers": [c for c in criteria if c["band"] == 0],
        "unscored": [c for c in criteria if not c["scored"]],
    }
