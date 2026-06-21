"""Cleanup (2026-06-20): deactivate the redundant EC portal crawl now that the EU
Funding & Tenders API + TED API are wired, and move NORAD into the catalogue
(html_js is now allowed by migration 037), using its WORKING calls URL.

  * EC: deactivate any donor_sources row pointing at the ec.europa.eu funding-
    tenders PORTAL crawl (api.tech.ec.europa.eu — the API — is a different host in
    sources.yaml and is left untouched).
  * NORAD: upsert as html_js with the live calls list. Bernard's curated URL
    /en/for-partners/calls-and-announcements/ returns 404; the working page is
    /en/for-partners/guides-and-tools/calls-for-proposals2/.

DRY-RUN by default. Run with --commit to apply.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from db.supabase_client import get_client   # noqa: E402

NORAD = {
    "donor_name": "Norwegian Agency for Development Cooperation (NORAD)",
    "donor_code": "Norad",
    "base_url": "https://www.norad.no/",
    "rfp_listing_url": "https://www.norad.no/en/for-partners/guides-and-tools/"
                       "calls-for-proposals2/",
    "scrape_method": "html_js",
    # access_model / source_class intentionally omitted — those donor_sources
    # columns ship in the (still-pending) part of migration 037; backfill once run.
    "is_active": True,
    "notes": "Moved from sources.yaml 2026-06-20. JS-rendered calls list (html_js). "
             "Curated /calls-and-announcements/ URL was 404.",
    "created_by": "cleanup-script",
}


def main(commit: bool) -> int:
    sb = get_client()
    ds = sb.table("donor_sources").select(
        "id,donor_name,rfp_listing_url,scrape_method,is_active").execute().data or []

    # 1. Deactivate the redundant EC funding-tenders PORTAL crawl.
    ec = [r for r in ds if "ec.europa.eu/info/funding-tenders"
          in (r.get("rfp_listing_url") or "").lower() and r.get("is_active")]
    print(f"EC portal crawl rows to deactivate: {len(ec)}")
    for r in ec:
        print(f"   off  {r['donor_name'][:45]:45} {r['rfp_listing_url'][:55]}")
        if commit:
            sb.table("donor_sources").update({"is_active": False}).eq(
                "id", r["id"]).execute()

    # 2. Move NORAD in (upsert by listing URL).
    exists = any((r.get("rfp_listing_url") or "") == NORAD["rfp_listing_url"]
                 for r in ds)
    print(f"\nNORAD: {'update' if exists else 'insert'} {NORAD['rfp_listing_url']} "
          f"({NORAD['scrape_method']})")
    if commit:
        sb.table("donor_sources").upsert(NORAD,
                                         on_conflict="rfp_listing_url").execute()

    print("\nCOMMITTED." if commit else "\nDRY RUN — re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
