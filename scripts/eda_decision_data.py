"""EDA / data-quality audit for the decision model (ML rebuild — B0 + B1).

READ-ONLY. Pulls scan_decisions (labels + features) and rfp_submissions
(extraction audit) from Supabase and prints a findings report. Writes nothing.

  python scripts/eda_decision_data.py            # full report to stdout
  python scripts/eda_decision_data.py --csv out  # also dump frames to out/*.csv

B0 — Extraction-data quality: per-source missingness/noise of the fields the
     model depends on (title, description, deadline, value, currency, geography,
     program_area, funding_agency, focus_theme).
B1 — Dataset assembly + EDA: human_decision label class balance, per-source
     label mix, feature distributions + missingness, leakage checks, statistical
     power.

Nothing here trains or persists — it bounds what training can achieve.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:                                   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.decision_model import CLASSES, FEATURE_NAMES, raw_vector  # noqa: E402
from db.supabase_client import get_client, safe_execute  # noqa: E402

pd.set_option("display.width", 140)
pd.set_option("display.max_columns", 40)

# Fields the model / matching rely on, and how to read them off rfp_submissions.
_EXTRACTION_FIELDS = [
    "opportunity_title", "brief_description", "submission_deadline",
    "estimated_value", "currency", "call_geographic_scope", "call_domain_areas",
    "funding_agency", "focus_theme", "date_posted",
]


def _hr(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def _fetch_all(table: str, columns: str, *, eq: dict | None = None,
               page: int = 1000) -> list[dict]:
    """Paginated read with transient-error retry (Supabase caps at 1000/req)."""
    sb = get_client()
    out: list[dict] = []
    start = 0
    while True:
        q = sb.table(table).select(columns)
        for k, v in (eq or {}).items():
            q = q.eq(k, v)
        q = q.range(start, start + page - 1)
        rows = safe_execute(q).data or []
        out.extend(rows)
        if len(rows) < page:
            break
        start += page
    return out


def _is_missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple)):
        return len([x for x in v if x not in (None, "")]) == 0
    return False


# ---------------------------------------------------------------------------
# B0 — extraction quality
# ---------------------------------------------------------------------------
def audit_extraction(subs: list[dict]) -> pd.DataFrame:
    _hr("B0 — EXTRACTION QUALITY (rfp_submissions, per source)")
    df = pd.DataFrame(subs)
    if df.empty:
        print("no rfp_submissions rows.")
        return df
    print(f"total rfp_submissions: {len(df)}")
    print("by source:", dict(Counter(df.get("source", pd.Series(dtype=str)).fillna("∅"))))

    # exclude duplicates from the quality picture (they're flagged out of training)
    if "is_duplicate" in df:
        dup = int(df["is_duplicate"].fillna(False).astype(bool).sum())
        print(f"is_duplicate=true: {dup} (excluded from per-source missingness below)")
        live = df[~df["is_duplicate"].fillna(False).astype(bool)].copy()
    else:
        live = df.copy()

    rows = []
    for src, g in live.groupby(live["source"].fillna("∅")):
        rec = {"source": src, "n": len(g)}
        for f in _EXTRACTION_FIELDS:
            if f not in g:
                rec[f] = float("nan")
                continue
            miss = g[f].apply(_is_missing).mean() * 100
            rec[f] = round(miss, 1)
        rows.append(rec)
    # all-sources row
    rec = {"source": "ALL", "n": len(live)}
    for f in _EXTRACTION_FIELDS:
        rec[f] = round(live[f].apply(_is_missing).mean() * 100, 1) if f in live else float("nan")
    rows.append(rec)
    miss_df = pd.DataFrame(rows).set_index("source")
    print("\n% MISSING per field (0 = always present, 100 = never extracted):")
    print(miss_df.to_string())

    # noise spot-checks on the numeric/date/array fields
    _hr("B0 — NOISE / VALIDITY SPOT-CHECKS")
    if "estimated_value" in live:
        ev = pd.to_numeric(live["estimated_value"], errors="coerce")
        present = ev.notna().sum()
        print(f"estimated_value: {present}/{len(live)} numeric-parseable; "
              f"<=0: {int((ev <= 0).sum())}; "
              f"min {ev.min():,.0f} / median {ev.median():,.0f} / max {ev.max():,.0f}"
              if present else "estimated_value: none parseable")
    if "currency" in live:
        print("currency values:", dict(Counter(live["currency"].fillna("∅"))))
    if "submission_deadline" in live:
        dl = pd.to_datetime(live["submission_deadline"], errors="coerce", utc=True)
        today = pd.Timestamp.now(tz="UTC")
        print(f"submission_deadline: {dl.notna().sum()}/{len(live)} parseable; "
              f"in the PAST: {int((dl < today).sum())}; "
              f"> today+2y: {int((dl > today + pd.Timedelta(days=730)).sum())}")
    if "call_domain_areas" in live:
        # how many carry a generic 'Health' vs specific taxonomy keys
        def _generic(v):
            if isinstance(v, (list, tuple)):
                vals = [str(x) for x in v]
            elif v:
                vals = [str(v)]
            else:
                return None
            if not vals:
                return None
            return all(x.strip().lower() in ("health", "") for x in vals)
        gen = live["call_domain_areas"].apply(_generic)
        print(f"program_area: generic-'Health'-only {int((gen == True).sum())}, "
              f"specific {int((gen == False).sum())}, missing {int(gen.isna().sum())}")
    if "funding_agency" in live:
        top = Counter(live["funding_agency"].fillna("∅")).most_common(10)
        print("top funding_agency:", top)
    return df


# ---------------------------------------------------------------------------
# B1 — dataset assembly + EDA
# ---------------------------------------------------------------------------
def _label(r: dict) -> str | None:
    lab = (r.get("label") or "").strip().title()
    return lab if lab in CLASSES else None


def assemble_labels(decisions: list[dict], subs: list[dict]) -> pd.DataFrame:
    """Latest human_decision per rfp_uid + its features, joined to the true
    rfp_submissions.source / auto_recommendation / decision_overridden_by."""
    sub_by_uid = {}
    for s in subs:
        for k in (s.get("uid"), s.get("form_id")):
            if k:
                sub_by_uid[k] = s

    # latest per rfp_uid (decisions come back unordered; sort by created_at)
    decisions = sorted(decisions, key=lambda r: r.get("created_at") or "")
    latest: dict[str, dict] = {}
    no_uid: list[dict] = []
    for r in decisions:
        lab = _label(r)
        if not lab:
            continue
        uid = r.get("rfp_uid")
        if uid:
            latest[uid] = r          # later row wins
        else:
            no_uid.append(r)

    recs = []
    for uid, r in latest.items():
        sub = sub_by_uid.get(uid, {})
        recs.append({
            "rfp_uid": uid,
            "label": _label(r),
            "created_at": r.get("created_at"),
            "decision_source": r.get("source"),
            "true_source": sub.get("source"),
            "overridden_by": sub.get("decision_overridden_by"),
            "auto_recommendation": sub.get("auto_recommendation"),
            "funding_agency": r.get("funding_agency") or sub.get("funding_agency"),
            "features": r.get("features") or {},
        })
    for r in no_uid:
        recs.append({
            "rfp_uid": None, "label": _label(r), "created_at": r.get("created_at"),
            "decision_source": r.get("source"), "true_source": None,
            "overridden_by": None, "auto_recommendation": None,
            "funding_agency": r.get("funding_agency"), "features": r.get("features") or {},
        })
    return pd.DataFrame(recs)


def eda_labels(df: pd.DataFrame) -> None:
    _hr("B1 — LABELS: class balance & statistical power")
    n = len(df)
    counts = Counter(df["label"])
    print(f"usable human_decision labels (latest per rfp_uid): {n}")
    print("class balance:", dict(counts))
    if n:
        base = max(counts.values()) / n
        print(f"majority-class baseline accuracy: {base:.3f} "
              f"(predict '{counts.most_common(1)[0][0]}' always)")
        print(f"cold-start gate (train script): MIN_TOTAL=45, MIN_PER_CLASS=10 -> "
              f"{'MET' if n >= 45 and all(counts.get(c,0) >= 10 for c in CLASSES) else 'NOT MET'}")
        # power note
        print(f"\nPOWER NOTE: with n={n} and 3 classes, a 5-fold CV test fold holds "
              f"~{n//5} rows; a single misclassification moves macro-F1 by "
              f"~{1/max(1,(n//5)):.2f}. Treat all metrics as indicative, ±wide CI.")

    _hr("B1 — LABELS by true rfp_submissions.source")
    if "true_source" in df:
        tab = pd.crosstab(df["true_source"].fillna("∅(no join)"), df["label"])
        print(tab.to_string())

    _hr("B1 — LEAKAGE CHECK: human label vs rule auto_recommendation")
    sub = df[df["auto_recommendation"].notna()].copy()
    if sub.empty:
        print("no rows with auto_recommendation joined — cannot check.")
    else:
        sub["auto_recommendation"] = sub["auto_recommendation"].str.strip().str.title()
        ct = pd.crosstab(sub["auto_recommendation"], sub["label"],
                         margins=True, margins_name="ALL")
        print("rows=rule, cols=human:")
        print(ct.to_string())
        agree = (sub["auto_recommendation"] == sub["label"]).mean()
        print(f"\nrule==human agreement: {agree:.3f} over {len(sub)} joinable rows.")
        print("→ auto_recommendation is EXCLUDED as a feature by design "
              "(echoes the rule); this quantifies how much it would leak.")

    # source-of-truth sanity: auto-without-override shouldn't be a training label
    leaky = df[(df["true_source"] == "auto") & df["overridden_by"].isna()]
    print(f"\nauto-source rows WITHOUT a human override present as labels: {len(leaky)} "
          f"(should be ~0 — these are rule output, not human judgement).")


def eda_features(df: pd.DataFrame) -> None:
    _hr("B1 — FEATURE MATRIX: missingness & distributions")
    X = np.array([raw_vector(f) for f in df["features"]], float) if len(df) else np.empty((0, len(FEATURE_NAMES)))
    if X.size == 0:
        print("no feature vectors.")
        return
    feat = pd.DataFrame(X, columns=list(FEATURE_NAMES))
    miss = feat.isna().mean().mul(100).round(1)
    desc = feat.describe().T[["mean", "std", "min", "50%", "max"]]
    summary = desc.join(miss.rename("%missing"))
    print(f"feature vectors: {len(feat)} rows x {feat.shape[1]} cols")
    print(summary.to_string())

    # near-constant features (no signal at this n)
    nunique = feat.nunique(dropna=True)
    const = nunique[nunique <= 1].index.tolist()
    if const:
        print(f"\nNEAR-CONSTANT (<=1 distinct, no signal yet): {const}")

    # criteria value spread (2/1/0/NaN) per class — the core signal
    _hr("B1 — CRITERIA (2/1/0/NaN) mean per class — does signal separate classes?")
    crit = list(FEATURE_NAMES[:9])
    feat["__label"] = df["label"].values
    means = feat.groupby("__label")[crit].mean().round(2)
    print(means.T.to_string())


def main() -> int:
    csv_dir = None
    if "--csv" in sys.argv:
        i = sys.argv.index("--csv")
        csv_dir = Path(sys.argv[i + 1]) if i + 1 < len(sys.argv) else Path("eda_out")
        csv_dir.mkdir(parents=True, exist_ok=True)

    _hr("PULLING DATA (read-only)")
    decisions = _fetch_all(
        "scan_decisions",
        "id,created_at,event_type,label,reason,rfp_uid,opportunity_title,"
        "opportunity_link,funding_agency,source,alignment_score,features,decided_by",
        eq={"event_type": "human_decision"})
    print(f"human_decision rows: {len(decisions)}")
    rejects = _fetch_all("scan_decisions", "id,label,source",
                         eq={"event_type": "system_reject"})
    print(f"system_reject rows: {len(rejects)}")
    feedback = _fetch_all("scan_decisions", "id,label", eq={"event_type": "feedback"})
    print(f"feedback rows: {len(feedback)}")
    subs = _fetch_all(
        "rfp_submissions",
        "uid,form_id,source,opportunity_title,brief_description,submission_deadline,"
        "estimated_value,currency,call_geographic_scope,program_area,funding_agency,"
        "focus_theme,date_posted,decision,auto_recommendation,decision_overridden_by,"
        "is_duplicate")
    print(f"rfp_submissions rows: {len(subs)}")

    if rejects:
        _hr("REJECTS by reason category (gate-quality context)")
        print(dict(Counter((r.get("label") or "∅") for r in rejects).most_common()))
    if feedback:
        print("\nfeedback labels:", dict(Counter((f.get("label") or "∅") for f in feedback)))

    audit_extraction(subs)
    labels = assemble_labels(decisions, subs)
    eda_labels(labels)
    eda_features(labels)

    if csv_dir:
        pd.DataFrame(subs).to_csv(csv_dir / "rfp_submissions.csv", index=False)
        labels.drop(columns=["features"]).to_csv(csv_dir / "labels.csv", index=False)
        feat = pd.DataFrame([raw_vector(f) for f in labels["features"]],
                            columns=list(FEATURE_NAMES))
        feat["label"] = labels["label"].values
        feat.to_csv(csv_dir / "feature_matrix.csv", index=False)
        print(f"\nCSVs written to {csv_dir}/")

    _hr("DONE — read-only, nothing written to the DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
