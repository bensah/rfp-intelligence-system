"""Single source of truth for a row's LIVE Bid Strength + Auto-decision + probability.

Every surface that shows those numbers — the Review screen, the Screen table, the
Settings › Records table, and the View-RFP modal — MUST derive them HERE so they can
never disagree. The stored `alignment_score` / `auto_recommendation` columns are only a
SCAN-TIME SNAPSHOT: they go stale the moment the org profile, donor intel, or scoring
logic changes (e.g. a row scanned at 45.5/Decline that today derives 92/Proceed). This
recomputes fresh, via the exact same path scan-time scoring uses
(criteria_derive.derive_criteria → matching.composite_match → fatal_decline → band).
"""
from __future__ import annotations

from typing import Any

# The nine MUST/PREFER criterion labels auto_score emits.
CRITERIA = (
    "qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing",
    "funding_quality", "funder_relationship", "competitiveness", "bid_effort",
)


def assess_row(row: dict[str, Any], policies: dict | None = None) -> dict[str, Any]:
    """LIVE assessment of one rfp_submissions row.

    Returns {alignment_score, auto_recommendation, probability, <9 criteria labels>},
    computed via the SAME core scoring path as scan-time `auto_score` (so a value shown
    on any screen matches the Review gauge). Falls back to the row's stored values on any
    error, so a display never crashes on a scoring hiccup."""
    from core.pipeline import prob_tier
    try:
        from core.auto_scorer import auto_score
        from core.policies import get_policies
        res = auto_score(dict(row), policies or get_policies())
        score = res.get("alignment_score")
        out: dict[str, Any] = {
            "alignment_score": score,
            "auto_recommendation": res.get("auto_recommendation"),
            "probability": prob_tier(score, short=True),
        }
        for k in CRITERIA:
            if res.get(k) is not None:
                out[k] = res.get(k)
        return out
    except Exception:
        s = row.get("alignment_score")
        return {
            "alignment_score": s,
            "auto_recommendation": row.get("auto_recommendation"),
            "probability": prob_tier(s, short=True),
        }
