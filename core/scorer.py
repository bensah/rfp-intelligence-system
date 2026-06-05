"""Eligibility scoring engine.

Mirrors the Excel `Criteria_Reference` sheet. Each of the nine MUST/PREFER
criteria scores as Yes=1.0, Partial=0.5, No=0.0, None=0.0 (unscored).
`alignment_score` = sum(weight_i * value_i) * 100, clamped to [0, 100].
`auto_recommendation` follows the decline-flag override, then thresholds.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

CRITERIA = (
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

_VALUE_MAP = {
    "yes": 1.0, "y": 1.0, "true": 1.0, "1": 1.0,
    "partial": 0.5, "p": 0.5,
    "no": 0.0, "n": 0.0, "false": 0.0, "0": 0.0,
}


@lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "config" / "scoring_weights.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _value(v: Any) -> float:
    if v is None:
        return 0.0
    return _VALUE_MAP.get(str(v).strip().lower(), 0.0)


def alignment_score(values: Mapping[str, Any]) -> float:
    """Compute alignment score 0-100 from a mapping keyed by the 9 criteria."""
    cfg = _load_config()
    total = 0.0
    weight_sum = 0.0
    for c in CRITERIA:
        w = float(cfg.get(c, 0.0))
        weight_sum += w
        total += w * _value(values.get(c))
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
