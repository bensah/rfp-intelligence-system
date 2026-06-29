"""Stage donor_intel.opportunity_listing_urls → source_registry (pending).

POLICY (enforced here): new sources live in the REGISTRY first and stay there
until a human verifies them (Verify → Source registry), and only THEN are they
promoted into donor_sources (the CATALOGUE = the scan source-of-truth). The
catalogue must always be the smaller, hand-verified subset; the registry is the
superset. So this script NEVER writes to the catalogue (an earlier version did,
which inflated the catalogue ~50 → 211 active and inverted the invariant — see
scripts/cleanup_catalogue_bloat.py).

The donor-360 enrichment captures each donor's OWN RFP/tender/grant listing
page(s) in `donor_intel.opportunity_listing_urls`. This stages those as
`source_registry` rows with status='pending', deduped by HOST against BOTH the
registry AND the catalogue (so nothing already staged or already promoted is
re-added). The actual best-method / liveness check + promotion happens during
human verification, per policy.

ORDER: import the enriched donor CSV first (populates opportunity_listing_urls),
THEN run this to stage into the registry, THEN verify + promote in the UI.

Usage:
    python scripts/sync_listing_urls_to_catalogue.py            # dry-run
    python scripts/sync_listing_urls_to_catalogue.py --apply    # stage to registry
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

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


def _norm_host(url: str) -> str:
    try:
        from core.source_registry import normalize_host
        h = normalize_host(url)
        if h:
            return h
    except Exception:
        pass
    h = (url or "").strip().lower()
    for p in ("https://", "http://", "www."):
        h = h.replace(p, "")
    return h.split("/")[0].strip()


def _split(raw: str) -> list[str]:
    """opportunity_listing_urls is pipe-/semicolon-/comma-separated."""
    if not raw:
        return []
    return [u.strip() for u in re.split(r"[|;,\n]+", str(raw))
            if u.strip().startswith("http")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Stage to registry (default: dry-run).")
    args = ap.parse_args()
    sb = get_client()

    # Dedup by HOST against BOTH the registry (already staged) and the catalogue
    # (already promoted) — a host in either place must NOT be re-staged.
    reg = sb.table("source_registry").select("host").limit(5000).execute().data or []
    cat = sb.table("donor_sources").select("host,base_url,rfp_listing_url").limit(5000).execute().data or []
    known = {_norm_host(r.get("host") or "") for r in reg}
    for r in cat:
        known.add(_norm_host(r.get("host") or r.get("rfp_listing_url") or r.get("base_url") or ""))
    known.discard("")

    donors = (sb.table("donor_intel")
              .select("id,donor,canonical_key,opportunity_listing_urls")
              .limit(5000).execute().data or [])

    new_rows, seen = [], set(known)
    for d in donors:
        for url in _split(d.get("opportunity_listing_urls") or ""):
            if not urlparse(url).netloc:
                continue
            h = _norm_host(url)
            if not h or h in seen:
                continue          # dedup: already staged / promoted / seen this run
            seen.add(h)
            new_rows.append({
                "host": h,
                "classification": "unknown",
                "status": "pending",          # NEVER auto-promoted to the catalogue
                "detected_as": "donor-360-listing",
                "sample_url": url[:600],
                "sample_title": ((d.get("donor") or "").strip() or "(unnamed donor)")[:300],
                "verified_by": "donor-csv-sync",
            })

    print(f"donor_intel: {len(donors)} rows · known hosts (registry+catalogue): "
          f"{len(known)}\nNEW hosts to STAGE into registry (pending): {len(new_rows)}\n")
    for r in new_rows[:30]:
        print(f"  + {r['sample_title'][:34]:34} {r['host'][:54]}")
    if len(new_rows) > 30:
        print(f"  … and {len(new_rows) - 30} more")
    if not new_rows:
        print("Nothing to stage. (Import the enriched donor CSV first, or all "
              "hosts are already in the registry/catalogue.)")
        return 0
    if not args.apply:
        print("\nDRY-RUN — nothing staged. Re-run with --apply to add them to the "
              "registry as 'pending'. Verify + promote in Verify → Source registry.")
        return 0
    added = 0
    for i in range(0, len(new_rows), 100):
        try:
            sb.table("source_registry").upsert(
                new_rows[i:i + 100], on_conflict="host").execute()
            added += len(new_rows[i:i + 100])
        except Exception as exc:
            print(f"  stage batch failed: {exc}")
    print(f"\nStaged {added} host(s) into source_registry as 'pending'. "
          "Next: verify each in Verify → Source registry, then promote to the "
          "catalogue. (This script NEVER writes to donor_sources by policy.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
