"""Fill the §4 catalogue fields that have never had a writer. DRY RUN BY DEFAULT.

    python -m scripts.backfill_catalog_synthesis                 # dry run, 10 rows
    python -m scripts.backfill_catalog_synthesis --limit 50      # dry run, 50 rows
    python -m scripts.backfill_catalog_synthesis --limit 50 --apply
    python -m scripts.backfill_catalog_synthesis --fetch-html --limit 20 --apply

Nine columns are blank on every one of 686 rows because nothing ever wrote them, not
because extraction failed. `core.catalog_synthesis` reads the `raw_text` already stored
(620 rows carry it, ~3k characters each) and produces them.

ONLY BLANK COLUMNS ARE WRITTEN. A field already holding a value — including one a human
corrected — is never touched, so this is safe to re-run and safe to stop half way.

--fetch-html is what `attachments` / `resource_links` need. `raw_text` is the page's TEXT,
so its links are gone; recovering the documents means re-fetching the page. That is a
network request per row, so it is opt-in and bounded, and without it those two columns stay
empty (which the summary states rather than hides).

The free tier is the constraint worth watching: one model call per row, ~11s each measured
against Ollama Cloud's gpt-oss:120b. 100 rows is roughly 18 minutes of wall clock. Run it in
batches with --limit and watch the per-field yield in the summary.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_env() -> None:
    """Read .env without python-dotenv (its find_dotenv() trips over a piped stdin)."""
    path = os.path.join(_ROOT, ".env")
    if not os.path.exists(path):
        return
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _fetch(url: str) -> str | None:
    try:
        import requests
        from core.scraper import USER_AGENT, HTTP_TIMEOUT
        r = requests.get(url, headers={"User-Agent": USER_AGENT},
                         timeout=HTTP_TIMEOUT, allow_redirects=True)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--include-expired", action="store_true",
                    help="also synthesise calls whose deadline has passed (default: skip "
                         "them — nobody can bid on a closed call)")
    ap.add_argument("--screened", action="store_true",
                    help="only calls this tenant has ingested into its pipeline")
    ap.add_argument("--decided", action="store_true",
                    help="only calls with a recorded Proceed / Park / Decline")
    ap.add_argument("--redo", action="store_true",
                    help="re-ask rows that already have an answer (use after a prompt "
                         "change); by default those are skipped so a run advances")
    ap.add_argument("--apply", action="store_true",
                    help="write to the database (default is a dry run)")
    ap.add_argument("--fetch-html", action="store_true",
                    help="re-fetch each call page so attachments/resource_links can be "
                         "recovered — one network request per row")
    ap.add_argument("--uid", help="a single row, for spot checks")
    args = ap.parse_args()

    _load_env()
    from db.supabase_client import service_client
    from core import catalog_synthesis as CS
    from core import opportunity_detail as _od

    sb = service_client()

    def _blank(v):
        if v is None:
            return True
        if isinstance(v, (list, tuple, dict)):
            return len(v) == 0
        return str(v).strip() in ("", "[]", "{}")

    # A ROW IS "ALREADY ASKED" IF ANY HIGH-YIELD FIELD CAME BACK.
    #
    # The selector used to be "any field still blank", which meant every completed row
    # re-qualified for ever: what_is_not_funded returns on 14% of rows, attachments on 4%,
    # eligibility_countries on 2%, so those stay blank on nearly everything. A second visit
    # spends a call to re-attempt exactly the fields the model already declined — observed
    # live as 7 calls buying 2 fields, on rows finished in the previous batch.
    #
    # These three come back on 94-100% of rows, so one of them present means the model has
    # read this page. --redo forces a re-ask when the prompt has changed and you WANT that.
    _ASKED = ("full_description", "applicant_fit_profile", "what_is_funded")

    def _already_asked(r):
        return any(not _blank(r.get(f)) for f in _ASKED)

    # ORDERED, so consecutive runs advance through the catalogue instead of re-reading
    # whatever the database happened to return first.
    q = sb.table("extracted_solicitations").select("*").order("uid")
    if args.uid:
        q = q.eq("uid", args.uid)
    rows = q.limit(5000).execute().data or []

    # DON'T SPEND A CALL ON A CLOSED CALL. Nobody can bid on it, so an overview for it is pure
    # cost: 78 of the 357 outstanding rows are expired — 22% of the work, removed by default
    # rather than hidden behind a flag.
    from datetime import date as _date
    _today = _date.today()

    def _expired(r):
        if str(r.get("funding_status") or "").strip().lower() == "closed":
            return True
        d = str(r.get("deadline") or "")[:10]
        if not d:
            return False            # unknown is not the same as passed
        try:
            return _date.fromisoformat(d) < _today
        except ValueError:
            return False

    # Optional narrowing to what the tenant has taken an interest in. Measured live, from 357
    # outstanding rows:
    #     default (live only)      279   ~93 min
    #     --screened                81   ~27 min   in the pipeline at all
    #     --decided                  8   ~3 min    Proceed / Park / Decline recorded
    # --decided is that small because only 73 pipeline rows carry a decision and most were
    # already synthesised. It leaves every call a reviewer might BROWSE unsynthesised —
    # including the whole Live Opportunity Feed, which is where an unscreened call gets read.
    _pipe_links: set = set()
    if args.screened or args.decided:
        _dec = {"proceed", "park", "decline"}
        for srow in (sb.table("rfp_submissions").select("opportunity_link,decision")
                     .limit(5000).execute().data or []):
            link = _od.normalise_link(srow.get("opportunity_link"))
            if not link:
                continue
            if args.decided and str(srow.get("decision") or "").strip().lower() not in _dec:
                continue
            _pipe_links.add(link)

    # A THIN ROW IS ONLY THIN UNTIL WE FETCH IT. The stored raw_text is the brief for any row
    # discovered from a listing (core/extract.py falls back to it when no page was fetched),
    # so 247 rows look unreadable while their own call page carries thousands of characters —
    # measured, 6 of 8 sampled went from under 400 to between 1,300 and 5,200.
    #
    # So with --fetch-html the thin rows are CANDIDATES, not exclusions: the fetch happens
    # first and `synthesize_row` still declines to spend a model call if the page turns out to
    # be as empty as the stored text (an aggregator paywall stub, which is the other two of
    # the eight). Without --fetch-html there is nothing to improve them with, so they are
    # skipped as before.
    with_text = [r for r in rows
                 if str(r.get("raw_text") or "").strip() or
                 (args.fetch_html and r.get("opportunity_url"))]
    thin = ([] if args.fetch_html
            else [r for r in with_text if len(str(r.get("raw_text") or "")) < CS._MIN_TEXT])
    readable = [r for r in with_text if r not in thin]
    done = [r for r in readable if _already_asked(r)]
    pending = readable if args.redo else [r for r in readable if not _already_asked(r)]
    _expired_n = len([r for r in pending if _expired(r)])
    if not args.include_expired:
        pending = [r for r in pending if not _expired(r)]
    _before_scope = len(pending)
    if args.screened or args.decided:
        pending = [r for r in pending
                   if _od.normalise_link(r.get("opportunity_url")) in _pipe_links]
    todo = pending[:args.limit]

    print(f"model      : {CS._model()}")
    print(f"mode       : {'APPLY (writes)' if args.apply else 'DRY RUN (no writes)'}")
    print(f"fetch html : {args.fetch_html}"
          + ("   REDO: re-asking rows already answered" if args.redo else ""))
    print(f"catalogue  : {len(rows)} rows")
    print(f"  no text / boilerplate only : {len(rows) - len(readable)}"
          + (f"  (under {CS._MIN_TEXT} chars — no call spent)" if not args.fetch_html
             else "  (no URL to fetch)"))
    if args.fetch_html:
        _thin_now = len([r for r in readable
                         if len(str(r.get("raw_text") or "")) < CS._MIN_TEXT])
        print(f"  thin, will be re-fetched   : {_thin_now}"
              "  (the page usually carries far more than the stored brief)")
    print(f"  already synthesised        : {len(done)}")
    print(f"  expired / closed           : {_expired_n}"
          + ("  (INCLUDED)" if args.include_expired else "  (skipped — nobody can bid)"))
    if args.screened or args.decided:
        print(f"  outside the chosen scope   : {_before_scope - len(pending)}"
              f"  ({'decided P/P/D only' if args.decided else 'in the pipeline only'})")
    print(f"  still to do                : {len(pending)}")
    print(f"this batch : {len(todo)}")
    print()
    if not todo:
        print("Nothing to do.")
        return 0

    filled = {f: 0 for f in CS.ALL_FIELDS}
    written = failed = deeper = 0
    t_start = time.time()
    for i, r in enumerate(todo, 1):
        html = _fetch(r.get("opportunity_url") or "") if args.fetch_html else None
        t0 = time.time()
        try:
            got = CS.synthesize_row(r, html=html)
        except Exception as exc:
            failed += 1
            print(f"  {i:3d}. {r['uid'][:24]}  FAILED {type(exc).__name__}: {exc}")
            continue
        # raw_text is PROVENANCE, not one of the schema fields being filled: it is the source
        # text this run recovered, saved so the next pass does not re-fetch the same page. It
        # is reported on its own line rather than inflating the field yield.
        _deeper = "raw_text" in got
        if _deeper:
            deeper += 1
        _named = sorted(f for f in got if f != "raw_text")
        for f in _named:
            filled[f] = filled.get(f, 0) + 1
        print(f"  {i:3d}. {r['uid'][:24]}  {time.time() - t0:5.1f}s  "
              f"{len(_named)} field(s): {', '.join(_named) or '-'}"
              + ("   [source text recovered]" if _deeper else ""))
        if got and args.apply:
            try:
                sb.table("extracted_solicitations").update(got).eq("uid", r["uid"]).execute()
                written += 1
            except Exception as exc:
                failed += 1
                print(f"       write failed: {exc}")

    n = len(todo)
    print(f"\nelapsed {time.time() - t_start:.0f}s  ({(time.time() - t_start) / n:.1f}s per row)")
    print(f"LLM calls {CS.calls_made()}   rows written {written}   failures {failed}"
          f"   thin-skipped mid-run {CS.skipped_thin()}"
          f"   overviews discarded as padded {CS.padded_overviews()}")
    if args.fetch_html:
        print(f"source text recovered on {deeper} row(s) — saved to raw_text, so the next "
              "pass and every gate downstream read the fuller call")
    print("\nPER-FIELD YIELD")
    for f in (f for f in CS.ALL_FIELDS if f != "raw_text"):
        c = filled.get(f, 0)
        print(f"  {f:24s} {c:3d}/{n}  {100 * c / n:3.0f}%")
    if not args.fetch_html and (filled.get("attachments", 0) == 0
                                and filled.get("resource_links", 0) == 0):
        print("\nattachments / resource_links are 0% because --fetch-html was not set: "
              "raw_text is the page's TEXT, so its links are already gone.")
    if not args.apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
