"""Backfill host + Donor-Intelligence links (migrations 041 / 043).

source_uid is a DB-generated sequential bigint (migration 043, sequence default),
so it is NEVER written here. This backfill only populates:
  * host        — normalised netloc (strip "www."). donor_sources only; the join
                  key to the host-keyed source_registry.
  * donor_intel_id / donor_key — resolved through core.donor_intel.match_donor
                  (matches canonical_key / donor / donor_short / aliases), linking
                  the source to its donor in the Donor Intelligence Mapping table.

Idempotent: re-running recomputes and re-writes the same values. Read-then-write
per row; reports any source whose donor couldn't be resolved.

Usage:
    python scripts/backfill_source_uids.py            # apply
    python scripts/backfill_source_uids.py --dry-run  # report only, write nothing
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import donor_intel  # noqa: E402
from db.supabase_client import get_client  # noqa: E402


def norm_host(url: str) -> str:
    h = urlsplit((url or "").strip()).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _resolve_donor(*names: str) -> tuple[int | None, str | None]:
    """First non-empty name that resolves to a donor_intel row wins."""
    for n in names:
        if not n or str(n).strip().lower() in ("", "nan", "none"):
            continue
        row = donor_intel.match_donor(n)
        if row:
            return row.get("id"), row.get("canonical_key")
    return None, None


def backfill_catalogue(sb, dry: bool) -> list[str]:
    rows = sb.table("donor_sources").select(
        "id,donor_name,donor_code,rfp_listing_url,base_url").execute().data or []
    unmatched: list[str] = []
    for r in rows:
        url = r.get("rfp_listing_url") or r.get("base_url") or ""
        host = norm_host(url)
        did, dkey = _resolve_donor(r.get("donor_name"), r.get("donor_code"))
        if did is None:
            unmatched.append(f"catalogue: {r.get('donor_name')}")
        # source_uid is a DB-generated sequential bigint (migration 043) — never
        # written here. We only set the host join key + the donor link.
        patch = {"host": host or None, "donor_intel_id": did, "donor_key": dkey}
        print(f"  [cat] {str(host):38} donor={str(dkey)[:28]:28} {str(r.get('donor_name'))[:30]}")
        if not dry:
            sb.table("donor_sources").update(patch).eq("id", r["id"]).execute()
    return unmatched


def backfill_registry(sb, dry: bool) -> list[str]:
    rows = sb.table("source_registry").select(
        "host,donor_name,donor_code").execute().data or []
    unmatched: list[str] = []
    for r in rows:
        host = (r.get("host") or "").lower()
        host = host[4:] if host.startswith("www.") else host
        if not host:
            continue
        did, dkey = _resolve_donor(r.get("donor_name"), r.get("donor_code"), host)
        if did is None:
            unmatched.append(f"registry: {host}")
        patch = {"donor_intel_id": did, "donor_key": dkey}  # source_uid is DB-generated
        print(f"  [reg] {host:42} donor={str(dkey)[:28]:28} {str(r.get('donor_name'))[:30]}")
        if not dry:
            sb.table("source_registry").update(patch).eq("host", r["host"]).execute()
    return unmatched


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    sb = get_client()
    print(f"=== Backfill source UIDs + donor links {'(DRY RUN)' if dry else ''} ===")
    print("\n-- donor_sources --")
    u1 = backfill_catalogue(sb, dry)
    print("\n-- source_registry --")
    u2 = backfill_registry(sb, dry)
    unmatched = u1 + u2
    print(f"\nDone. {len(unmatched)} source(s) with NO donor_intel match:")
    for x in unmatched:
        print("   ?", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
