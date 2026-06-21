"""Train the decision model (ML Phase 3 / Workstream B6) — predict the human
Proceed/Park/Decline from the features captured in scan_decisions.

MODEL CHOICE (see docs/DECISION_MODEL_REPORT.md + scripts/eval_decision_model.py):
class-weighted multinomial LogisticRegression (L2), C tuned by CV. KNN scored a
hair higher in nested CV (0.77 vs 0.72 macro-F1) but the gap is inside one fold's
std at n≈63 (tied), KNN's probabilities are badly calibrated (CV log-loss worse
than uniform), it can't serve through the pure-python path, and instance-based
methods degrade hardest under the train(migration)/serve(auto) distribution shift
this dataset has. LogReg is portable, calibrated, interpretable, and clears the
rule baseline — the right assistive serving model.

The fitted scaler + coefficients are exported as a compact JSON blob in
app_settings.decision_model so SERVING stays pure-python on Cloud
(core.decision_model.predict — no sklearn at request time).

  python scripts/train_decision_model.py            # train + honest report (no write)
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
from sklearn.metrics import (balanced_accuracy_score, confusion_matrix,
                             f1_score, recall_score)
from sklearn.model_selection import GridSearchCV, StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _dataset import load                                          # noqa: E402
from core.decision_model import CLASSES, FEATURE_NAMES            # noqa: E402
from core.settings import set_setting                              # noqa: E402

MIN_TOTAL = 45          # cold-start: need at least this many labels …
MIN_PER_CLASS = 10      # … and this many of EACH class
_C_GRID = [0.05, 0.1, 0.25, 0.5, 1.0]   # L2 inverse strength, tuned by CV


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


def _fit(Xs, y, C):
    return LogisticRegression(
        C=C, class_weight="balanced", max_iter=5000, solver="lbfgs").fit(Xs, y)


def _tune_C(X, y):
    """Pick C by inner CV (f1_macro) on a leak-safe pipeline."""
    pipe = Pipeline([("impute", SimpleImputer(strategy="mean")),
                     ("scale", StandardScaler()),
                     ("clf", LogisticRegression(class_weight="balanced",
                                                max_iter=5000, solver="lbfgs"))])
    gs = GridSearchCV(pipe, {"clf__C": _C_GRID}, scoring="f1_macro",
                      cv=StratifiedKFold(5, shuffle=True, random_state=42),
                      n_jobs=-1).fit(X, y)
    return gs.best_params_["clf__C"]


def main(commit: bool, activate: bool) -> int:
    ds = load()
    X, y = ds.X, ds.y
    counts = Counter(CLASSES[c] for c in y.tolist())
    print(f"labeled decisions usable: {len(y)}  {dict(counts)}")

    if len(y) < MIN_TOTAL or any(counts.get(c, 0) < MIN_PER_CLASS for c in CLASSES):
        print(f"\nCold-start gate NOT met (need ≥{MIN_TOTAL} total and "
              f"≥{MIN_PER_CLASS} per class). Keep collecting decisions — no model trained.")
        return 0

    k = len(CLASSES)
    labels = list(range(k))
    C = _tune_C(X, y)
    print(f"tuned C (CV f1_macro over {_C_GRID}): {C}")

    # Nested-style honest CV: each fold refits scaler + model on its train split.
    yp = np.empty_like(y)
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    for tr, te in skf.split(X, y):
        m, s = _impute_scale_fit(X[tr])
        clf = _fit(_apply(X[tr], m, s), y[tr], C)
        yp[te] = clf.predict(_apply(X[te], m, s))
    acc = (yp == y).mean()
    macro_f1 = f1_score(y, yp, labels=labels, average="macro", zero_division=0)
    bal = balanced_accuracy_score(y, yp)
    recalls = recall_score(y, yp, labels=labels, average=None, zero_division=0)
    cm = confusion_matrix(y, yp, labels=labels)
    base = max(counts.values()) / len(y)

    # Rule baseline (auto_recommendation vs human) — the bar to clear.
    rule_idx = [i for i, mta in enumerate(ds.meta) if mta["auto_rec"] in CLASSES]
    rule_f1 = None
    if rule_idx:
        yr = y[rule_idx]
        yrp = np.array([CLASSES.index(ds.meta[i]["auto_rec"]) for i in rule_idx])
        rule_f1 = float(f1_score(yr, yrp, average="macro", zero_division=0))

    cv = {"accuracy": round(float(acc), 3), "macro_f1": round(float(macro_f1), 3),
          "balanced_accuracy": round(float(bal), 3),
          "per_class_recall": {CLASSES[c]: round(float(recalls[c]), 3) for c in range(k)},
          "confusion": cm.tolist(),
          "majority_baseline_acc": round(float(base), 3),
          "rule_macro_f1": round(rule_f1, 3) if rule_f1 is not None else None,
          "tuned_C": C}
    print(f"\nCV (5-fold): macro-F1 {cv['macro_f1']}  balanced-acc "
          f"{cv['balanced_accuracy']}  acc {cv['accuracy']}")
    print(f"  baselines — majority acc {cv['majority_baseline_acc']}"
          + (f", rule macro-F1 {cv['rule_macro_f1']}" if rule_f1 is not None else ""))
    print(f"  per-class recall: {cv['per_class_recall']}")
    print(f"  confusion [rows=true {list(CLASSES)}]: {cv['confusion']}")
    beats = rule_f1 is None or macro_f1 > rule_f1
    print(f"\nBeats rule baseline (macro-F1): {beats}"
          + ("" if beats else "  → DON'T --activate yet; collect more labels."))

    # Final fit on ALL data → stored model. Export scaler params + coefficients
    # as W = [coef | intercept] (K x d+1) for the pure-python serving path.
    means, stds = _impute_scale_fit(X)
    clf = _fit(_apply(X, means, stds), y, C)
    W = np.hstack([clf.coef_, clf.intercept_.reshape(-1, 1)])
    model = {
        "model_type": "sklearn_logreg_multinomial",
        "sklearn_C": C,
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
    print(f"\nStored decision_model in app_settings — {state}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(commit="--commit" in sys.argv, activate="--activate" in sys.argv))
