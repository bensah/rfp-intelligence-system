"""Import the donor intelligence matrix into Supabase.

Loads docs/donor_intel_matrix_app_ready.xlsx:
  * `donors`        -> donor_intel        (upsert on canonical_key)
  * `source_seeds`  -> donor_source_seeds (upsert on donor+url)

Blank cells are stored as NULL (the readme convention: BLANK = "not
documented" / unknown, NEVER coerce to "no"). Re-runnable: upserts so a
re-import refreshes rows in place. Run after migration 020:

    python scripts/import_donor_intel.py
"""
from __future__ import annotations

import os
import sys

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.supabase_client import get_client  # noqa: E402

XLSX = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs", "donor_intel_matrix_app_ready.xlsx",
)


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _rows(ws):
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() for h in next(it)]
    for raw in it:
        yield {header[i]: _clean(raw[i]) for i in range(len(header)) if i < len(raw)}


def main() -> None:
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    sb = get_client()

    # ---- donors -> donor_intel ----
    donors = [r for r in _rows(wb["donors"]) if r.get("canonical_key")]
    # Upsert in batches keyed on canonical_key.
    for i in range(0, len(donors), 100):
        sb.table("donor_intel").upsert(
            donors[i:i + 100], on_conflict="canonical_key"
        ).execute()
    print(f"donor_intel: upserted {len(donors)} donors")

    # ---- source_seeds -> donor_source_seeds ----
    seeds = [
        {"donor": r.get("donor"), "url": r.get("url"),
         "source_type": r.get("source_type")}
        for r in _rows(wb["source_seeds"])
        if r.get("donor") and r.get("url")
    ]
    for i in range(0, len(seeds), 200):
        sb.table("donor_source_seeds").upsert(
            seeds[i:i + 200], on_conflict="donor,url"
        ).execute()
    print(f"donor_source_seeds: upserted {len(seeds)} seed URLs")


if __name__ == "__main__":
    main()
