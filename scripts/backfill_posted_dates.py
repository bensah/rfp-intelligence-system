"""Backfill: recover extracted_solicitations.date_posted from the row's own raw_text.

Why: the stale-posting rule (auto_scorer.deadline_in_future +
insufficient_data_reject) is the ONLY evidence-based way to retire an undated call, and
it needs `date_posted`. Donor-catalogue rows were stored with that column NULL even
though the date is sitting in the same row's `raw_text`, usually unlabelled under the
headline ("17/10/2025"). With it NULL the rule cannot fire, the row stays
funding_status='Open' forever, and every re-screen re-admits it — which is exactly how
the Fondation Pierre Fabre / ODESS 2026 call came back week after week nearly a year
after its window closed on 2025-11-07.

The date is extracted with deadline_extract.extract_posted_date — the same function the
scan pipeline's posting-date backstop uses, so a backfilled row and a freshly-crawled one
carry the same value by construction.

SAFETY
  * BLANK-ONLY: rows that already have a date_posted are never touched.
  * Never writes `deadline` — a posting date is not a submission deadline, and writing
    one there would publish a date the funder never stated.
  * Future-dated and absurdly old results are rejected as parse errors.
  * Does not delete, close or rescore anything. Closing stale rows is the separate,
    already-existing step below, run only with --close-stale.

Usage:
    python scripts/backfill_posted_dates.py                  # dry-run (report only)
    python scripts/backfill_posted_dates.py --apply          # write date_posted
    python scripts/backfill_posted_dates.py --apply --close-stale
                                                             # then retire undated rows
                                                             # older than the stale window
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from core import deadline_extract, extracted_store              # noqa: E402
from db.supabase_client import service_client, safe_execute     # noqa: E402

_TABLE = "extracted_solicitations"
# A posting date before this is a parse artefact (a copyright year, a stray "2009" in a
# history section), not a real publication date for a call we are still screening.
_MIN_YEAR = 2015


def _plausible(iso: str, today: date) -> tuple[bool, str]:
    """A recovered posting date must be in the past and not absurdly old."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (ValueError, TypeError):
        return False, "unparseable"
    if d > today:
        return False, f"future ({d.isoformat()})"
    if d.year < _MIN_YEAR:
        return False, f"implausibly old ({d.isoformat()})"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write date_posted (default: dry-run)")
    ap.add_argument("--close-stale", action="store_true",
                    help="after backfilling, run extracted_store.mark_closed_* to retire "
                         "past-deadline and stale undated rows (requires --apply)")
    ap.add_argument("--status", default="Open",
                    help="only rows with this funding_status (default: Open; "
                         "'*' for all)")
    ap.add_argument("--limit", type=int, default=0, help="cap rows examined (0 = no cap)")
    args = ap.parse_args(argv)

    today = date.today()
    sb = service_client()
    q = sb.table(_TABLE).select(
        "uid,opportunity_name,opportunity_url,raw_text,date_posted,deadline,funding_status"
    ).is_("date_posted", "null")
    if args.status != "*":
        q = q.eq("funding_status", args.status)
    if args.limit:
        q = q.limit(args.limit)
    rows = safe_execute(q).data or []

    print(f"{len(rows)} row(s) with date_posted NULL"
          + (f" and funding_status='{args.status}'" if args.status != "*" else "")
          + f"   [{'APPLY' if args.apply else 'DRY-RUN'}]\n")

    recovered: list[tuple[str, str, str, str]] = []
    no_text = rejected = no_date = 0
    for r in rows:
        raw = r.get("raw_text") or ""
        if not raw.strip():
            no_text += 1
            continue
        try:
            iso, how = deadline_extract.extract_posted_date(
                raw, title=r.get("opportunity_name") or "")
        except Exception as exc:
            print(f"  ! {r['uid']}: extractor raised {exc!r}")
            continue
        if not iso:
            no_date += 1
            continue
        ok, why = _plausible(iso, today)
        if not ok:
            rejected += 1
            print(f"  - {r['uid']}: rejected {iso} — {why}")
            continue
        recovered.append((r["uid"], iso, how, (r.get("opportunity_name") or "")[:58]))

    for uid, iso, how, name in recovered:
        age = (today - date.fromisoformat(iso)).days
        print(f"  {iso}  ({age:4d}d, {how:<12})  {uid}  {name}")

    print(f"\n  recovered      : {len(recovered)}")
    print(f"  no raw_text    : {no_text}   (nothing to read — needs a re-crawl)")
    print(f"  no date found  : {no_date}")
    print(f"  implausible    : {rejected}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply.")
        return 0

    written = 0
    for uid, iso, _how, _name in recovered:
        try:
            safe_execute(sb.table(_TABLE).update({"date_posted": iso}).eq("uid", uid))
            written += 1
        except Exception as exc:
            print(f"  ! {uid}: write failed — {exc}")
    print(f"\nWrote date_posted on {written} row(s).")

    if args.close_stale:
        today_iso = today.isoformat()
        past = extracted_store.mark_closed_past_deadline(today_iso)
        stale = extracted_store.mark_closed_stale_undated(today_iso)
        posted = extracted_store.mark_closed_stale_posted(today_iso)
        print(f"Closed {past} past-deadline · {stale} stale undated · "
              f"{posted} stale posted row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
