"""Eligibility scoring engine.

Mirrors the Excel `Criteria_Reference` sheet. Each of the nine MUST/PREFER
criteria scores as Yes=1.0, Partial=0.5, No=0.0, None=0.0 (unscored).
`alignment_score` = sum(weight_i * value_i) * 100, clamped to [0, 100].
`auto_recommendation` follows the decline-flag override, then thresholds.
"""
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

CRITERIA = (
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

import re

# Canonical normalization for a criterion response → ordinal score 2/1/0 (None
# when unscored / "Not sure"). ONE place, used by the scorer, the decision tree
# (core.auto_scorer) and the model features (core.features) so old stored values
# (True/Partial/False, Yes/No) and the NEW MS-Form rich labels ("Yes, via a
# partner", "Strong - priorities + experience", "High", …) all normalise to the
# same scale (Bernard 2026-06-17: convert Yes=2, Partial=1, No=0).
_SCORE_MAP = {
    # legacy / canonical
    "yes": 2, "y": 2, "true": 2, "1": 2,
    "partial": 1, "p": 1, "partly": 1,
    "no": 0, "n": 0, "false": 0, "0": 0,
    # qualification
    "yes, fully": 2, "mostly, one item unclear": 1, "no, not eligible": 0,
    # strategic_fit — NEW labels (priorities-vs-donor-priorities) + legacy (kept so
    # old stored rows still score and map to the new label by score via match_response)
    "strongly aligns": 2, "limited priority": 1, "off-strategy": 0, "off-theme": 0,
    "strong - priorities + experience": 2, "priority area, limited experience": 1,
    "experienced but off-strategy": 1, "neither": 0,
    # capacity
    "yes, comfortably": 2, "yes, but a stretch": 1, "no, beyond us": 0,
    # geographic_fit
    "yes, our own presence": 2, "yes, via a partner": 1, "no presence there": 0,
    # cofinancing — MUST-5 now spans co-financing AND the funder's compliance gates
    # (SAM/tax-exempt/safeguarding/…), so the labels read Met / Not Met: fully met = 2,
    # partial with effort = 1, not met = 0. OLD labels kept as aliases so existing
    # stored rows still score and remap to the new label by score (match_response).
    "yes, fully met": 2, "partial, with effort": 1, "not met": 0,
    "yes, none required": 2, "no, required": 0, "yes / none required": 2,
    # funding_quality
    "high": 2, "moderate": 1, "low": 0,
    # funder_relationship
    "current/past grantee": 2, "some contact": 1, "none": 0,
    # competitiveness
    "strong (limited field / incumbent / clear edge)": 2, "weak (wide-open)": 0,
    # bid_effort — 5-point (time × BD-team), collapsed to 2/1/0 for the rule
    # engine (the finer ordinal lives in the label + days_to_deadline feature):
    #   4 Ample+team / 3 Ample-no-team / 3 Tight+team        -> 2
    #   2 Tight-no-team                                       -> 1
    #   1 NotEnough+team / 0 NotEnough-no-team                -> 0  (time dominates)
    "ample time, sufficient resources": 2,
    "ample time, but no dedicated team": 2,
    "tight but doable, with a team": 2,
    "tight, and no dedicated team": 1,
    "tight but doable, insufficient resources": 1,   # legacy 3-point label
    "not enough time, even with a team": 0,
    "not enough time, no team": 0,
    "not enough": 0,                                  # legacy 3-point label
}
# Conservative fallback for minor MS-Form wording drift. First match wins; an
# unrecognised response stays None (treated as missing — never a guess).
_FALLBACK = (
    ("strong", 2), ("high", 2), ("ample", 2), ("comfortab", 2),
    ("moderate", 1), ("tight", 1), ("partial", 1), ("stretch", 1),
    ("limited experience", 1), ("via a partner", 1), ("with effort", 1),
    ("priority area", 1),
    ("low", 0), ("weak", 0), ("neither", 0), ("not enough", 0),
    ("no presence", 0), ("beyond", 0), ("not eligible", 0),
)


def criterion_score(value: Any) -> int | None:
    """Normalise any criterion response → 2 / 1 / 0, or None (unscored / Not sure)."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).strip().lower()).replace("–", "-").replace("—", "-")
    if not s or "not sure" in s:
        return None
    if s in _SCORE_MAP:
        return _SCORE_MAP[s]
    for kw, sc in _FALLBACK:
        if s.startswith(kw) or kw in s:
            return sc
    return None


_SCORE_TO_FLOAT = {2: 1.0, 1: 0.5, 0: 0.0}


# --- Bid effort (PREFER 9) — objective derivation ----------------------------
# A 5-point ordinal from TIME-to-deadline × RESOURCES (a Business-Development /
# fundraising / resource-mobilization team = "sufficient"). Time-dominant +
# monotone: a <7-day deadline caps the score low whether or not there's a team
# (it stresses the team either way). Tunable thresholds.
BID_EFFORT_AMPLE_DAYS = 14   # > this  -> Ample time
BID_EFFORT_TIGHT_DAYS = 7    # >= this -> Tight but doable; below -> Not enough


def days_until(deadline, *, today: date | None = None) -> int | None:
    """Whole days from today to a deadline (date / datetime / ISO string).
    None if missing or unparseable."""
    if deadline is None:
        return None
    if isinstance(deadline, datetime):
        d = deadline.date()
    elif isinstance(deadline, date):
        d = deadline
    else:
        try:
            d = datetime.strptime(str(deadline)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    return (d - (today or date.today())).days


def bid_effort_label(days_to_deadline: int | None, has_bd_team: bool) -> str | None:
    """Map (days-to-deadline, has-BD-team) → one of the 5 canonical bid-effort
    labels (None when the deadline is unknown — can't derive). Scores 4..0:
        4 Ample+team · 3 Ample-no-team · 3 Tight+team ·
        2 Tight-no-team · 1 NotEnough+team · 0 NotEnough-no-team."""
    if days_to_deadline is None:
        return None
    suff = bool(has_bd_team)
    if days_to_deadline > BID_EFFORT_AMPLE_DAYS:
        return ("Ample time, sufficient resources" if suff
                else "Ample time, but no dedicated team")
    if days_to_deadline >= BID_EFFORT_TIGHT_DAYS:
        return ("Tight but doable, with a team" if suff
                else "Tight, and no dedicated team")
    return ("Not enough time, even with a team" if suff
            else "Not enough time, no team")


# Canonical per-criterion response options (best → worst), reused by every form
# (Review, Submit, MS Form). Every label is recognised by criterion_score above.
# "Not sure" → null (missing) for the human-answered eight; bid_effort is
# auto-derived on a 5/6-point time×resources scale (no "Not sure").
CRITERION_RESPONSES: dict[str, list[str]] = {
    "qualification": [
        "Yes, fully", "Mostly, one item unclear", "No, not eligible", "Not sure"],
    "strategic_fit": [
        "Strongly aligns", "Limited priority", "Off-strategy", "Not sure"],
    "capacity": [
        "Yes, comfortably", "Yes, but a stretch", "No, beyond us", "Not sure"],
    "geographic_fit": [
        "Yes, our own presence", "Yes, via a partner", "No presence there", "Not sure"],
    "cofinancing": [
        "Yes, fully met", "Partial, with effort", "Not met", "Not sure"],
    "funding_quality": ["High", "Moderate", "Low", "Not sure"],
    "funder_relationship": [
        "Current/past grantee", "Some contact", "None", "Not sure"],
    "competitiveness": [
        "Strong (limited field / incumbent / clear edge)", "Moderate",
        "Weak (wide-open)", "Not sure"],
    "bid_effort": [
        "Ample time, sufficient resources", "Ample time, but no dedicated team",
        "Tight but doable, with a team", "Tight, and no dedicated team",
        "Not enough time, even with a team", "Not enough time, no team"],
}


def default_response(key: str, stored) -> str:
    """Best matching response option for a criterion given a stored value —
    handles legacy True/Partial/False (maps by score) and already-new labels.
    Used to pre-select dropdowns without losing existing data."""
    opts = CRITERION_RESPONSES.get(key) or []
    if not opts:
        return ""
    if stored is not None and str(stored) in opts:
        return str(stored)
    sc = criterion_score(stored)
    if sc is None:
        return "Not sure" if "Not sure" in opts else opts[-1]
    for o in opts:
        if criterion_score(o) == sc:
            return o
    return opts[0]


@lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "config" / "scoring_weights.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _value(v: Any) -> float:
    return _SCORE_TO_FLOAT.get(criterion_score(v), 0.0)


# "Not sure" / undetermined criterion → value 1 (Park), NOT 0 (owner 2026-06-29).
# When a criterion has no detectable components (nothing the call/donor imposes and
# no proxy), it resolves to "Not sure" and contributes the MIDDLE value so the bid
# routes to Park (manual review) rather than being penalised as a hard fail. Genuine
# detected failures still score 0 and gate via the MUST rule (criterion_score == 0).
_NOT_SURE_FLOAT = 0.5


def alignment_score(values: Mapping[str, Any]) -> float:
    """Compute alignment score 0-100 from a mapping keyed by the 9 criteria.

    ALL 9 criteria count toward the denominator. A response of "Not sure" / unscored
    (criterion_score → None) is rated value 1 (0.5) — the Park midpoint — so an
    UNKNOWN criterion reads as 'needs review', not a hard fail. A clearly DETECTED
    failure scores 0 and still gates (MUST rule)."""
    cfg = _load_config()
    total = 0.0
    weight_sum = 0.0
    for c in CRITERIA:
        sc = criterion_score(values.get(c))      # None → "Not sure" → value 1 (0.5)
        w = float(cfg.get(c, 0.0))
        weight_sum += w
        total += w * (_SCORE_TO_FLOAT[sc] if sc is not None else _NOT_SURE_FLOAT)
    if weight_sum <= 0:
        return 0.0
    score = (total / weight_sum) * 100.0
    return max(0.0, min(100.0, round(score, 1)))


def auto_recommendation(score: float, decline_flags_present: bool) -> str:
    """Decline-flag override beats score; thresholds from scoring_weights.yaml."""
    if decline_flags_present:
        return "Decline"
    th = _load_config().get("thresholds", {}) or {}
    proceed = float(th.get("proceed", 70))
    park = float(th.get("park", 45))
    if score >= proceed:
        return "Proceed"
    if score >= park:
        return "Park"
    return "Decline"


def score_submission(
    values: Mapping[str, Any], decline_flags_present: bool
) -> tuple[float, str]:
    """One-shot convenience: returns (alignment_score, auto_recommendation)."""
    s = alignment_score(values)
    return s, auto_recommendation(s, decline_flags_present)
