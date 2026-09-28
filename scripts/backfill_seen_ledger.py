"""Backfill: a tombstone in rfp_seen for every row that has ever been in the pipeline.

Why: the ledger is the ONLY thing that stops a call returning after its
rfp_submissions row is deleted. A row that still exists is caught by the live
deduplicator; a DELETED one is remembered only here. The owner's standing
requirement is that a call already decided - declined, parked or proceeded -
never comes back for review.

The ingest path does tombstone every insert (scan_pipeline calls
seen_ledger.record_one right after the insert) and it works: every auto row from
the recent scans is tombstoned, 4/4, 5/5, 12/12. The gap is HISTORICAL. Migrations
056 and 059 renamed two columns on rfp_submissions but not on rfp_seen, so for a
long stretch every ledger read AND write raised Postgres 42703 - and both call
sites caught it and carried on, so the scan reported success while recording
nothing (see core/seen_ledger.py, which now resolves the spelling at runtime).

Rows written during that stretch have no tombstone. Measured on the live
database: 221 of 314 pipeline rows, including calls that were declined by hand.

WHAT THIS DOES NOT DO
  * It does not delete, close, rescore or re-decide anything.
  * It does not touch a row that already has a tombstone (upsert on uid, and rows
    already present are skipped so an existing `reason` is preserved - a
    'human_decline' tombstone must not be downgraded to a bookkeeping one).
  * It writes only the dedup projection seen_ledger already defines, so a
    backfilled tombstone and a freshly-written one are identical by construction.

Usage:
    python scripts/backfill_seen_ledger.py            # dry-run, report only
    python scripts/backfill_seen_ledger.py --apply    # write the tombstones
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dotenv_compat import load_dotenv  # noqa: E402

load_dotenv()

from core import seen_ledger  # noqa: E402
from db.supabase_client import safe_execute, service_client  # noqa: E402

# Read across every tenant: the ledger is a platform-wide record of "we have seen
# this", not tenant data, and the scan consults it before it knows a tenant.
_PAGE = 500


def _all_pipeline_rows(sb) -> list[dict]:
    cols = ("uid,opportunity_id,opportunity_title,opportunity_link,funding_agency,"
            "call_submission_deadline,call_award_value,donor_decision,tenant_id")
    out: list[dict] = []
    start = 0
    while True:
        res = safe_execute(sb.table("rfp_submissions").select(cols)
                           .range(start, start + _PAGE - 1))
        rows = res.data or []
        out.extend(rows)
        if len(rows) < _PAGE:
            return out
        start += _PAGE


def rows_needing_a_tombstone(rows, existing_uids) -> list[dict]:
    """Pipeline rows with no tombstone yet.

    Skipping rows that already have one is not just an optimisation: an upsert
    would overwrite `reason`, and a 'human_decline' tombstone - the record that a
    person looked at this call and said no - must not be downgraded to a
    bookkeeping one.
    """
    have = {u for u in (existing_uids or set()) if u}
    seen_in_batch: set[str] = set()
    out: list[dict] = []
    for r in rows or []:
        uid = (r or {}).get("uid")
        if not uid or uid in have or uid in seen_in_batch:
            continue
        seen_in_batch.add(uid)
        out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the tombstones (default is a dry run)")
    ap.add_argument("--reason", default="backfill_history",
                    help="reason recorded on the new tombstones")
    args = ap.parse_args()

    sb = service_client()
    rows = _all_pipeline_rows(sb)
    print(f"pipeline rows: {len(rows)}")

    existing = {r.get("uid") for r in seen_ledger.fetch_all() if r.get("uid")}
    print(f"already tombstoned: {len(existing)}")

    missing = rows_needing_a_tombstone(rows, existing)
    print(f"MISSING a tombstone: {len(missing)}\n")
    if not missing:
        print("nothing to do")
        return 0

    by_decision: dict[str, int] = {}
    for r in missing:
        key = (r.get("donor_decision") or "(blank)")
        by_decision[key] = by_decision.get(key, 0) + 1
    print("by donor decision:")
    for k, n in sorted(by_decision.items(), key=lambda kv: -kv[1]):
        print(f"   {n:5}  {k}")

    print("\nfirst 10 that would be written:")
    for r in missing[:10]:
        print(f"   {r['uid']:22} {(r.get('funding_agency') or '-')[:24]:26} "
              f"{(r.get('opportunity_title') or '')[:46]}")

    if not args.apply:
        print(f"\nDRY RUN - nothing written. Re-run with --apply to record "
              f"{len(missing)} tombstone(s).")
        return 0

    written = 0
    for i in range(0, len(missing), 200):
        written += seen_ledger.record(missing[i:i + 200], reason=args.reason)
    print(f"\nrecorded {written} tombstone(s) with reason={args.reason!r}")
    if written != len(missing):
        print(f"WARNING: {len(missing) - written} were not recorded - see the log "
              f"for the seen_ledger warning explaining why")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
