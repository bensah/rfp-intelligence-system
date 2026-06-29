"""Import an enriched donor-intelligence CSV → donor_intel (upsert on canonical_key).

For the donor-360 agent output (e.g. docs/donor_intelligence_mapping_v3_1.csv). All
CSV columns map 1:1 to donor_intel columns (verified). Re-runnable: upserts in place,
so existing donors refresh and NEW donors (beyond the original set) are inserted.

Conventions: blank cell -> NULL (BLANK = "not documented", never coerce to "no").
DB-managed columns (id / created_at / updated_at) are dropped so the DB assigns ids
to new rows and timestamps stay authoritative.

Usage:
    python scripts/import_donor_intel_csv.py [path]   # default: docs/donor_intelligence_mapping_v3_1.csv
    python scripts/import_donor_intel_csv.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from db.supabase_client import get_client

_DROP = {"id", "created_at", "updated_at", "category_clean"}   # DB-managed / derived


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?",
                    default="docs/donor_intelligence_mapping_v3_1.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sb = get_client()

    # Real donor_intel columns (so a stray CSV column never breaks the upsert).
    sample = sb.table("donor_intel").select("*").limit(1).execute().data
    db_cols = set(sample[0].keys()) if sample else set()

    with open(args.path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        raw = list(reader)
    payload, skipped = [], 0
    for r in raw:
        ck = (r.get("canonical_key") or "").strip()
        if not ck:
            # Derive from donor name if the agent left it blank (new donors).
            donor = (r.get("donor") or "").strip()
            if not donor:
                skipped += 1
                continue
            ck = donor.lower()
        row = {}
        for k, v in r.items():
            if k in _DROP or (db_cols and k not in db_cols):
                continue
            s = (v or "").strip()
            row[k] = s or None
        row["canonical_key"] = ck
        payload.append(row)

    print(f"CSV rows: {len(raw)} · upsertable: {len(payload)} · skipped(no key/donor): {skipped}")
    new_keys = {p["canonical_key"] for p in payload}
    existing = {r.get("canonical_key") for r in
                (sb.table("donor_intel").select("canonical_key").limit(5000).execute().data or [])}
    print(f"  → {len(new_keys - existing)} NEW donors, {len(new_keys & existing)} updates")
    if args.dry_run:
        print("\nDRY-RUN — nothing written. Re-run without --dry-run to upsert.")
        return 0
    done = 0
    for i in range(0, len(payload), 100):
        batch = payload[i:i + 100]
        try:
            sb.table("donor_intel").upsert(batch, on_conflict="canonical_key").execute()
            done += len(batch)
        except Exception as exc:
            print(f"  batch {i // 100} failed: {exc}")
    print(f"\nUpserted {done}/{len(payload)} donors. "
          "Next: python scripts/sync_listing_urls_to_catalogue.py --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
