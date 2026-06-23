"""One-off backfill for the auto-reject log (scan_decisions, system_reject):

  1. DEDUP — collapse rows that are the same opportunity (same normalised link, or
     same title+funder), keeping the OLDEST and deleting the rest. Matches the
     live dedup now in decision_log.log_rejects.
  2. SOLICITATION — fill solicitation_type so the Verify list comes ready-labelled:
     'Other' for not-an-rfp rejects, else the type detected from the title/link
     (NOFO/RFP/CFP/…), keeping any value already set.

Dry-run by default; --commit applies (deletes dupes + updates survivors).

    python scripts/backfill_reject_dedup_solicitation.py            # dry-run
    python scripts/backfill_reject_dedup_solicitation.py --commit
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import type_detect                                  # noqa: E402
from core.decision_log import _reject_keys, _reason_category  # noqa: E402
from db.supabase_client import get_client                     # noqa: E402


def main(argv: list[str]) -> int:
    commit = "--commit" in argv
    sb = get_client()
    # Paginate — PostgREST caps a single response at 1000 rows. Oldest first so
    # the OLDEST of any dup group is the survivor.
    rows: list = []
    step = 1000
    while True:
        page = (sb.table("scan_decisions")
                .select("id,created_at,opportunity_link,opportunity_title,"
                        "funding_agency,reason,solicitation_type")
                .eq("event_type", "system_reject")
                .order("created_at").range(len(rows), len(rows) + step - 1)
                .execute().data or [])
        rows.extend(page)
        if len(page) < step:
            break
    print(f"=== reject backfill {'(COMMIT)' if commit else '(DRY RUN)'} — "
          f"{len(rows)} system_reject rows ===")

    seen_link: set[str] = set()
    seen_id: set[str] = set()
    delete_ids: list = []
    updates: list[tuple] = []   # (id, solicitation_type)
    for r in rows:
        lk, idk = _reject_keys(r.get("opportunity_link") or "",
                               r.get("opportunity_title") or "",
                               r.get("funding_agency") or "")
        if (lk and lk in seen_link) or (idk and idk in seen_id):
            delete_ids.append(r["id"])           # duplicate of an older row
            continue
        if lk:
            seen_link.add(lk)
        if idk:
            seen_id.add(idk)
        # solicitation backfill for the survivor
        if _reason_category(r.get("reason")) == "not-an-rfp":
            sol = "Other"
        else:
            sol = r.get("solicitation_type") or type_detect.detect_solicitation(r)
        if sol and sol != r.get("solicitation_type"):
            updates.append((r["id"], sol))

    print(f"  duplicates to delete: {len(delete_ids)}")
    print(f"  survivors to (re)label solicitation: {len(updates)}")
    if not commit:
        print("\nDry-run. Re-run with --commit to apply.")
        return 0

    for i in range(0, len(delete_ids), 100):
        sb.table("scan_decisions").delete().in_("id", delete_ids[i:i + 100]).execute()
    for rid, sol in updates:
        sb.table("scan_decisions").update({"solicitation_type": sol}).eq("id", rid).execute()
    print(f"\n✓ Deleted {len(delete_ids)} dupes; labelled {len(updates)} survivors. "
          f"{len(rows) - len(delete_ids)} unique rejects remain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
