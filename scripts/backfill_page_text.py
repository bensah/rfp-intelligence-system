"""Backfill: re-fetch the call's OWN page for store rows that only hold a snippet.

Why: three gates (theme, closure, US-state) read the page, and a row whose
`raw_text` is a feed snippet starves all of them. Two rows sat in a single review
week for exactly this reason, and neither gate was wrong:

    AS-260918-111124   RSS gave 403 chars of "Calendar of Events" + a deadline.
                       The real page is 3794 chars and names the New York State
                       Department of Health, which the US-state gate rejects.
    AS-260918-111157   882 chars of search snippet. The real page is 4649 chars
                       and states a 2022-07-28 deadline, which the expiry gate
                       rejects.

The pipeline now captures the page BEFORE gating, so new rows arrive with it.
This is for rows already stored.

THE FLOOR IS TAKEN FROM THE DATA, not guessed. Stored `raw_text` length is
strongly bimodal - 315 rows under 500 chars, 301 over 6000, with a clear valley
around 2000 - so a row under the floor is a snippet or a fragment rather than a
short page.

SAFETY
  * Dry run by default; --apply writes.
  * Only ever LENGTHENS raw_text. A fetch that returns less than the row already
    holds is discarded, so a bot wall or an error template cannot erase evidence.
  * Writes raw_text ONLY. No deadline, no decision, no rescore - re-judging is
    the separate prune step, deliberately, so this stays reversible in effect.
  * Default order puts rows that back a LIVE pipeline row first, because those
    are the ones costing somebody review time.

Usage:
    python scripts/backfill_page_text.py                  # dry run
    python scripts/backfill_page_text.py --apply
    python scripts/backfill_page_text.py --apply --limit 50 --all-rows
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.dotenv_compat import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from core.auto_scorer import PAGE_TEXT_FLOOR  # noqa: E402
from core.catalog_synthesis import page_text  # noqa: E402
from core.http import get as http_get  # noqa: E402
from db.supabase_client import safe_execute, service_client  # noqa: E402

# One number, shared with the gate that decides whether to capture a page at
# scan time, so a backfilled row and a freshly-scanned one agree on what
# counts as having read the call.
SNIPPET_FLOOR = PAGE_TEXT_FLOOR
STORE_CAP = 20000         # what the store already holds for a full page
_PAGE = 500


def _rows_needing_a_page(sb, live_only: bool) -> list[dict]:
    """Store rows whose stored text is below the floor, snippet-first.

    `live_only` keeps it to rows that back a row currently in a tenant pipeline.
    """
    live_links = set()
    if live_only:
        start = 0
        while True:
            res = safe_execute(sb.table("rfp_submissions")
                               .select("opportunity_link")
                               .range(start, start + _PAGE - 1))
            batch = res.data or []
            live_links.update((r.get("opportunity_link") or "").strip()
                              for r in batch)
            if len(batch) < _PAGE:
                break
            start += _PAGE

    out: list[dict] = []
    start = 0
    while True:
        res = safe_execute(sb.table("extracted_solicitations")
                           .select("uid,opportunity_url,opportunity_name,raw_text,source")
                           .range(start, start + _PAGE - 1))
        batch = res.data or []
        for r in batch:
            url = (r.get("opportunity_url") or "").strip()
            if not url.lower().startswith("http"):
                continue
            if len(r.get("raw_text") or "") >= SNIPPET_FLOOR:
                continue
            if live_only and url not in live_links:
                continue
            out.append(r)
        if len(batch) < _PAGE:
            break
        start += _PAGE
    out.sort(key=lambda r: len(r.get("raw_text") or ""))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--all-rows", action="store_true",
                    help="include store rows that back no live pipeline row")
    args = ap.parse_args()

    sb = service_client()
    rows = _rows_needing_a_page(sb, live_only=not args.all_rows)
    if args.limit:
        rows = rows[:args.limit]
    scope = "every store row" if args.all_rows else "rows backing a live pipeline row"
    print(f"{scope}: {len(rows)} below {SNIPPET_FLOOR} chars of stored page text\n")
    if not rows:
        print("nothing to do")
        return 0

    grew, unchanged, failed, longer_already = 0, 0, 0, 0
    for r in rows:
        url, had = r["opportunity_url"], len(r.get("raw_text") or "")
        try:
            resp = http_get(url, timeout=30)
            code = getattr(resp, "status_code", 0)
            text = page_text(resp.text) if 200 <= code < 300 else ""
        except Exception as exc:
            print(f"  fetch failed  {had:>6} -> ---    {type(exc).__name__:22} {url[:58]}")
            failed += 1
            continue
        if not text:
            print(f"  HTTP {code:<4}     {had:>6} -> ---    {url[:66]}")
            failed += 1
            continue
        if len(text) <= had:
            # Never shorten. A bot wall or an error template must not erase what we
            # already have.
            longer_already += 1
            print(f"  not longer    {had:>6} -> {len(text):<6} kept old   {url[:52]}")
            continue
        print(f"  RECOVERED     {had:>6} -> {len(text):<6} "
              f"{(r.get('opportunity_name') or '')[:44]}")
        if args.apply:
            try:
                safe_execute(sb.table("extracted_solicitations")
                             .update({"raw_text": text[:STORE_CAP]})
                             .eq("uid", r["uid"]))
                grew += 1
            except Exception as exc:
                print(f"    write failed: {type(exc).__name__}: {exc}")
                failed += 1
        else:
            unchanged += 1

    print(f"\n{'WROTE' if args.apply else 'WOULD WRITE'}: "
          f"{grew if args.apply else unchanged} row(s) lengthened; "
          f"{longer_already} already as long or longer; {failed} unreadable")
    if not args.apply:
        print("DRY RUN - nothing written. Re-run with --apply.")
    else:
        print("Next: scripts/prune_ineligible_screened.py to re-judge on the "
              "recovered pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
