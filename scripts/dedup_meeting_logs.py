"""De-duplicate migration-sourced meeting_logs.

Repeated Excel "Sync now" runs — especially before MID existed, and across the
encoding-crash partial runs — left duplicate meeting_logs: the SAME meeting
inserted several times under different derived keys, so the MID-based merge
never matched them. This collapses each set of duplicates down to a single
row, preferring a RESOLVED copy so an in-app "Resolved" decision is never lost.

How a duplicate is detected: rows are grouped by their natural meeting
identity — meeting_date + (rfp_uid, or donor_title when there's no RFP) + the
normalised ACTIONS text — computed from the row's own content, so copies group
together regardless of what stored external_id each happens to carry. Actions
text is part of the key so two DISTINCT actions for the same meeting (same date
+ donor) are never merged into one — only true duplicates collapse. This mirrors
the merge key in migrate_excel.py (the sync's own preservation logic).

Scope: ONLY source='migration' rows. In-app notes (source IS NULL) are never
touched. Dry-run by default — pass --apply to actually delete.

    python scripts/dedup_meeting_logs.py             # preview (no deletes)
    python scripts/dedup_meeting_logs.py --apply      # delete the duplicates
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for _s in (sys.stdout, sys.stderr):  # UTF-8 so status glyphs never crash on Windows
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from db.supabase_client import get_client  # noqa: E402


def _natural_key(meeting_date, donor_title, rfp_uid, actions=None) -> str:
    """Content-derived ACTION identity — mirrors migrate_excel._content_key: the
    meeting identity (date + rfp_uid/donor) PLUS the normalised actions text, so
    distinct actions for one meeting are never collapsed into each other."""
    nk = (str(rfp_uid).strip() if rfp_uid else "")
    if not nk:
        nk = (str(donor_title).strip().lower() if donor_title else "")
    base = hashlib.md5(f"{str(meeting_date or '')[:10]}|{nk}".encode("utf-8")).hexdigest()[:16]
    a = (str(actions).strip().lower() if actions else "")
    return base + ":" + hashlib.md5(a.encode("utf-8")).hexdigest()[:8]


def _survivor_first(rows: list[dict]) -> list[dict]:
    """Order so the row to KEEP is first: prefer resolved, then a stable id."""
    return sorted(rows, key=lambda r: (0 if r.get("is_resolved") else 1, str(r.get("id"))))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete duplicates (default: dry-run preview)")
    args = ap.parse_args()

    sb = get_client()
    rows = (
        sb.table("meeting_logs")
        .select("id,meeting_date,donor_title,rfp_uid,is_resolved,external_id,actions")
        .eq("source", "migration")
        .execute()
        .data
        or []
    )
    print(f"Fetched {len(rows)} migration meeting_logs rows "
          "(in-app notes with no source are excluded).")

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[_natural_key(r.get("meeting_date"), r.get("donor_title"),
                            r.get("rfp_uid"), r.get("actions"))].append(r)

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"{len(groups)} distinct meetings · {len(dup_groups)} have duplicates.\n")

    to_delete: list[str] = []
    for members in dup_groups.values():
        ordered = _survivor_first(members)
        keep, losers = ordered[0], ordered[1:]
        to_delete.extend(str(x["id"]) for x in losers)
        print(f"- {keep.get('meeting_date')} · {keep.get('donor_title')!r}: "
              f"{len(members)} copies -> keep {str(keep['id'])[:8]} "
              f"(resolved={bool(keep.get('is_resolved'))}), delete {len(losers)}")

    print(f"\nTotal duplicate rows to delete: {len(to_delete)}")
    if not to_delete:
        print("Nothing to do.")
        return
    if not args.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply to delete.")
        return

    for i in range(0, len(to_delete), 100):
        sb.table("meeting_logs").delete().in_("id", to_delete[i:i + 100]).execute()
    print(f"Deleted {len(to_delete)} duplicate row(s). Done.")


if __name__ == "__main__":
    main()
