"""Backfill donor_intel.founded (year of creation) where it's MISSING, using
the curated partner table in core.partners.

Matches a donor to the partner list by donor_short (acronym) first, then donor
(full name), case-insensitive. Only fills rows whose `founded` is empty — never
overwrites an existing value. Dry-run by default; --commit to write.

  python scripts/update_donor_founded.py            # preview
  python scripts/update_donor_founded.py --commit   # write
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.partners import PARTNER_FOUNDED          # noqa: E402
from db.supabase_client import get_client          # noqa: E402


def _missing(v) -> bool:
    if v in (None, "", 0, "0"):
        return True
    return isinstance(v, str) and not v.strip()


def main(commit: bool) -> int:
    sb = get_client()
    rows = (sb.table("donor_intel")
            .select("canonical_key, donor, donor_short, founded")
            .execute().data or [])
    updates = []
    for r in rows:
        if not _missing(r.get("founded")):
            continue
        acr = (r.get("donor_short") or "").strip().lower()
        name = (r.get("donor") or "").strip().lower()
        yr = PARTNER_FOUNDED.get(acr) or PARTNER_FOUNDED.get(name)
        if yr:
            updates.append((r.get("canonical_key"), r.get("donor"), yr))

    print(f"{len(rows)} donors; {len(updates)} with a missing 'founded' matched "
          f"to the partner table:")
    for _ck, dn, yr in updates:
        print(f"  {yr}  {dn}")
    if not updates:
        print("Nothing to update.")
        return 0
    if not commit:
        print("\nDRY RUN — re-run with --commit to write.")
        return 0

    n = 0
    for ck, dn, yr in updates:
        try:
            sb.table("donor_intel").update({"founded": yr}).eq(
                "canonical_key", ck).execute()
            n += 1
        except Exception as exc:
            print(f"  warn: {dn}: {exc}")
    print(f"\nUpdated {n} donors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(commit="--commit" in sys.argv))
