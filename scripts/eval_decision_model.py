"""Honest evaluation harness for the decision model (Workstream B2–B5).

READ-ONLY. Pulls the labeled dataset and runs a data-scientist-grade comparison,
reporting metrics with the small-n caveat. Writes NOTHING (no DB, no model).

  python scripts/eval_decision_model.py

What it does, in order:
  B3 baselines      — majority class + the rule (auto_recommendation) vs humans.
  B4 model compare  — LogReg / RandomForest / HistGBM / KNN, each tuned by
                      GridSearchCV INSIDE a nested StratifiedKFold (unbiased).
                      Metrics: macro-F1 (primary), per-class recall, balanced
                      accuracy, pooled confusion, per-fold macro-F1 mean±std.
  B3 time split     — train older / test newer (deployment simulation).
  B3 learning curve — macro-F1 vs training-set size.
  B3 calibration    — CV log-loss for the leading model (proxy; n too small to
                      fit a separate calibrator reliably).
  B2 text arm       — TF-IDF(title+description) → LogReg, fit INSIDE folds.
  B5 dim-reduction  — drop dead features / PCA / SelectKBest vs the full matrix.

Everything that touches X is fit inside CV folds (no leakage). auto_recommendation
is NEVER a feature (it's the rule we're trying to beat).
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np  # noqa: E402
from sklearn.decomposition import PCA  # noqa: E402
from sklearn.ensemble import (HistGradientBoostingClassifier,  # noqa: E402
                              RandomForestClassifier)
from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402
from sklearn.feature_selection import SelectKBest, f_classif  # noqa: E402
from sklearn.impute import SimpleImputer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import (balanced_accuracy_score,  # noqa: E402
                             confusion_matrix, f1_score, log_loss,
                             recall_score)
from sklearn.model_selection import (GridSearchCV,  # noqa: E402
                                     StratifiedKFold, learning_curve)
from sklearn.neighbors import KNeighborsClassifier  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from _dataset import load  # noqa: E402
from core.decision_model import CLASSES  # noqa: E402

warnings.filterwarnings("ignore")
RS = 42
K_OUTER, K_INNER = 5, 3


def _hr(t: str) -> None:
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def _pipe(clf, *, scale: bool) -> Pipeline:
    steps = [("impute", SimpleImputer(strategy="mean"))]
    if scale:
        steps.append(("scale", StandardScaler()))
    steps.append(("clf", clf))
    return Pipeline(steps)


# (name, pipeline, param grid, needs_scaling)
def _models() -> dict:
    return {
        "LogReg": (
            _pipe(LogisticRegression(class_weight="balanced", max_iter=5000,
                                     solver="lbfgs"), scale=True),
            {"clf__C": [0.05, 0.1, 0.25, 0.5, 1.0]}),
        "RandomForest": (
            _pipe(RandomForestClassifier(class_weight="balanced",
                                         random_state=RS), scale=False),
            {"clf__n_estimators": [200, 400],
             "clf__max_depth": [None, 3, 5],
             "clf__min_samples_leaf": [1, 2, 3]}),
        "HistGBM": (
            _pipe(HistGradientBoostingClassifier(class_weight="balanced",
                                                 random_state=RS), scale=False),
            {"clf__learning_rate": [0.05, 0.1],
             "clf__max_depth": [None, 3],
             "clf__max_iter": [100, 200]}),
        "KNN": (
            _pipe(KNeighborsClassifier(), scale=True),
            {"clf__n_neighbors": [3, 5, 7, 9],
             "clf__weights": ["uniform", "distance"]}),
    }


def _nested_cv(name, pipe, grid, X, y):
    """Outer StratifiedKFold for unbiased scoring; inner GridSearchCV for tuning.
    Returns pooled predictions + per-fold macro-F1s + the params picked per fold."""
    outer = StratifiedKFold(K_OUTER, shuffle=True, random_state=RS)
    yp = np.empty_like(y)
    fold_f1, picks = [], []
    for tr, te in outer.split(X, y):
        inner = StratifiedKFold(K_INNER, shuffle=True, random_state=RS)
        gs = GridSearchCV(pipe, grid, scoring="f1_macro", cv=inner, n_jobs=-1)
        gs.fit(X[tr], y[tr])
        pred = gs.predict(X[te])
        yp[te] = pred
        fold_f1.append(f1_score(y[te], pred, average="macro", zero_division=0))
        picks.append(gs.best_params_)
    return yp, np.array(fold_f1), picks


def _report(name, y, yp, fold_f1):
    labels = list(range(len(CLASSES)))
    mf1 = f1_score(y, yp, labels=labels, average="macro", zero_division=0)
    bal = balanced_accuracy_score(y, yp)
    rec = recall_score(y, yp, labels=labels, average=None, zero_division=0)
    cm = confusion_matrix(y, yp, labels=labels)
    print(f"\n{name}")
    print(f"  macro-F1 (pooled) {mf1:.3f}   balanced-acc {bal:.3f}   "
          f"per-fold macro-F1 {fold_f1.mean():.3f} ± {fold_f1.std():.3f}")
    print("  per-class recall: " + ", ".join(
        f"{CLASSES[i]} {rec[i]:.2f}" for i in labels))
    print(f"  confusion [rows=true {list(CLASSES)}]:\n{cm}")
    return mf1


def main() -> int:
    ds = load()
    X, y = ds.X, ds.y
    from collections import Counter
    counts = Counter(CLASSES[i] for i in y.tolist())
    n = len(y)

    # ---- B3 baselines -----------------------------------------------------
    _hr("B3 — BASELINES")
    base_maj = max(counts.values()) / n
    print(f"majority-class accuracy: {base_maj:.3f}  "
          f"(majority macro-F1: "
          f"{f1_score(y, np.full_like(y, np.bincount(y).argmax()), average='macro', zero_division=0):.3f})")
    rule_idx = [i for i, m in enumerate(ds.meta) if m["auto_rec"] in CLASSES]
    if rule_idx:
        yr_true = y[rule_idx]
        yr_pred = np.array([CLASSES.index(ds.meta[i]["auto_rec"]) for i in rule_idx])
        rule_f1 = f1_score(yr_true, yr_pred, average="macro", zero_division=0)
        rule_acc = (yr_true == yr_pred).mean()
        print(f"RULE (auto_recommendation) on {len(rule_idx)} joinable rows: "
              f"accuracy {rule_acc:.3f}  macro-F1 {rule_f1:.3f}  "
              f"← the bar any model must clear")
    else:
        rule_f1 = None
        print("no auto_recommendation to form the rule baseline.")

    # ---- B4 model comparison (nested CV) ----------------------------------
    _hr("B4 — MODEL COMPARISON (nested CV, tuned inside folds)")
    print(f"n={n}, outer={K_OUTER}-fold, inner={K_INNER}-fold. macro-F1 is the "
          f"selection metric; per-fold ± shows the (large, small-n) variance.")
    results = {}
    for name, (pipe, grid) in _models().items():
        yp, fold_f1, picks = _nested_cv(name, pipe, grid, X, y)
        mf1 = _report(name, y, yp, fold_f1)
        results[name] = (mf1, fold_f1, picks)
    ranked = sorted(results.items(), key=lambda kv: kv[1][0], reverse=True)
    best = ranked[0][0]
    print("\nRANKING by pooled macro-F1: " + " > ".join(
        f"{k} {v[0]:.3f}" for k, v in ranked))
    print(f"most-common tuned params for {best}: {Counter(map(str, results[best][2]))}")

    # ---- B3 time-based split ----------------------------------------------
    _hr("B3 — TIME-BASED SPLIT (train older 70% / test newer 30%)")
    order = sorted(range(n), key=lambda i: ds.meta[i]["created_at"] or "")
    cut = int(n * 0.7)
    tr_i, te_i = order[:cut], order[cut:]
    te_classes = set(y[te_i].tolist())
    if len(te_classes) < 2:
        print(f"test fold has only classes {[CLASSES[c] for c in te_classes]} "
              f"— labels cluster in time (mostly one migration batch); "
              f"time-split is not informative here.")
    else:
        pipe, grid = _models()[best]
        inner = StratifiedKFold(min(K_INNER, np.bincount(y[tr_i]).min()),
                                shuffle=True, random_state=RS)
        gs = GridSearchCV(pipe, grid, scoring="f1_macro", cv=inner, n_jobs=-1)
        gs.fit(X[tr_i], y[tr_i])
        pred = gs.predict(X[te_i])
        print(f"{best}: train {len(tr_i)} / test {len(te_i)}  "
              f"macro-F1 {f1_score(y[te_i], pred, average='macro', zero_division=0):.3f}  "
              f"balanced-acc {balanced_accuracy_score(y[te_i], pred):.3f}")
        print(f"  test class mix: {dict(Counter(CLASSES[c] for c in y[te_i].tolist()))}")

    # ---- B3 learning curve ------------------------------------------------
    _hr("B3 — LEARNING CURVE (does more data help?)")
    pipe, grid = _models()[best]
    # use the most-common tuned C/params: simplest = re-tune lightly is overkill;
    # use a sensible fixed config of the winner for the curve.
    try:
        sizes, tr_sc, te_sc = learning_curve(
            pipe, X, y, cv=StratifiedKFold(K_OUTER, shuffle=True, random_state=RS),
            scoring="f1_macro", train_sizes=np.linspace(0.4, 1.0, 5), n_jobs=-1)
        for s, te in zip(sizes, te_sc.mean(axis=1)):
            print(f"  train_n={int(s):3d}  CV macro-F1 {te:.3f}")
        slope = te_sc.mean(axis=1)[-1] - te_sc.mean(axis=1)[0]
        print(f"  trend over the range: {slope:+.3f} "
              f"({'still climbing — more labels will help' if slope > 0.03 else 'flat — plateauing'})")
    except Exception as exc:
        print(f"learning curve unavailable: {exc}")

    # ---- B3 calibration (proxy) -------------------------------------------
    _hr("B3 — CALIBRATION (CV log-loss for the leading model)")
    try:
        from sklearn.model_selection import cross_val_predict
        proba = cross_val_predict(
            _models()[best][0], X, y, method="predict_proba",
            cv=StratifiedKFold(K_OUTER, shuffle=True, random_state=RS))
        print(f"  {best} CV log-loss {log_loss(y, proba):.3f} "
              f"(lower=better; vs uniform {log_loss(y, np.full((n,3),1/3)):.3f}). "
              f"n too small to fit a separate calibrator — interpret as a "
              f"sharpness proxy only.")
    except Exception as exc:
        print(f"calibration unavailable: {exc}")

    # ---- B2 text arm ------------------------------------------------------
    _hr("B2 — TEXT-FEATURE ARM (TF-IDF on title+description, fit in folds)")
    have_text = sum(1 for t in ds.text if t.strip())
    if have_text < n * 0.6:
        print(f"only {have_text}/{n} rows have text — skipping (too sparse).")
    else:
        txt = np.array(ds.text, dtype=object)
        outer = StratifiedKFold(K_OUTER, shuffle=True, random_state=RS)
        yp = np.empty_like(y)
        for tr, te in outer.split(X, y):
            vec = TfidfVectorizer(min_df=2, ngram_range=(1, 2),
                                  max_features=300, stop_words="english")
            Xtr = vec.fit_transform(txt[tr])
            Xte = vec.transform(txt[te])
            clf = LogisticRegression(class_weight="balanced", max_iter=5000)
            clf.fit(Xtr, y[tr])
            yp[te] = clf.predict(Xte)
        tf1 = f1_score(y, yp, average="macro", zero_division=0)
        print(f"  TF-IDF+LogReg macro-F1 {tf1:.3f}  vs structured {best} "
              f"{results[best][0]:.3f}  → text "
              f"{'ADDS signal' if tf1 > results[best][0] else 'does NOT beat structured'} "
              f"(high-dim/small-n: treat with caution).")

    # ---- B5 dimensionality / noise ----------------------------------------
    _hr("B5 — DIMENSIONALITY / NOISE (vs full feature matrix)")
    # dead/near-constant columns (no variance) → drop and re-measure the winner.
    var = np.nanvar(X, axis=0)
    dead = [ds.feature_names[i] for i in range(X.shape[1]) if var[i] < 1e-9]
    print(f"zero-variance features (drop candidates): {dead or 'none'}")
    pipe, grid = _models()[best]

    def _cv_macro(pp):
        outer = StratifiedKFold(K_OUTER, shuffle=True, random_state=RS)
        yp = np.empty_like(y)
        for tr, te in outer.split(X, y):
            inner = StratifiedKFold(K_INNER, shuffle=True, random_state=RS)
            gs = GridSearchCV(pp, grid, scoring="f1_macro", cv=inner, n_jobs=-1)
            gs.fit(X[tr], y[tr]); yp[te] = gs.predict(X[te])
        return f1_score(y, yp, average="macro", zero_division=0)

    full = results[best][0]
    print(f"  full ({X.shape[1]} feat):        macro-F1 {full:.3f}")
    pca_pipe = Pipeline([("impute", SimpleImputer(strategy="mean")),
                         ("scale", StandardScaler()),
                         ("pca", PCA(n_components=0.95, random_state=RS)),
                         ("clf", _models()[best][0].named_steps["clf"])])
    print(f"  PCA(95% var) + {best}:        macro-F1 {_cv_macro(pca_pipe):.3f}")
    kbest_pipe = Pipeline([("impute", SimpleImputer(strategy="mean")),
                           ("scale", StandardScaler()),
                           ("sel", SelectKBest(f_classif, k=min(10, X.shape[1]))),
                           ("clf", _models()[best][0].named_steps["clf"])])
    print(f"  SelectKBest(10) + {best}:     macro-F1 {_cv_macro(kbest_pipe):.3f}")

    _hr("VERDICT")
    print(f"leading model: {best}  (pooled macro-F1 {results[best][0]:.3f}, "
          f"per-fold {results[best][1].mean():.3f} ± {results[best][1].std():.3f})")
    print(f"baselines: majority {base_maj:.3f}"
          + (f", rule macro-F1 {rule_f1:.3f}" if rule_f1 is not None else ""))
    print("Read with the small-n caveat: per-fold ± is wide; differences inside "
          "~1 std are noise. See the written recommendation.")
    _hr("DONE — read-only, nothing written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
