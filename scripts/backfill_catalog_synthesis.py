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

    sb = service_client()
    q = sb.table("extracted_solicitations").select("*")
    if args.uid:
        q = q.eq("uid", args.uid)
    rows = q.limit(max(args.limit * 4, 40)).execute().data or []

    def _blank(v):
        if v is None:
            return True
        if isinstance(v, (list, tuple, dict)):
            return len(v) == 0
        return str(v).strip() in ("", "[]", "{}")

    # Rows worth spending a call on: some text to read, and something still missing.
    todo = [r for r in rows
            if str(r.get("raw_text") or "").strip()
            and any(_blank(r.get(f)) for f in CS.ALL_FIELDS)][:args.limit]

    print(f"model      : {CS._model()}")
    print(f"mode       : {'APPLY (writes)' if args.apply else 'DRY RUN (no writes)'}")
    print(f"fetch html : {args.fetch_html}")
    print(f"candidates : {len(todo)} of {len(rows)} scanned\n")
    if not todo:
        print("Nothing to do.")
        return 0

    filled = {f: 0 for f in CS.ALL_FIELDS}
    written = failed = 0
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
        for f in got:
            filled[f] = filled.get(f, 0) + 1
        print(f"  {i:3d}. {r['uid'][:24]}  {time.time() - t0:5.1f}s  "
              f"{len(got)} field(s): {', '.join(sorted(got)) or '-'}")
        if got and args.apply:
            try:
                sb.table("extracted_solicitations").update(got).eq("uid", r["uid"]).execute()
                written += 1
            except Exception as exc:
                failed += 1
                print(f"       write failed: {exc}")

    n = len(todo)
    print(f"\nelapsed {time.time() - t_start:.0f}s  ({(time.time() - t_start) / n:.1f}s per row)")
    print(f"LLM calls {CS.calls_made()}   rows written {written}   failures {failed}")
    print("\nPER-FIELD YIELD")
    for f in CS.ALL_FIELDS:
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
