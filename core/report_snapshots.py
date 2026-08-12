"""Persisted report snapshots — neat, shareable URLs for the Report page.

Each generated report stores its full selection (period, view-by, sections,
metrics) under a short id `YYYYMMDD-NNNNNN` in `app_settings`, so the report
URL can be just `?r=<id>` instead of a long query string — and, crucially, the
report is now actually saved for future reference rather than living only in
the URL. Snapshots share one JSON dict (`app_settings.report_snapshots_json`),
pruned to the most recent N so the row never grows unbounded.

Note: single-org, shared dict — concurrent Generates last-write-wins, which is
fine for a small internal team. Revisit if this becomes multi-tenant.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime
from typing import Optional

from core import settings as _settings

_KEY = "report_snapshots_json"
_MAX = 100  # keep the most recent N snapshots


def _load() -> dict:
    raw = _settings.get_setting(_KEY)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def _save(d: dict, updated_by: Optional[str] = None) -> None:
    _settings.set_setting(_KEY, json.dumps(d), updated_by=updated_by)


def save_snapshot(selection: dict, updated_by: Optional[str] = None) -> str:
    """Persist a selection dict; return its short id `YYYYMMDD-NNNNNN`."""
    d = _load()
    today = date.today().strftime("%Y%m%d")
    rid = f"{today}-{random.randint(0, 999999):06d}"
    for _ in range(20):  # vanishingly unlikely to collide, but be safe
        if rid not in d:
            break
        rid = f"{today}-{random.randint(0, 999999):06d}"
    d[rid] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "selection": selection,
    }
    if len(d) > _MAX:  # prune oldest by created_at
        keep = sorted(d.items(),
                      key=lambda kv: kv[1].get("created_at", ""),
                      reverse=True)[:_MAX]
        d = dict(keep)
    _save(d, updated_by=updated_by)
    return rid


def get_snapshot(rid: Optional[str]) -> Optional[dict]:
    """Return the stored selection dict for `rid`, or None if not found."""
    if not rid:
        return None
    entry = _load().get(rid)
    if not entry:
        return None
    sel = entry.get("selection")
    return sel if isinstance(sel, dict) else None


def restore_items(saved_items, all_keys, saved_universe=None) -> set:
    """Which metrics a restored report should show.

    A snapshot stores the metrics that were ON. It does NOT record which metrics existed at the
    time, so a metric added to the report LATER is absent from every older snapshot — and
    reopening one silently hid it. That is what "charts went missing" was: the saved report
    predated the metric, so the checkbox came back unticked.

    New snapshots record their key universe (`all_items`), which makes the distinction exact:
    anything in that universe but not in `items` was deliberately switched off and stays off.

    For OLDER snapshots there is no universe to compare against, so the ambiguity is real and has
    to be resolved one way: a key the snapshot never knew about is treated as ON, matching the
    page's own "everything defaults to on". The cost is that a deliberate de-selection in a
    pre-`all_items` snapshot is not preserved — accepted because the alternative is a report that
    silently drops content the reader has no way to discover.
    """
    all_keys = set(all_keys or [])
    if not isinstance(saved_items, list):
        return set(all_keys)                     # nothing saved -> everything on
    kept = {k for k in saved_items if k in all_keys}
    universe = set(saved_universe) if isinstance(saved_universe, list) else set(kept)
    return kept | (all_keys - universe)
