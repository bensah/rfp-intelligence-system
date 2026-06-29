"""Backfill is_duplicate / duplicate_of_uid on existing rfp_submissions rows.

For each row that's currently flagged is_duplicate=false, run the same
deduplicator the live Submit form uses. If a match group is found, the
EARLIEST `submitted_at` becomes canonical and the others are marked
is_duplicate=true with duplicate_of_uid pointing at it.

    python scripts/dedup_existing.py --dry-run     # show what would change
    python scripts/dedup_existing.py               # actually update
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.deduplicator import find_duplicates  # noqa: E402
from db.supabase_client import get_client  # noqa: E402


def run(*, dry_run: bool = False, reset: bool = False,
        preserve_completed: bool = True) -> dict:
    """Programmatic entry point. Returns summary dict.

    Args:
        dry_run: don't write to DB, just report.
        reset: unflag all duplicates before re-running (clears prior decisions).
        preserve_completed: skip flagging a pair when BOTH rows have
            progress_status='Completed' — they represent real donor submissions
            (possibly intentional multi-submissions of the same RFP) and
            shouldn't be auto-merged.
    """
    sb = get_client()

    if reset and not dry_run:
        # Clear all existing duplicate flags before re-running
        sb.table("rfp_submissions").update(
            {"is_duplicate": False, "duplicate_of_uid": None}
        ).neq("uid", "").execute()

    res = (
        sb.table("rfp_submissions")
        .select(
            "uid,opportunity_title,opportunity_link,funding_agency,"
            "call_submission_deadline,call_award_value,submitted_at,search_date,"
            "progress_status,decision,donor_decision,stage,"
            "amount_requested,date_completed,submissions,"
            "is_duplicate,duplicate_of_uid"
        )
        .order("search_date")
        .execute()
    )
    rows: list[dict[str, Any]] = res.data or []
    if not rows:
        return {"flagged": 0, "skipped_completed": 0, "considered": 0, "updates": []}

    # Reset in-memory flags too (because we just cleared them in DB if reset=True)
    if reset:
        for r in rows:
            r["is_duplicate"] = False
            r["duplicate_of_uid"] = None

    canonical_pool = [r for r in rows if not r.get("is_duplicate")]
    seen: set[str] = set()
    updates: list[tuple[str, str, str]] = []
    skipped_completed = 0

    def _is_completed(r: dict) -> bool:
        return (r.get("progress_status") or "").strip().lower() == "completed"

    # Stage ranking: later stages win as canonical (earliest → 1, last → 7).
    STAGE_RANK = {
        "identification & screening": 1,
        "go/no-go decision & bid planning": 2,
        "proposal development": 3,
        "budgeting development": 4,
        "review, compliance check & approvals": 5,
        "final packaging & submission": 6,
        "post-submission follow-up": 7,
    }

    def _completeness_score(r: dict) -> int:
        """Higher = keep this row as canonical when its cluster collapses."""
        score = 0
        ps = (r.get("progress_status") or "").strip().lower()
        if ps == "completed":
            score += 1000
        elif ps == "in progress":
            score += 50
        elif ps == "not started":
            score += 5

        dd = (r.get("donor_decision") or "").strip().lower()
        if dd == "approved":
            score += 500
        elif dd in ("under review", "not approved"):
            score += 200

        dec = (r.get("decision") or "").strip().lower()
        if dec.startswith("proceed"):
            score += 100

        # Stage tiebreaker — later stage wins (+10 to +70)
        stage = (r.get("stage") or "").strip().lower()
        score += STAGE_RANK.get(stage, 0) * 10

        if r.get("amount_requested"):
            score += 20
        if r.get("date_completed"):
            score += 20
        sub = r.get("submissions") or 0
        try:
            if float(sub) > 1:
                score += 30
        except (TypeError, ValueError):
            pass
        return score

    # Process by completeness DESC so the strongest canonical claims its
    # cluster first; ties broken by search_date ASC (earliest finder).
    ordered = sorted(
        canonical_pool,
        key=lambda r: (
            -_completeness_score(r),
            r.get("search_date") or r.get("submitted_at") or "",
        ),
    )

    for r in ordered:
        if r["uid"] in seen:
            continue
        # Compare r against every row not already claimed
        others = [x for x in canonical_pool
                  if x["uid"] != r["uid"] and x["uid"] not in seen]
        matches = find_duplicates(r, others)
        if not matches:
            seen.add(r["uid"])
            continue
        # r wins canonical (highest completeness in this iteration).
        # Flag matches as duplicates, EXCEPT Completed rows (preserved as
        # real submission events even when they match).
        for m in matches:
            if m["uid"] in seen:
                continue
            if preserve_completed and _is_completed(m):
                skipped_completed += 1
                continue
            seen.add(m["uid"])
            updates.append((m["uid"], r["uid"], m.get("_reason", "match")))
        seen.add(r["uid"])

    if not dry_run:
        for uid, canon, _reason in updates:
            sb.table("rfp_submissions").update(
                {"is_duplicate": True, "duplicate_of_uid": canon}
            ).eq("uid", uid).execute()

    return {
        "flagged": len(updates),
        "skipped_completed": skipped_completed,
        "considered": len(canonical),
        "total_rows": len(rows),
        "updates": updates,
        "reset": reset,
        "dry_run": dry_run,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="Unflag all duplicates before re-running")
    ap.add_argument("--no-preserve-completed", action="store_true",
                    help="Flag pairs even when both have progress=Completed "
                         "(default: preserve, treat as real submissions)")
    args = ap.parse_args()

    result = run(
        dry_run=args.dry_run,
        reset=args.reset,
        preserve_completed=not args.no_preserve_completed,
    )
    print(f"Considered {result['considered']} canonical row(s) of {result['total_rows']} total.")
    if result["reset"]:
        print("Reset: cleared all prior duplicate flags before re-running.")
    if result["skipped_completed"]:
        print(f"Skipped {result['skipped_completed']} pair(s) where both rows are Completed "
              "(treated as real submissions).")
    if not result["updates"]:
        print("No duplicates detected. Nothing to do.")
        return
    print(f"\nWould mark {len(result['updates'])} row(s) as duplicates:")
    for uid, canon, reason in result["updates"]:
        print(f"  {uid:18}  -> dup of {canon:18}  ({reason})")
    if not args.dry_run:
        print(f"\nFlagged {len(result['updates'])} row(s).")
    else:
        print("\nDry run — no writes performed.")


if __name__ == "__main__":
    main()
