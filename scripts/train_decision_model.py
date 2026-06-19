"""Train the decision model (ML Phase 3) — predict the human Proceed/Park/Decline
from the features captured in scan_decisions.

Trains with scikit-learn (class-weighted multinomial LogisticRegression + L2,
stratified k-fold CV). The fitted scaler + coefficients are exported as a compact
JSON blob in app_settings.decision_model so SERVING stays pure-python on Cloud
(core.decision_model.predict — no sklearn at request time).

  python scripts/train_decision_model.py            # train + report (no write)
  python scripts/train_decision_model.py --commit   # also persist (SHADOW: active=False)
  python scripts/train_decision_model.py --activate  # persist AND surface (active=True)

Assistive only. Target = the human decision; `auto_recommendation` is excluded
(it's a function of the criteria — training on it would echo the rule).
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score,
                             recall_score)
from sklearn.model_selection import StratifiedKFold

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.decision_model import CLASSES, FEATURE_NAMES, raw_vector  # noqa: E402
from core.settings import set_setting                                # noqa: E402
from db.supabase_client import get_client                            # noqa: E402

MIN_TOTAL = 45          # cold-start: need at least this many labels …
MIN_PER_CLASS = 10      # … and this many of EACH class
_C = 1.0                # inverse L2 strength
_CLS_IDX = {c: i for i, c in enumerate(CLASSES)}


def _impute_scale_fit(X):
    """NaN→column mean, then standardize. Returns (means, stds) — the SAME params
    serving uses (core.decision_model.predict)."""
    means = np.nanmean(X, axis=0)
    means = np.where(np.isnan(means), 0.0, means)
    Xi = np.where(np.isnan(X), means, X)
    stds = Xi.std(axis=0)
    stds = np.where(stds < 1e-8, 1.0, stds)
    return means, stds


def _apply(X, means, stds):
    return (np.where(np.isnan(X), means, X) - means) / stds


def _fit(Xs, y):
    return LogisticRegression(
        C=_C, class_weight="balanced", max_iter=4000, solver="lbfgs",
    ).fit(Xs, y)


def main(commit: bool, activate: bool) -> int:
    sb = get_client()
    rows = (sb.table("scan_decisions").select("label, features")
            .eq("event_type", "human_decision").execute().data or [])
    X, y = [], []
    for r in rows:
        lab = (r.get("label") or "").strip().title()
        if lab in _CLS_IDX and r.get("features"):
            X.append(raw_vector(r["features"]))
            y.append(_CLS_IDX[lab])
    X = np.array(X, float); y = np.array(y, int)
    counts = Counter(CLASSES[c] for c in y.tolist())
    print(f"labeled decisions usable: {len(y)}  {dict(counts)}")

    if len(y) < MIN_TOTAL or any(counts.get(c, 0) < MIN_PER_CLASS for c in CLASSES):
        print(f"\nCold-start gate NOT met (need ≥{MIN_TOTAL} total and "
              f"≥{MIN_PER_CLASS} per class). Keep collecting decisions — no model trained.")
        return 0

    k = len(CLASSES)
    labels = list(range(k))

    # Stratified 5-fold CV (each fold refits scaler + model on its train split).
    yp = np.empty_like(y)
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    for tr, te in skf.split(X, y):
        m, s = _impute_scale_fit(X[tr])
        clf = _fit(_apply(X[tr], m, s), y[tr])
        yp[te] = clf.predict(_apply(X[te], m, s))
    acc = accuracy_score(y, yp)
    macro_f1 = f1_score(y, yp, labels=labels, average="macro", zero_division=0)
    recalls = recall_score(y, yp, labels=labels, average=None, zero_division=0)
    cm = confusion_matrix(y, yp, labels=labels)
    base = max(counts.values()) / len(y)
    cv = {"accuracy": round(float(acc), 3), "macro_f1": round(float(macro_f1), 3),
          "per_class_recall": {CLASSES[c]: round(float(recalls[c]), 3) for c in range(k)},
          "confusion": cm.tolist()}
    print(f"\nCV (5-fold): accuracy {cv['accuracy']}  macro-F1 {cv['macro_f1']}  "
          f"(majority baseline {base:.3f})")
    print(f"  per-class recall: {cv['per_class_recall']}")
    print(f"  confusion [rows=true {list(CLASSES)}]: {cv['confusion']}")

    # Final fit on ALL data → stored model. Export scaler params + coefficients
    # as W = [coef | intercept] (K x d+1) for the pure-python serving path.
    means, stds = _impute_scale_fit(X)
    clf = _fit(_apply(X, means, stds), y)
    # sklearn drops the per-class row for a binary problem; here k=3 so coef_ is k x d.
    W = np.hstack([clf.coef_, clf.intercept_.reshape(-1, 1)])
    model = {
        "model_type": "sklearn_logreg_multinomial",
        "sklearn_C": _C,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n": int(len(y)),
        "class_counts": {c: int(counts.get(c, 0)) for c in CLASSES},
        "classes": list(CLASSES),
        "feature_names": list(FEATURE_NAMES),
        "means": means.tolist(),
        "stds": stds.tolist(),
        "W": W.tolist(),
        "metrics": cv,
        "baseline_accuracy": round(float(base), 3),
        "active": bool(activate),
    }
    if not commit and not activate:
        print("\nDRY RUN — re-run with --commit (shadow) or --activate (surface) to store.")
        return 0
    set_setting("decision_model", json.dumps(model), updated_by="train_decision_model")
    state = "ACTIVE (surfaced)" if activate else "SHADOW (active=False)"
    print(f"\nStored decision_model in app_settings — {state}. "
          f"beats baseline: {cv['accuracy'] > base}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(commit="--commit" in sys.argv, activate="--activate" in sys.argv))
