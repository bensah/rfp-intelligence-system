"""Fix / seed primary donor listing sources that the generic crawler mis-handled.

Two donors Bernard flagged (2026-06-20):
  * Novo Nordisk Foundation — was in donor_sources but is_active=False and pointed
    at the JS grid `/en/grant/?sort=DESC` (scrape_method=html → 0 children). The
    foundation publishes a clean RSS feed at /en/grant/feed/ (verified: 10 grant
    entries with real detail links) — switch to rss + activate.
  * NORAD — not present. Its calls list is JS-rendered (static HTML carries only a
    couple of calls), so seed it as html_js so the GitHub Actions Playwright scan
    renders the full list. Cloud Manual Scan falls back to static HTML (partial).

Idempotent: matches existing rows by donor_name (updates) else inserts. DRY-RUN by
default — prints the planned change. Run with --commit to write.

  python scripts/fix_primary_sources.py            # preview
  python scripts/fix_primary_sources.py --commit   # apply
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

SOURCES = [
    {
        "donor_name": "Novo Nordisk Foundation",
        "donor_code": "NNF",
        "base_url": "https://novonordiskfonden.dk/",
        "rfp_listing_url": "https://novonordiskfonden.dk/en/grant/feed/",
        "scrape_method": "rss",
        "is_active": True,
        "notes": "Clean grant RSS feed (/en/grant/feed/). Replaces the JS grid "
                 "/en/grant/?sort=DESC which yielded 0 children under html.",
    },
    # NORAD lives in config/sources.yaml instead — donor_sources has a CHECK
    # constraint that rejects scrape_method='html_js' (needed for its JS-rendered
    # calls list), and sources.yaml has no such constraint.
]


def main(commit: bool) -> int:
    sb = get_client()
    existing = sb.table("donor_sources").select(
        "id,donor_name,rfp_listing_url,scrape_method,is_active").execute().data or []
    by_name = {(r.get("donor_name") or "").strip().lower(): r for r in existing}

    for s in SOURCES:
        cur = by_name.get(s["donor_name"].strip().lower())
        if cur:
            print(f"UPDATE  {s['donor_name']}")
            print(f"   url:    {cur.get('rfp_listing_url')}  ->  {s['rfp_listing_url']}")
            print(f"   method: {cur.get('scrape_method')}  ->  {s['scrape_method']}")
            print(f"   active: {cur.get('is_active')}  ->  {s['is_active']}")
            if commit:
                sb.table("donor_sources").update({
                    "rfp_listing_url": s["rfp_listing_url"],
                    "scrape_method": s["scrape_method"],
                    "is_active": s["is_active"],
                    "base_url": s["base_url"],
                    "notes": s["notes"],
                }).eq("id", cur["id"]).execute()
        else:
            print(f"INSERT  {s['donor_name']}  ({s['scrape_method']})  {s['rfp_listing_url']}")
            if commit:
                sb.table("donor_sources").insert(s).execute()
    print("\nCOMMITTED." if commit else "\nDRY RUN — re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
