"""Decision model (ML Phase 3) — encode features + serve predictions.

A small multinomial-logistic model predicts the HUMAN decision
(Decline / Park / Proceed) for an RFP that passed the hard gate, from the same
features captured in scan_decisions (core.features.FEATURE_ORDER). Trained
offline by scripts/train_decision_model.py (numpy-only — no sklearn) and stored
as a compact JSON blob in app_settings.decision_model:

    {model_type, trained_at, n, classes, feature_names, means, stds, W,
     metrics, active}

Serving is pure numpy (numpy is already a dependency), so it runs on Streamlit
Cloud. ASSISTIVE only — a prediction never auto-applies; `active` gates whether
the UI surfaces it (False = shadow mode while we validate).

Encoding is shared between train + serve here so they never drift.
"""
from __future__ import annotations

import json
import math
from typing import Any

# The 9 criteria (numeric 2/1/0/None) + context features (see core.features).
_CRITERIA = (
    "qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing",
    "funding_quality", "funder_relationship", "competitiveness", "bid_effort",
)
_GEO_ORDINAL = {"strong": 1.0, "regional": 0.6, "silent": 0.4, "foreign": 0.0}
_CHANNELS = ("grants.gov", "web", "aggregator-resolved", "rss")

# Expanded feature-vector layout (order is the model's contract).
FEATURE_NAMES: tuple[str, ...] = _CRITERIA + (
    "alignment_score", "geo_strength", "has_deadline", "days_to_deadline",
    "decline_flags_present", "funder_is_usg", "log_value_usd", "text_len",
) + tuple(f"channel={c}" for c in _CHANNELS)

CLASSES = ("Decline", "Park", "Proceed")
_MODEL_KEY = "decision_model"


def _f(v) -> float:
    """float or NaN (missing)."""
    if v is None or v == "":
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def raw_vector(features: dict | None) -> list[float]:
    """Encode a features dict (scan_decisions.features) into the raw numeric
    vector in FEATURE_NAMES order. Missing values are NaN (imputed at train/serve
    time from the stored column means)."""
    f = features or {}
    vec: list[float] = []
    for k in _CRITERIA:
        vec.append(_f(f.get(k)))                              # 2 / 1 / 0 / NaN
    vec.append(_f(f.get("alignment_score")))
    gs = f.get("geo_strength")
    vec.append(_GEO_ORDINAL.get(str(gs).lower(), float("nan")) if gs else float("nan"))
    vec.append(1.0 if f.get("has_deadline") else (float("nan") if f.get("has_deadline") is None else 0.0))
    vec.append(_f(f.get("days_to_deadline")))
    vec.append(1.0 if f.get("decline_flags_present") else (float("nan") if f.get("decline_flags_present") is None else 0.0))
    vec.append(1.0 if f.get("funder_is_usg") else (float("nan") if f.get("funder_is_usg") is None else 0.0))
    vec.append(_f(f.get("log_value_usd")))
    vec.append(_f(f.get("text_len")))
    chan = str(f.get("channel") or "").lower()
    for c in _CHANNELS:
        vec.append(1.0 if chan == c else 0.0)
    return vec


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------
def load_model() -> dict | None:
    try:
        from core.settings import get_setting
        raw = get_setting(_MODEL_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _softmax(z: list[float]) -> list[float]:
    m = max(z)
    e = [math.exp(v - m) for v in z]
    s = sum(e) or 1.0
    return [v / s for v in e]


def predict(features: dict, model: dict | None = None) -> dict | None:
    """Return {decision, proba: {class: p}, confidence, drivers, active} or None
    when no model is stored / it can't score. Pure python (no numpy needed)."""
    model = model or load_model()
    if not model or not model.get("W"):
        return None
    try:
        names = model["feature_names"]
        means = model["means"]
        stds = model["stds"]
        W = model["W"]                       # K x (d+1), last col = bias
        classes = model.get("classes", list(CLASSES))
        raw = dict(zip(FEATURE_NAMES, raw_vector(features)))
        # standardize (impute NaN -> mean -> 0 after centering)
        x = []
        for i, n in enumerate(names):
            v = raw.get(n, float("nan"))
            if v != v:                       # NaN
                v = means[i]
            s = stds[i] or 1.0
            x.append((v - means[i]) / s)
        x.append(1.0)                        # bias
        z = [sum(wj * xj for wj, xj in zip(row, x)) for row in W]
        p = _softmax(z)
        order = sorted(range(len(classes)), key=lambda i: p[i], reverse=True)
        top = order[0]
        # top drivers: standardized contribution to the winning class
        contrib = sorted(
            ((names[i], W[top][i] * x[i]) for i in range(len(names))),
            key=lambda kv: abs(kv[1]), reverse=True)[:4]
        return {
            "decision": classes[top],
            "proba": {classes[i]: round(p[i], 3) for i in range(len(classes))},
            "confidence": round(p[top], 3),
            "drivers": [{"feature": n, "weight": round(w, 3)} for n, w in contrib],
            "active": bool(model.get("active", False)),
            "trained_at": model.get("trained_at"),
            "n": model.get("n"),
        }
    except Exception:
        return None
