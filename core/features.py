"""Feature extraction for the decision model (ML Phase 3).

ONE place that turns a candidate / `rfp_submissions` row into the named feature
dict the decision model trains and predicts on. Captured into
`scan_decisions.features` (jsonb) at decision time so the model can train
WITHOUT re-crawling (migration 027's intent), and reconstructable from a stored
row for backfill.

Design choices (see the Phase 3 spec):
  * Target is the HUMAN decision (Decline < Park < Proceed). These are the
    inputs only.
  * `auto_recommendation` is deliberately EXCLUDED — it's a deterministic
    function of the criteria, so feeding it would teach the model to echo the
    rule instead of learning a better mapping.
  * Criterion responses are NORMALISED to an ordinal score (2 / 1 / 0, None for
    "Not sure" / unscored) via core.scorer.criterion_score, so legacy
    True/Partial/False AND the new MS-Form rich labels ("Yes, via a partner",
    "Strong - priorities + experience", "High", …) feed the model on ONE scale
    (Bernard 2026-06-17: Yes=2, Partial=1, No=0). Other features keep readable
    values (geo_strength='strong', days_to_deadline=45).

Best-effort: never raises into a scan or a UI save — returns whatever it could
compute, with missing pieces left as None.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any

from core.scorer import criterion_score

log = logging.getLogger(__name__)

# The model's parameter order (stable). The trainer reads this; don't reorder
# without bumping the stored model's feature_order.
_CRITERION_FEATURES = (
    "qualification", "strategic_fit", "capacity",
    "geographic_fit", "cofinancing",
    "funding_quality", "funder_relationship", "competitiveness",
    "bid_effort",
)
FEATURE_ORDER: tuple[str, ...] = _CRITERION_FEATURES + (
    "alignment_score", "geo_strength", "has_deadline", "days_to_deadline",
    "decline_flags_present", "funder_is_usg", "log_value_usd", "channel",
    "text_len",
)

def _parse_date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _safe_geo_strength(row: dict, policies: dict | None) -> str | None:
    try:
        from core.auto_scorer import _geo_strength
        return _geo_strength(row, policies or {})
    except Exception:
        return None


def _funder_is_usg(funder: str | None, policies: dict | None) -> bool | None:
    if not funder:
        return None
    try:
        rules = (policies or {}).get("scoring_rules", {}) or {}
        usg = rules.get("usg_funders") or {}
        pats = [p.lower() for p in (usg.get("patterns") or []) if p]
        fl = funder.lower()
        return any(p in fl for p in pats)
    except Exception:
        return None


def _log_value_usd(value: Any, currency: Any) -> float | None:
    try:
        v = float(str(value).replace(",", "").replace("$", "")) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        from core.dropdowns import usd_rate
        usd = v * float(usd_rate(currency or "USD"))
    except Exception:
        usd = v
    return round(math.log1p(max(0.0, usd)), 4)


def _channel(row: dict) -> str:
    origin = (row.get("_source_origin") or row.get("source") or "").lower()
    link = (row.get("opportunity_link") or "").lower()
    if "grants.gov" in origin or "grants.gov" in link or "(kw=" in origin:
        return "grants.gov"
    if row.get("_resolved_from_aggregator") or row.get("_aggregator_link"):
        return "aggregator-resolved"
    if "rss" in origin or link.endswith((".xml", ".rss")):
        return "rss"
    return "web"


def extract(row: dict, policies: dict | None = None, *,
            asof: date | None = None) -> dict[str, Any]:
    """Build the named feature dict for a candidate / rfp_submissions row.

    `asof` dates the deadline distance (default today; backfill passes the
    decision's own date). `policies` is loaded lazily if omitted."""
    if not isinstance(row, dict):
        return {}
    if policies is None:
        try:
            from core.policies import get_policies
            policies = get_policies()
        except Exception:
            policies = {}
    if asof is None:
        asof = date.today()

    feats: dict[str, Any] = {}
    for k in _CRITERION_FEATURES:
        feats[k] = criterion_score(row.get(k))   # 2 / 1 / 0 / None

    sc = row.get("alignment_score")
    try:
        feats["alignment_score"] = float(sc) if sc not in (None, "") else None
    except (TypeError, ValueError):
        feats["alignment_score"] = None

    feats["geo_strength"] = _safe_geo_strength(row, policies)

    dl = _parse_date(row.get("submission_deadline"))
    feats["has_deadline"] = bool(dl)
    feats["days_to_deadline"] = (dl - asof).days if dl else None

    df = row.get("decline_flags_present")
    feats["decline_flags_present"] = bool(df) if df is not None else None

    feats["funder_is_usg"] = _funder_is_usg(row.get("funding_agency"), policies)
    feats["log_value_usd"] = _log_value_usd(
        row.get("estimated_value"), row.get("currency"))
    feats["channel"] = _channel(row)
    feats["text_len"] = len((row.get("brief_description") or "").strip())
    return feats
