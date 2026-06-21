"""Reverse-sync: bring ACTIVE donor_sources rows that aren't represented in the
source_registry INTO the registry, so the registry is the complete single source
of truth (new sources are added there going forward).

Dedup by BASE DOMAIN (last two labels) against registry host + registry sample_url
hosts, so domain-changed synced rows (e.g. gatesfoundation→grandchallenges.org)
and api-subdomains (api.grants.gov vs grants.gov) don't create duplicates.

DRY-RUN by default; --commit to upsert.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import source_registry as sr            # noqa: E402
from core.source_registry import normalize_host    # noqa: E402
from db.supabase_client import get_client          # noqa: E402

# donor_sources.scrape_method (technical) -> registry "Method" (friendly).
REV_METHOD = {"rest_json": "API", "rss": "RSS / feed", "html": "Page crawl",
              "html_js": "JS page crawl", "manual": "Manual"}


def _base(host: str | None) -> str:
    p = (host or "").split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else (host or "")


def main(commit: bool) -> int:
    reg = sr.list_rows()
    reg_bases = set()
    for r in reg:
        reg_bases.add(_base(r.get("host")))
        su = normalize_host(r.get("sample_url"))
        if su:
            reg_bases.add(_base(su))

    sb = get_client()
    ds = sb.table("donor_sources").select(
        "donor_name,donor_code,rfp_listing_url,scrape_method,source_class,"
        "access_model,opportunity_types").eq("is_active", True).execute().data or []

    add = []
    for d in ds:
        url = d.get("rfp_listing_url") or ""
        host = normalize_host(url)
        if not host or _base(host) in reg_bases:
            continue                                # already represented
        sc = d.get("source_class")
        scl = (sc or "").lower()
        if "aggreg" in scl:
            cls = "aggregator"
        elif scl.startswith("primary") or "procurement platform" in scl:
            cls = "primary"
        else:
            try:
                from core import aggregators
                cls = aggregators.classify(url)[0]
                cls = cls if cls in ("primary", "aggregator") else "primary"
            except Exception:
                cls = "primary"
        add.append({
            "host": host,
            "donor_name": d.get("donor_name"),
            "donor_code": d.get("donor_code"),
            "sample_url": url,
            "ingestion_method": REV_METHOD.get(d.get("scrape_method"), "Page crawl"),
            "source_class": sc or ("Primary source" if cls == "primary"
                                   else "Opportunity Aggregator"),
            "classification": cls,
            "status": "confirmed",
            "access_model": d.get("access_model"),
            "opportunity_types": d.get("opportunity_types"),
            "verified_by": "catalogue-sync",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        })

    print(f"catalogue rows not in registry → add: {len(add)}")
    for a in add:
        print(f"  + {a['host']:<34} {a['classification']:<10} "
              f"{a['ingestion_method']:<14} {a.get('donor_name') or ''}")
    if not commit:
        print("\nDRY RUN — re-run with --commit to upsert into source_registry.")
        return 0
    if add:
        sb.table("source_registry").upsert(add, on_conflict="host").execute()
    print(f"\nUpserted {len(add)} catalogue sources into the registry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
