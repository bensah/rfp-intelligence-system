"""Per-criterion confidence / factor transparency — DISPLAY-ONLY, non-breaking.

Shows HOW strong each 2/1/0 criterion score is, as a fraction of the factors that
"won" (matched org × donor × RFP):
  * MUST-5 (cofinancing & compliance) is genuinely multi-factor → the REAL
    won/total compliance checks (shared with `derive_cofinancing`, so the label
    and the count never drift).
  * Every other criterion → its score as a fraction of the 2-point max, so the %
    always CORRELATES with the 2/1/0 scale (2 → 100%, 1 → 50%, 0/Not sure → 0%).

This does NOT change any score or decision — it only annotates the Review grid
("Yes, comfortably · 4/5 · 80%").
"""
from __future__ import annotations

from typing import Any

from core.scorer import criterion_score


def confidence(key: str, label: Any, org: dict | None = None,
               rfp: dict | None = None, donor: dict | None = None) -> tuple[int, int, int]:
    """Return (won, total, pct). pct always correlates with the 2/1/0 score."""
    if key == "cofinancing":
        # MUST-5 = Σ component scores ÷ activated components (soft cost-share/prefinance
        # contribute 0.5; hard gates 0/1). den is always ≥1 (permissive defaults).
        try:
            from core.criteria_derive import cofinancing_bid_strength
            num, den = cofinancing_bid_strength(org or {}, rfp or {}, donor)
            if den:
                return round(num, 1), den, round(num / den * 100)
        except Exception:
            pass
    if key == "qualification":
        # MUST-1 = the score-based ratio (Σ item scores ÷ activated items), NOT the
        # benefit-of-doubt won/total. den 0 → nothing imposed → fall through to label.
        try:
            from core.criteria_derive import qualification_bid_strength
            num, den = qualification_bid_strength(org or {}, rfp or {}, donor)
            if den:
                return num, den, round(num / den * 100)
        except Exception:
            pass
    if key == "strategic_fit":
        # MUST-2 = ONE component "Strategic priority fitness" scored 0/0.5/1 (best band).
        try:
            from core.criteria_derive import strategic_bid_strength
            matched, total, best = strategic_bid_strength(org or {}, rfp or {}, donor)
            if total:
                return best, 1, round(best * 100)
        except Exception:
            pass
    if key == "capacity":
        # MUST-3 = Σ component scores ÷ activated components.
        try:
            from core.criteria_derive import capacity_bid_strength
            num, den = capacity_bid_strength(org or {}, rfp or {}, donor)
            if den:
                return num, den, round(num / den * 100)
        except Exception:
            pass
    if key == "geographic_fit":
        # MUST-4 = ONE tiered component (own=1 · via partner=0.5 · none=0).
        try:
            from core.criteria_derive import geographic_bid_strength
            sc, _ = geographic_bid_strength(org or {}, rfp or {}, None, donor)
            return sc, 1, round(sc * 100)
        except Exception:
            pass
    sc = criterion_score(label)
    if sc is None:                          # Not sure / undetermined → Park midpoint
        return 1, 2, 50
    sc = int(sc)
    return sc, 2, round(sc / 2 * 100)


def badge(key: str, label: Any, org: dict | None = None,
          rfp: dict | None = None, donor: dict | None = None) -> str:
    """Compact 'won/total · pct%' string for inline display."""
    won, total, pct = confidence(key, label, org, rfp, donor)
    return f"{won}/{total} · {pct}%"
