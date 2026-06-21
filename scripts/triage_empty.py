"""Triage EMPTY/NOISE/DEAD sources: for each, try BOTH the requests path and the
Playwright path, and report counts + HTTP status, so we know which are:
  * method-fixable (html -> html_js recovers calls)
  * genuinely empty (no current calls)
  * blocked (403/5xx even with a browser UA)
Read-only. Pass url-substrings as args, else uses the built-in EMPTY/DEAD list.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RFPIS_DEEP_READ", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests  # noqa: E402

from core.scraper import scan_source  # noqa: E402
from db.supabase_client import get_client  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

DEFAULT = ["afdb.org", "chanzuckerberg", "cepi.net", "ausschreibungen.giz",
           "developmentaid.org", "google.org", "gcgh.grandchallenges",
           "hewlett.org", "solve.mit.edu", "english.rvo.nl",
           "opensocietyfoundations", "packard.org", "research.swiss",
           "grandchallenges.org/grant", "grants.nih.gov", "thepandemicfund"]


def try_method(name, url, method):
    try:
        return len(scan_source({"name": name, "method": method, "url": url}) or [])
    except Exception as exc:
        return f"ERR:{str(exc)[:30]}"


def main(subs):
    sb = get_client()
    rows = sb.table("donor_sources").select(
        "donor_name,rfp_listing_url,scrape_method").execute().data or []
    for sub in (subs or DEFAULT):
        m = [r for r in rows if sub.lower() in (r.get("rfp_listing_url") or "").lower()]
        if not m:
            print(f"  ?  no row for {sub}")
            continue
        r = m[0]
        name, url, cur = r["donor_name"], r["rfp_listing_url"], r["scrape_method"]
        try:
            resp = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
            st = resp.status_code
            ln = len(resp.text)
        except Exception as exc:
            st, ln = f"ERR:{type(exc).__name__}", 0
        h = try_method(name, url, "html")
        js = try_method(name, url, "html_js")
        print(f"  {name[:30]:30} cur={cur:8} HTTP={str(st):6} len={ln:>7}  "
              f"html={h}  html_js={js}")
        print(f"       {url[:95]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
