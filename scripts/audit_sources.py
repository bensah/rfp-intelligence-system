"""Per-source HEALTH AUDIT — the core reliability check.

For every catalogue source verify, in order:
  1. LIVENESS  — does the URL resolve? (GET for html/rss; POST APIs are exercised
                 by the crawl itself, so they are never GET-short-circuited.)
  2. EXTRACTION— does the real crawl pick individual solicitation links + titles?
  3. QUALITY   — do the extracted candidates pass the scorer's eligibility gate,
                 i.e. are they actual solicitations (not navigation/campaign junk)?

Runs with Playwright (RFPIS_DEEP_READ=1) so JS pages render like the real scan.

Verdict per source:
  OK    — yields candidates, >=1 passes the eligibility gate (real solicitations)
  NOISE — yields candidates but 0 eligible (crawl is picking junk)
  EMPTY — page reachable but crawl found nothing
  DEAD  — page unreachable (network error / 4xx / 5xx) and nothing extracted
Read-only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RFPIS_DEEP_READ", "1")  # render JS pages like the real scan
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from urllib.parse import urlsplit  # noqa: E402

import requests  # noqa: E402

from core.scraper import scan_source, _STRONG_OPP_PATH, _GRANTY_RE  # noqa: E402
from core.auto_scorer import is_eligible  # noqa: E402
from core.policies import get_policies  # noqa: E402
from db.supabase_client import get_client  # noqa: E402


def _is_detail_link(c: dict, listing_url: str) -> bool:
    """A real individual-solicitation link: distinct same-site detail page (not
    the homepage, a language root, or the listing page itself) with a granty
    title or opportunity-shaped path."""
    link = (c.get("opportunity_link") or "").strip()
    if not link:
        return False
    sp, lp = urlsplit(link), urlsplit(listing_url)
    path = sp.path.rstrip("/").lower()
    if path in ("", "/en", "/fr", "/es", "/de") or len(path) <= 4:
        return False  # homepage / language root
    if (sp.netloc.lower() + path) == (lp.netloc.lower() + lp.path.rstrip("/").lower()):
        return False  # points back at the listing page itself
    title = c.get("opportunity_title") or ""
    if _STRONG_OPP_PATH.search(sp.path):
        return True
    if _GRANTY_RE.search(title) or _GRANTY_RE.search(sp.path):
        return True
    return len(title) >= 25

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 25
_API_METHODS = {"rest_json"}


def liveness(url: str) -> tuple[int, str]:
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        return r.status_code, (r.headers.get("content-type") or "").lower()
    except Exception as exc:
        return 0, f"ERR:{type(exc).__name__}"


def audit_one(row: dict, policies: dict) -> dict:
    url = (row.get("rfp_listing_url") or "").strip()
    method = (row.get("scrape_method") or "").strip()
    name = row.get("donor_name") or url
    out = {"name": name, "url": url, "method": method, "status": "",
           "candidates": 0, "linked": 0, "eligible": 0, "verdict": "", "note": ""}
    if not url:
        out["verdict"] = "NO-URL"
        return out

    # 1. liveness (diagnostic only for APIs)
    if method not in _API_METHODS:
        st, ct = liveness(url)
        out["status"] = str(st)
        if st == 0 or st >= 400:
            # still try the crawl — some sources block GET but the handler copes
            pass

    # 2. extraction (this also exercises POST APIs)
    try:
        cands = scan_source({"name": name, "method": method, "url": url}) or []
    except Exception as exc:
        out["verdict"] = "CRAWL-ERR"
        out["note"] = str(exc)[:70]
        return out
    out["candidates"] = len(cands)
    # extraction HEALTH: distinct individual-solicitation detail links
    out["linked"] = sum(1 for c in cands if _is_detail_link(c, url))
    # eligibility is a downstream / policy signal, reported but NOT the verdict
    elig = 0
    for c in cands:
        try:
            ok, _ = is_eligible(c, policies)
            if ok:
                elig += 1
        except Exception:
            pass
    out["eligible"] = elig

    if out["candidates"] == 0:
        out["verdict"] = "DEAD" if (out["status"] and out["status"] != "200"
                                    and method not in _API_METHODS) else "EMPTY"
        if out["status"] and out["status"] != "200":
            out["note"] = f"HTTP {out['status']}"
    elif out["linked"] == 0:
        out["verdict"] = "NOISE"  # extracts only nav/homepage junk, no real calls
        out["note"] = "no individual-solicitation links (nav/homepage only)"
    else:
        out["verdict"] = "OK"
        if elig == 0:
            out["note"] = "real calls found; 0 currently pass policy gate"
    return out


def main(argv: list[str]) -> int:
    only_active = "--all" not in argv
    policies = get_policies()
    sb = get_client()
    rows = sb.table("donor_sources").select(
        "donor_name,rfp_listing_url,scrape_method,is_active").execute().data or []
    if only_active:
        rows = [r for r in rows if r.get("is_active")]
    rows.sort(key=lambda r: (r.get("donor_name") or "").lower())
    print(f"Auditing {len(rows)} sources ({'active only' if only_active else 'all'}) "
          f"with Playwright\n")

    buckets: dict[str, list] = {}
    for r in rows:
        res = audit_one(r, policies)
        buckets.setdefault(res["verdict"], []).append(res)
        flag = {"OK": "✓", "DEAD": "✗", "EMPTY": "∅", "NOISE": "▣",
                "CRAWL-ERR": "!", "NO-URL": "?"}.get(res["verdict"], "·")
        print(f"  {flag} {res['verdict']:9} cand={res['candidates']:>3} "
              f"calls={res['linked']:>3} elig={res['eligible']:>3}  "
              f"{res['name'][:30]:30} {res['method']:9} {res['note'][:36]}")
        print(f"        {res['url'][:92]}")

    print("\n=== SUMMARY ===")
    for v in ("OK", "NOISE", "EMPTY", "DEAD", "CRAWL-ERR", "NO-URL"):
        if buckets.get(v):
            names = ", ".join(x["name"][:18] for x in buckets[v])
            print(f"  {v:9} {len(buckets[v]):>2}  | {names[:120]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
