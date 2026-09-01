"""Shared training-data loader for the decision model (Workstream B).

ONE place that assembles the labeled dataset from Supabase so the evaluation
harness (scripts/eval_decision_model.py) and the trainer
(scripts/train_decision_model.py) never diverge on how X / y are built.

Read-only. Returns the latest human_decision per rfp_uid, encoded with the SAME
core.decision_model.raw_vector the live model serves on (so eval ≈ serve), plus
the joined rfp_submissions context (true source, auto_recommendation for the rule
baseline, created_at for a time-based split, and title+description text for the
optional text-feature arm).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from core.decision_model import CLASSES, FEATURE_NAMES, raw_vector  # noqa: E402
from db.supabase_client import service_client, safe_execute  # noqa: E402

_CLS_IDX = {c: i for i, c in enumerate(CLASSES)}


def _fetch_all(table: str, columns: str, *, eq: dict | None = None,
               page: int = 1000) -> list[dict]:
    # The decision model is a PLATFORM-SHARED model — it must train on human
    # decisions from ALL tenants, so read with the RLS-bypassing service client,
    # NOT get_client() (which scopes to the runner's home tenant and would train
    # the shared model on one tenant only). Mirrors the Learning-data view fix.
    sb = service_client()
    out: list[dict] = []
    start = 0
    while True:
        q = sb.table(table).select(columns)
        for k, v in (eq or {}).items():
            q = q.eq(k, v)
        rows = safe_execute(q.range(start, start + page - 1)).data or []
        out.extend(rows)
        if len(rows) < page:
            break
        start += page
    return out


class Dataset:
    """Assembled labeled dataset. Attributes are aligned by row index."""

    def __init__(self, X, y, meta, text, feature_names):
        self.X = X                       # (n, d) float, NaN = missing
        self.y = y                       # (n,) int class index
        self.meta = meta                 # list[dict]: source, auto_rec, created_at, uid
        self.text = text                 # list[str]: title + description
        self.feature_names = feature_names

    def __len__(self):
        return len(self.y)


def load(verbose: bool = True) -> Dataset:
    decisions = _fetch_all(
        "scan_decisions", "created_at,label,rfp_uid,features",
        eq={"event_type": "human_decision"})
    subs = _fetch_all(
        "rfp_submissions",
        "uid,form_id,source,auto_recommendation,decision_overridden_by,"
        "opportunity_title,brief_description")
    sub_by_uid: dict[str, dict] = {}
    for s in subs:
        for k in (s.get("uid"), s.get("form_id")):
            if k:
                sub_by_uid[k] = s

    # latest human_decision per rfp_uid (rows come back unordered)
    decisions.sort(key=lambda r: r.get("created_at") or "")
    latest: dict[str, dict] = {}
    loose: list[dict] = []
    for r in decisions:
        lab = (r.get("label") or "").strip().title()
        if lab not in _CLS_IDX or not r.get("features"):
            continue
        uid = r.get("rfp_uid")
        (latest.__setitem__(uid, r) if uid else loose.append(r))

    X, y, meta, text = [], [], [], []
    for uid, r in list(latest.items()) + [(None, r) for r in loose]:
        sub = sub_by_uid.get(uid, {}) if uid else {}
        X.append(raw_vector(r["features"]))
        y.append(_CLS_IDX[(r["label"]).strip().title()])
        meta.append({
            "uid": uid,
            "source": sub.get("source"),
            "auto_rec": (sub.get("auto_recommendation") or "").strip().title() or None,
            "created_at": r.get("created_at"),
        })
        text.append(" ".join(p for p in (
            sub.get("opportunity_title"), sub.get("brief_description")) if p))

    ds = Dataset(np.array(X, float), np.array(y, int), meta, text,
                 list(FEATURE_NAMES))
    if verbose:
        from collections import Counter
        print(f"loaded {len(ds)} labeled rows  "
              f"{dict(Counter(CLASSES[i] for i in ds.y.tolist()))}")
    return ds
