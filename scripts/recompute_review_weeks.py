"""One-off maintenance: recompute every rfp_submissions.review_week from the
row's own SEARCH_DATE (fallback submitted_at), so the weekly review board
attributes each RFP to the week it ACTUALLY entered the pipeline.

ANCHOR = search_date — this matches scripts/migrate_excel.py, which set each
Excel row's review_week from its original "Search Date". `submitted_at` is the
IMPORT timestamp for migrated rows (all recent), so anchoring on it wrongly
collapses every historical RFP into the current week. search_date holds the
original screening date for Excel rows and "now" for manual/auto rows, so it's
correct for all sources.

Why: rows created before 2026-06-06 used `upcoming_review_week_label()`, which
pushed Friday/weekend submissions to the FOLLOWING Monday's week (e.g. a
Saturday-6-Jun submission landed in "Week 24 (8-14 Jun)" instead of
"Week 23 (1-7 Jun)"). This rewrites them to the current-week label that
`review_week_label()` now produces at write time.

Safe + idempotent: only rows whose stored week differs from the recomputed
week are updated; re-running changes nothing further.

Run from the repo root:
    python scripts/recompute_review_weeks.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

# Make `from core ...` / `from db ...` work when invoked as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.review_week import review_week_label  # noqa: E402
from db.supabase_client import get_client  # noqa: E402


def _to_date(s):
    """Parse an ISO timestamp/date string to a date; None on failure."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).split("T")[0]).date()
    except (ValueError, TypeError):
        return None


def main() -> None:
    sb = get_client()
    rows = (
        sb.table("rfp_submissions")
        .select("uid,submitted_at,search_date,review_week")
        .execute()
        .data
        or []
    )
    changed = 0
    for r in rows:
        d = _to_date(r.get("search_date")) or _to_date(r.get("submitted_at"))
        if not d:
            continue
        correct = review_week_label(d)
        if (r.get("review_week") or "") != correct:
            sb.table("rfp_submissions").update(
                {"review_week": correct}
            ).eq("uid", r["uid"]).execute()
            changed += 1
            print(f"  {r['uid']}: {r.get('review_week')!r} -> {correct!r}")
    print(f"\nUpdated {changed} of {len(rows)} row(s).")


if __name__ == "__main__":
    main()
