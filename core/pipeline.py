"""Shared pipeline-status helpers.

Deadline-status thresholds match the original Excel:
    Overdue          deadline < today
    Due Soon         today <= deadline <= today + 14
    On Track         deadline > today + 14

Probability-tier thresholds (single source of truth — used by Data page,
Highlights, and any future consumer):
    High        alignment_score >  90
    Medium      70 <= alignment_score <= 90
    Low         alignment_score <  70
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from core import dropdowns


DUE_SOON_DAYS = 14

# Probability tier thresholds
PROB_HIGH_MIN = 90   # > 90 → High
PROB_MED_MIN = 70    # >= 70 → Medium; < 70 → Low
PROB_LABEL_HIGH = "High (>90%)"
PROB_LABEL_MED = "Medium (70-90%)"
PROB_LABEL_LOW = "Low (<70%)"


def prob_tier(score: Any, short: bool = False) -> str | None:
    """Return the probability tier label for a given alignment_score.

    Returns None for null/non-numeric scores. When `short=True`, returns
    "High" / "Medium" / "Low" without the percentage band.
    """
    if score is None:
        return None
    try:
        s = float(score)
        if pd.isna(s):
            return None
    except (TypeError, ValueError):
        return None
    if s > PROB_HIGH_MIN:
        return "High" if short else PROB_LABEL_HIGH
    if s >= PROB_MED_MIN:
        return "Medium" if short else PROB_LABEL_MED
    return "Low" if short else PROB_LABEL_LOW


def deadline_status(d: Any, submitted: bool = False,
                    decision: str | None = None) -> str | None:
    """Deadline chip for a call/grant.

    Once a proposal has been SUBMITTED, a passed deadline is no longer "Overdue" — the
    window closing is expected. Instead we report the state-accurate outcome:
      * decision Approved  → "Awarded"
      * decision Not Approved → "Not approved"
      * otherwise (awaiting a decision) → "Submitted"
    Not-yet-submitted calls keep the discovery semantics (Overdue / Due Soon / On Track)."""
    if d is None or (isinstance(d, float) and pd.isna(d)):
        return None
    try:
        dt = pd.to_datetime(d, errors="coerce")
        if pd.isna(dt):
            return None
        deadline = dt.date()
    except Exception:
        return None
    today = date.today()
    if deadline < today:
        if submitted:
            dec = (decision or "").strip().lower()
            if dec == "approved":
                return "Awarded"
            if dec == "not approved":
                return "Not approved"
            return "Submitted"
        return "Overdue"
    if deadline <= today + timedelta(days=DUE_SOON_DAYS):
        return "Due Soon"
    return "On Track"


def days_to_deadline(d: Any) -> int | None:
    if d is None or (isinstance(d, float) and pd.isna(d)):
        return None
    try:
        dt = pd.to_datetime(d, errors="coerce")
        if pd.isna(dt):
            return None
        return (dt.date() - date.today()).days
    except Exception:
        return None


def usd_value(estimated: Any, currency: str | None) -> float:
    if estimated is None or (isinstance(estimated, float) and pd.isna(estimated)):
        return 0.0
    try:
        return float(estimated) * dropdowns.usd_rate(currency)
    except (TypeError, ValueError):
        return 0.0
