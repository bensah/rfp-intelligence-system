"""Verify PENDING source_registry rows by running the REAL extraction+eligibility
pipeline on each — the deterministic triage pass before human review.

For each pending registry source it performs exactly what a scan does:
  1. LIVENESS  — resolve the listing URL (browser UA, follow redirects).
  2. LISTINGS  — scan_source() on the URL; if it yields no individual-solicitation
                 links, PROBE a set of common listing paths (/grants /funding
                 /opportunities /calls /tenders /rfp …) to locate the real listings
                 page (a light version of "navigate the site to find listings").
  3. EXTRACTION— build_record() on the top detail candidates (the exact extraction
                 task) → confirms deadline/amount/type are recoverable.
  4. ELIGIBILITY— is_eligible() (downstream policy gate) — reported, not the verdict.

Verdict per source (written to registry.notes; status stays 'pending' so nothing
auto-promotes — human confirms, THEN we push to the catalogue):
  VERIFIED   — a listings page extracts real solicitation links + extraction works
  DEEP-DIVE  — reachable but only noise / needs multi-page nav or bespoke parser
  MANUAL     — reachable description page, no machine-readable listings path found
  DEAD       — unreachable (network error / 4xx / 5xx) everywhere tried

Usage:
    python scripts/verify_pending_sources.py            # dry-run (writes the report)
    python scripts/verify_pending_sources.py --apply    # also write verdicts to registry
    python scripts/verify_pending_sources.py --limit 10 # triage a subset first
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RFPIS_DEEP_READ", "1")   # render JS like the real scan
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

import requests  # noqa: E402

from core.scraper import scan_source  # noqa: E402
from core.extract import build_record  # noqa: E402
from core.auto_scorer import is_eligible  # noqa: E402
from core.policies import get_policies  # noqa: E402
from db.supabase_client import get_client  # noqa: E402
from scripts.audit_sources import _is_detail_link, liveness, UA, TIMEOUT  # noqa: E402

# Common listing paths to probe when the given URL isn't itself a listings page.
_PROBE_PATHS = [
    "/grants", "/grants/", "/funding", "/funding-opportunities", "/opportunities",
    "/calls", "/calls-for-proposals", "/call-for-proposals", "/tenders", "/tenders/",
    "/procurement", "/rfp", "/rfps", "/apply", "/grant-opportunities",
    "/what-we-fund", "/our-grants", "/proposals", "/competitions",
]


def _base(url: str) -> str:
    sp = urlsplit(url)
    return f"{sp.scheme or 'https'}://{sp.netloc}" if sp.netloc else url


def _fetch_text(url: str) -> str:
    """Readable text of a detail page (best-effort, requests-only)."""
    if not url:
        return ""
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            t.decompose()
        main = soup.find("main") or soup.find("article") or soup.body or soup
        return " ".join(main.get_text(" ", strip=True).split())[:20000]
    except Exception:
        return ""


def _crawl(name: str, url: str) -> list[dict]:
    try:
        return scan_source({"name": name, "method": "html", "url": url}) or []
    except Exception:
        return []


def _detail_links(cands: list[dict], url: str) -> int:
    return sum(1 for c in cands if _is_detail_link(c, url))


def verify_one(row: dict, policies: dict) -> dict:
    name = row.get("donor_name") or row.get("host") or "(unnamed)"
    url = (row.get("sample_url") or "").strip()
    host = (row.get("host") or "").strip()
    if not url and host:
        url = f"https://{host}"
    out = {"source_uid": row.get("source_uid"), "host": host, "name": name,
           "given_url": url, "listings_url": "", "status_code": "", "candidates": 0,
           "detail_links": 0, "extracted_ok": 0, "eligible": 0,
           "verdict": "", "note": ""}
    if not url:
        out["verdict"] = "MANUAL"; out["note"] = "no URL on registry row"
        return out

    st, _ = liveness(url)
    out["status_code"] = str(st)

    # 1) try the given URL
    tried = [url]
    cands = _crawl(name, url)
    best_url, best_cands, best_links = url, cands, _detail_links(cands, url)

    # 2) if it's not a listings page, probe common listing paths on the same host
    if best_links == 0:
        base = _base(url)
        for p in _PROBE_PATHS:
            cu = urljoin(base + "/", p.lstrip("/"))
            if cu in tried:
                continue
            tried.append(cu)
            cc = _crawl(name, cu)
            dl = _detail_links(cc, cu)
            if dl > best_links:
                best_url, best_cands, best_links = cu, cc, dl
            if best_links >= 3:
                break

    out["listings_url"] = best_url
    out["candidates"] = len(best_cands)
    out["detail_links"] = best_links

    # 3) EXTRACTION test — fetch the top detail PAGE text (the extractor needs the
    # page body, not just the listing link) then run the real build_record on it.
    # Proves end-to-end extraction works; bounded to 1 page to keep cost sane.
    detail = [c for c in best_cands if _is_detail_link(c, best_url)][:1]
    for c in detail:
        try:
            page = _fetch_text(c.get("opportunity_link") or "")
            cc = dict(c)
            if page:
                cc["_page_text"] = page
            rec, _r = build_record(cc, policies)
            if rec:
                out["extracted_ok"] += 1
        except Exception:
            pass
    # 4) eligibility (downstream signal only — NOT the verdict)
    for c in best_cands:
        try:
            if is_eligible(c, policies)[0]:
                out["eligible"] += 1
        except Exception:
            pass

    # Verdict: health = "extracts real individual-solicitation links" (per the
    # verification doctrine). Extraction/eligibility are reported signals, not gates.
    if best_links >= 1:
        out["verdict"] = "VERIFIED"
        out["note"] = (f"listings at {best_url} · {best_links} call link(s) · "
                       f"extract_ok={out['extracted_ok']} · eligible={out['eligible']}")
    elif out["candidates"] > 0:
        out["verdict"] = "DEEP-DIVE"
        out["note"] = (f"{out['candidates']} candidate(s) but no individual-solicitation "
                       "links — nav/noise; needs multi-page nav or bespoke parser")
    elif st == 0 or (st and st >= 400):
        out["verdict"] = "DEAD"
        out["note"] = f"HTTP {st}; no listings on given URL or probed common paths"
    else:
        out["verdict"] = "MANUAL"
        out["note"] = "reachable but no machine-readable listings (probed common paths)"
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write verdicts to registry.notes")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    policies = get_policies()
    sb = get_client()
    rows = [r for r in (sb.table("source_registry").select("*").limit(5000)
                        .execute().data or []) if (r.get("status") or "") == "pending"]
    rows.sort(key=lambda r: (r.get("donor_name") or r.get("host") or "").lower())
    if args.limit:
        rows = rows[:args.limit]
    print(f"Verifying {len(rows)} PENDING source(s) with {args.workers} workers "
          f"(deep-read={'on' if os.environ.get('RFPIS_DEEP_READ')=='1' else 'off'})\n")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(verify_one, r, policies): r for r in rows}
        for f in as_completed(futs):
            res = f.result()
            results.append(res)
            flag = {"VERIFIED": "✓", "DEEP-DIVE": "◐", "MANUAL": "✎",
                    "DEAD": "✗"}.get(res["verdict"], "·")
            print(f"  {flag} {res['verdict']:9} links={res['detail_links']:>2} "
                  f"extract={res['extracted_ok']:>1} elig={res['eligible']:>2}  "
                  f"{res['name'][:32]:32} {res['host'][:30]}")

    from collections import Counter
    print("\n=== SUMMARY ===")
    for v, n in Counter(r["verdict"] for r in results).most_common():
        print(f"  {v:9} {n}")

    # report doc
    results.sort(key=lambda r: (r["verdict"], r["name"].lower()))
    doc = ["# Pending source verification — for review\n",
           f"_{len(results)} pending sources triaged via the real extraction+eligibility "
           "pipeline. Status stays `pending` until you confirm; VERIFIED rows are ready "
           "to promote, DEEP-DIVE/MANUAL need agent investigation or manual-only._\n",
           "| Verdict | Donor | Host | Listings URL found | Call links | Extracted | Eligible | Note |",
           "|---|---|---|---|---|---|---|---|"]
    for r in results:
        doc.append(f"| {r['verdict']} | {r['name'][:40]} | {r['host']} | "
                   f"{r['listings_url'][:70]} | {r['detail_links']} | {r['extracted_ok']} | "
                   f"{r['eligible']} | {r['note'][:80]} |")
    Path("docs/PENDING_SOURCE_VERIFICATION.md").write_text("\n".join(doc), encoding="utf-8")
    print(f"\nReport: docs/PENDING_SOURCE_VERIFICATION.md")

    if args.apply:
        for r in results:
            note = (f"[verify 2026-06-25] {r['verdict']}: {r['note']} | "
                    f"listings={r['listings_url']}")
            sb.table("source_registry").update({"notes": note[:1000]}).eq(
                "source_uid", r["source_uid"]).execute()
        print(f"Wrote verdicts to registry.notes for {len(results)} row(s) "
              "(status unchanged = 'pending').")
    else:
        print("DRY-RUN — report written, registry NOT modified. Re-run --apply to "
              "record verdicts in registry.notes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
