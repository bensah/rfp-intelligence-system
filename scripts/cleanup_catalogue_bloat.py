"""Undo the 2026-06-24 catalogue-bloat policy breach.

`sync_listing_urls_to_catalogue.py` pushed 158 unverified donor hosts straight
into donor_sources (the CATALOGUE / scan source-of-truth), bypassing the policy:
new sources live in source_registry (staging) until verified, only then promoted.
That inflated active sources ~50 → 211, added duplicates + dead hosts, and
inverted the invariant (catalogue must be < registry).

This script restores the curated catalogue:
  1. Identify the bulk batch: created on 2026-06-24 with source_class='primary'
     AND no verification/scrape signal (last_scrape_status / verified_by / notes).
  2. Ensure every bulk host exists in source_registry as status='pending'
     (so nothing is lost — they can be verified + promoted later).
  3. DELETE the bulk rows from donor_sources.

Re-derivable: the bulk rows came from donor_intel.opportunity_listing_urls, so a
future verified promotion can re-add the good ones.

Usage:
    python scripts/cleanup_catalogue_bloat.py            # DRY-RUN (default)
    python scripts/cleanup_catalogue_bloat.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from db.supabase_client import get_client

_BULK_DATE = "2026-06-24"
_BULK_CLASS = "primary"
_EXPECT_MIN, _EXPECT_MAX = 120, 180   # safety rail around the known 158


def _norm_host(url_or_host: str) -> str:
    try:
        from core.source_registry import normalize_host
        h = normalize_host(url_or_host)
        if h:
            return h
    except Exception:
        pass
    h = (url_or_host or "").lower()
    for p in ("https://", "http://", "www."):
        h = h.replace(p, "")
    return h.split("/")[0].strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the delete (default: dry-run)")
    args = ap.parse_args()
    sb = get_client()

    cat = sb.table("donor_sources").select("*").limit(5000).execute().data or []
    reg = sb.table("source_registry").select("*").limit(5000).execute().data or []

    def _verified_signal(r) -> bool:
        return bool(r.get("last_scrape_status") or r.get("verified_by")
                    or (r.get("notes") and str(r["notes"]).strip() not in ("", "None")))

    bulk = [r for r in cat
            if (r.get("created_at") or "")[:10] == _BULK_DATE
            and (r.get("source_class") or "") == _BULK_CLASS
            and not _verified_signal(r)]
    keep_active = sum(1 for r in cat if r.get("is_active") and r not in bulk)

    print(f"Catalogue now: {len(cat)} total · {sum(1 for r in cat if r.get('is_active'))} active")
    print(f"Registry now:  {len(reg)} total")
    print(f"Bulk to remove: {len(bulk)}  →  catalogue after: {len(cat) - len(bulk)} "
          f"({keep_active} active)")

    if not (_EXPECT_MIN <= len(bulk) <= _EXPECT_MAX):
        print(f"\n⚠ ABORT: bulk count {len(bulk)} outside safety rail "
              f"[{_EXPECT_MIN},{_EXPECT_MAX}] — investigate before deleting.")
        return 1

    # Hosts to ensure in the registry (pending) before deletion.
    reg_hosts = {_norm_host(r.get("host") or r.get("rfp_listing_url") or "") for r in reg}
    to_register = {}
    for r in bulk:
        h = _norm_host(r.get("host") or r.get("rfp_listing_url") or "")
        if h and h not in reg_hosts and h not in to_register:
            to_register[h] = {
                "host": h, "classification": "unknown", "status": "pending",
                "detected_as": "catalogue-cleanup",
                "sample_url": (r.get("rfp_listing_url") or r.get("base_url") or "")[:600] or None,
                "sample_title": (r.get("donor_name") or "")[:300] or None,
                "verified_by": "cleanup-2026-06-25",
            }
    print(f"Bulk hosts to backfill into registry (pending): {len(to_register)} "
          f"({len(bulk) - len(to_register)} already present)")

    bulk_ids = [r["source_uid"] for r in bulk if r.get("source_uid") is not None]
    print(f"\nSample of rows to delete (first 12 of {len(bulk)}):")
    for r in bulk[:12]:
        print(f"  [{r.get('source_uid')}] {r.get('donor_name','')[:34]:34} "
              f"{(r.get('host') or '')[:38]}")

    if not args.apply:
        print("\nDRY-RUN — nothing changed. Re-run with --apply to execute.")
        return 0

    # 1) backfill registry
    if to_register:
        rows = list(to_register.values())
        for i in range(0, len(rows), 100):
            sb.table("source_registry").upsert(rows[i:i+100], on_conflict="host").execute()
        print(f"Registry: backfilled {len(rows)} pending host(s).")
    # 2) delete bulk from catalogue
    for i in range(0, len(bulk_ids), 100):
        sb.table("donor_sources").delete().in_("source_uid", bulk_ids[i:i+100]).execute()
    cat2 = sb.table("donor_sources").select("source_uid,is_active").limit(5000).execute().data or []
    reg2 = sb.table("source_registry").select("source_uid").limit(5000).execute().data or []
    print(f"\nDONE. Catalogue: {len(cat2)} total · "
          f"{sum(1 for r in cat2 if r.get('is_active'))} active · Registry: {len(reg2)} total")
    print("Invariant restored:", "OK (registry > catalogue)" if len(reg2) > len(cat2)
          else "STILL INVERTED — investigate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
