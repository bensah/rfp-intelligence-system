"""Drill into a source: show each extracted candidate + WHY the gate rejects it.

Separates 'healthy source, just not currently eligible' (real titles, individual
solicitation links, maybe deadlines) from 'junk' (nav/campaign/homepage links).

Usage: python scripts/inspect_source.py "<url-substring>" ["<url-substring>" ...]
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

from core.scraper import scan_source            # noqa: E402
from core.auto_scorer import is_eligible         # noqa: E402
from core.policies import get_policies           # noqa: E402
from db.supabase_client import get_client        # noqa: E402


def main(subs: list[str]) -> int:
    policies = get_policies()
    sb = get_client()
    rows = sb.table("donor_sources").select(
        "donor_name,rfp_listing_url,scrape_method,is_active").eq(
        "is_active", True).execute().data or []
    for sub in subs:
        match = [r for r in rows if sub.lower() in (r.get("rfp_listing_url") or "").lower()
                 or sub.lower() in (r.get("donor_name") or "").lower()]
        if not match:
            print(f"\n### no source matching '{sub}'")
            continue
        r = match[0]
        name, url, method = r["donor_name"], r["rfp_listing_url"], r["scrape_method"]
        print(f"\n{'='*78}\n### {name}  [{method}]\n    {url}")
        try:
            cands = scan_source({"name": name, "method": method, "url": url}) or []
        except Exception as exc:
            print(f"    CRAWL-ERR: {exc}")
            continue
        print(f"    {len(cands)} candidates\n")
        for c in cands[:14]:
            title = (c.get("opportunity_title") or "")[:62]
            link = (c.get("opportunity_link") or "")[:70]
            dl = c.get("submission_deadline")
            ok, reason = is_eligible(c, policies)
            mark = "✓" if ok else "✗"
            print(f"  {mark} {title}")
            print(f"      link: {link}")
            print(f"      deadline={dl}  reason={reason[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["wellcome.org"]))
