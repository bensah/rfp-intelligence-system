"""Reactivate the Coefficient Giving (ex-Open Philanthropy) /funds/ source.

It was parked as scrape_method='manual' + is_active=false before the dedicated
handler existed. core/scraper.py now has `_scan_coefficient_giving` (routed by
host), which crawls every fund page and keeps only the RFP-type "Research &
Updates" cards. Set the row to is_active=true + scrape_method='html' so the
weekly extraction picks it up (the host route fires before the generic html
branch, so the dedicated parser is used).

Usage:
    python scripts/reactivate_coefficient_giving.py            # apply
    python scripts/reactivate_coefficient_giving.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from db.supabase_client import get_client


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sb = get_client()

    rows = (sb.table("donor_sources")
            .select("source_uid,donor_name,rfp_listing_url,scrape_method,is_active")
            .ilike("rfp_listing_url", "%coefficientgiving.org/funds%")
            .execute().data or [])
    if not rows:
        print("No Coefficient Giving /funds source found — nothing to do.")
        return 0

    for r in rows:
        print(f"source_uid={r['source_uid']}  {r['donor_name']}  "
              f"{r['rfp_listing_url']}\n  before: method={r['scrape_method']!r} "
              f"active={r['is_active']}  ->  after: method='html' active=True")
        if args.dry_run:
            continue
        sb.table("donor_sources").update(
            {"scrape_method": "html", "is_active": True}
        ).eq("source_uid", r["source_uid"]).execute()

    print("\nDRY-RUN — nothing written." if args.dry_run else
          "\nUpdated. Next extraction run will crawl Coefficient Giving via "
          "the dedicated handler.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
