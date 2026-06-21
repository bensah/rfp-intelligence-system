"""Verify every Source-registry (or donor-catalogue) listing URL end-to-end.

For each source it: (1) resolves the unified Method -> scan dispatch, (2) fetches
with that method, (3) counts child opportunity candidates extracted, (4) detects
the opportunity TYPE(s) from child titles. Prints a verdict + suggested fix and
(with --commit) writes detected opportunity_types back to source_registry.

  python scripts/verify_sources.py                      # registry, all, report only
  python scripts/verify_sources.py --start 8            # skip the first 7 (done)
  python scripts/verify_sources.py --only grants.gov,afd.fr
  python scripts/verify_sources.py --commit             # write detected types
  python scripts/verify_sources.py --catalogue          # verify donor_sources too

Method->dispatch: API->rest_json · RSS / feed->rss · Page crawl->html ·
JS page crawl->html_js (needs Playwright = GitHub Actions) · Manual->skip.
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

from core import scraper                       # noqa: E402
from core import source_registry as sr         # noqa: E402
from db.supabase_client import get_client      # noqa: E402

_DISPATCH = {"api": "rest_json", "rss": "rss", "page crawl": "html",
             "js page crawl": "html_js", "manual": "manual"}

# Type keyword -> canonical opportunity type (longest/most-specific first).
_TYPE_RULES = [
    ("expression of interest", "EOI"), ("request for information", "RFI"),
    ("request for proposal", "RFP"), ("call for proposal", "CFP"),
    ("call for application", "Grant"), ("letter of inten", "LOI"),
    ("procurement", "Procurement notice"), ("contract award", "Contract award"),
    ("invitation to bid", "Tender"), ("tender", "Tender"), ("solicitation", "Tender"),
    ("fellowship", "Fellowship"), ("scholarship", "Scholarship"),
    ("internship", "Internship"), ("consultan", "Consultancy"),
    ("vacancy", "Job"), ("career", "Job"), ("recruit", "Job"), (" job", "Job"),
    ("webinar", "Training"), ("seminar", "Training"), ("conference", "Training"),
    ("training", "Training"), ("prize", "Award"), ("award", "Award"),
    ("rfp", "RFP"), ("cfp", "CFP"), (" eoi", "EOI"), (" rfi", "RFI"),
    ("grant", "Grant"), ("fund", "Grant"), ("call", "CFP"),
]


def _dispatch(method: str) -> str:
    return _DISPATCH.get((method or "").strip().lower(), "html")


def _detect_types(blob: str) -> list[str]:
    """Detect types from a blob of URL + donor name + child titles. URL/name
    keywords (e.g. /calls-for-proposals, /rfps-eois, /tender) are far cleaner
    signals than noisy crawled nav links."""
    blob = (blob or "").lower()
    found: list[str] = []
    for kw, typ in _TYPE_RULES:
        if kw in blob and typ not in found:
            found.append(typ)
    return found[:5]


def _fetch(url: str, disp: str, name: str):
    """Return (n_children, sample_titles, note)."""
    if disp == "manual":
        return 0, [], "manual (no scan)"
    try:
        if disp == "rss":
            c = scraper._scan_rss(name, url)
        elif disp == "rest_json":
            c = scraper.scan_source({"name": name, "method": "rest_json", "url": url})
        else:  # html / html_js
            c = scraper.expand_listing(url, source_name=name)
        titles = [(x.get("opportunity_title") or "").strip() for x in c]
        titles = [t for t in titles if t]
        note = ""
        if disp == "html_js" and not c:
            note = "needs Playwright (verify on Actions)"
        return len(titles), titles[:6], note
    except Exception as exc:
        return 0, [], f"ERROR {type(exc).__name__}: {str(exc)[:50]}"


def _verdict(n: int, disp: str, note: str) -> str:
    if note.startswith("ERROR"):
        return "FAIL"
    if disp == "manual":
        return "SKIP"
    if disp == "html_js" and n == 0:
        return "JS?"
    if n == 0:
        return "FAIL"
    if n == 1:
        return "WARN"
    return "OK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--catalogue", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    if args.catalogue:
        rows = get_client().table("donor_sources").select(
            "donor_name,rfp_listing_url,scrape_method,opportunity_types"
        ).eq("is_active", True).execute().data or []
        for r in rows:                           # normalise to registry-ish shape
            r["host"] = r.get("rfp_listing_url")
            r["sample_url"] = r.get("rfp_listing_url")
            r["ingestion_method"] = r.get("scrape_method")
    else:
        rows = sr.list_rows()
    if args.only:
        keep = {h.strip().lower() for h in args.only.split(",")}
        rows = [r for r in rows if (r.get("host") or "").lower() in keep]
    rows = rows[args.start:]

    print(f"{'host':<34} {'method':<13} {'verdict':<6} {'#':>3}  types / note")
    print("-" * 100)
    tally = {}
    updates = 0
    for r in rows:
        host = r.get("host") or ""
        url = r.get("sample_url") or ""
        disp = _dispatch(r.get("ingestion_method"))
        if not url.lower().startswith("http"):
            print(f"{host:<34} {disp:<13} {'NOURL':<6} {0:>3}  (no listing URL)")
            tally["NOURL"] = tally.get("NOURL", 0) + 1
            continue
        n, titles, note = _fetch(url, disp, host)
        v = _verdict(n, disp, note)
        tally[v] = tally.get(v, 0) + 1
        types = _detect_types(f"{url} {host} {r.get('donor_name') or ''} "
                              + " ".join(titles))
        info = note or (", ".join(types) if types else "(no type detected)")
        print(f"{host:<34} {disp:<13} {v:<6} {n:>3}  {info}")
        if titles and v in ("OK", "WARN"):
            print(f"{'':>36}e.g. {titles[0][:70]}")
        # Write detected types (registry only) — types come from URL/name so they
        # are reliable even when the local yield check can't reach JS/API sources.
        # Filter to the FUNDING bucket: Bernard confirmed ALL current sources are
        # funding-facing, so we suppress stray Job/Training/Fellowship hits that
        # come from nav links (e.g. a "Careers" link on a grants page).
        _FUNDING = {"Grant", "Award", "RFP", "CFP", "RFI", "EOI", "LOI", "Tender",
                    "Procurement notice", "Contract award"}
        ftypes = [t for t in types if t in _FUNDING]
        if args.commit and not args.catalogue and ftypes:
            if sr.update_row(host, {"opportunity_types": ftypes},
                             by="verify-script"):
                updates += 1

    print("-" * 100)
    print("tally:", tally, f"| type-updates written: {updates}" if args.commit else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
