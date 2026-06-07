"""One-shot backfill for migration rows the uid-keyed Excel sync missed.

When a Form1 Form_ID is edited after the first import (e.g. a -HHMM/-NNNN suffix
is added), the migration's upsert (keyed on `uid`) can no longer match the
existing DB row, so fields like `search_date` / `submitted_by` /
`submitted_by_email` stay blank on those specific rows.

This script matches `source='migration'` rfp_submissions rows to the Excel
Form1 sheet by NORMALISED TITLE and fills ONLY the still-NULL fields. Idempotent.

    python scripts/backfill_form1_meta.py             # apply
    python scripts/backfill_form1_meta.py --dry-run   # preview only
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ for migrate_excel

from migrate_excel import (  # noqa: E402
    DEFAULT_XLSX, build_col_map, map_form1_row_by_header,
)
from db.supabase_client import get_client  # noqa: E402

_BACKFILL_FIELDS = ("search_date", "submitted_by", "submitted_by_email")


def _norm(t) -> str:
    return re.sub(r"\s+", " ", str(t or "").strip().lower())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.xlsx or not Path(args.xlsx).exists():
        sys.exit(f"Excel file not found: {args.xlsx}")

    wb = load_workbook(args.xlsx, data_only=True)
    ws = wb["Form1"]
    col_map = build_col_map(ws)
    by_title: dict[str, dict] = {}
    for rn in range(2, ws.max_row + 1):
        row = [ws.cell(row=rn, column=c).value for c in range(1, ws.max_column + 1)]
        rec = map_form1_row_by_header(row, col_map)
        if not rec:
            continue
        key = _norm(rec.get("opportunity_title"))
        if key and key not in by_title:
            by_title[key] = {f: rec.get(f) for f in _BACKFILL_FIELDS}
    print(f"Form1: {len(by_title)} titles indexed")

    sb = get_client()
    db_rows = (
        sb.table("rfp_submissions")
        .select("uid,opportunity_title,search_date,submitted_by,submitted_by_email")
        .eq("source", "migration").execute().data or []
    )
    fixed, unmatched = 0, []
    for r in db_rows:
        meta = by_title.get(_norm(r.get("opportunity_title")))
        if not meta:
            if not r.get("search_date"):
                unmatched.append(r.get("uid"))
            continue
        patch = {f: meta[f] for f in _BACKFILL_FIELDS if not r.get(f) and meta.get(f)}
        if patch:
            print(f"  {r.get('uid')}: {patch}")
            if not args.dry_run:
                sb.table("rfp_submissions").update(patch).eq("uid", r["uid"]).execute()
            fixed += 1
    print(f"{'[dry-run] would backfill' if args.dry_run else 'backfilled'} {fixed} row(s).")
    if unmatched:
        print(f"still-missing (no Form1 title match): {unmatched}")


if __name__ == "__main__":
    main()
