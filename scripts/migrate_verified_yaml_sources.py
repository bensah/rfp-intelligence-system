"""Move verified config/sources.yaml sources into the catalogue + registry.

Part of consolidating onto the catalogue as the single source of truth. These
yaml-only sources were each VERIFIED to extract real solicitations via their
existing handlers (TED → _scan_ted, UK OCDS → _scan_ocds, ResearchNet →
_scan_researchnet), and are not duplicates of a catalogue row. They are added to
donor_sources (active, Primary) and flagged in source_registry (in_catalogue).

NOT migrated (with reason):
  * World Bank Procurement Notices — duplicate of the catalogue 'worldbank.org'
    row (same procnotices API).
  * Fondation Pierre Fabre — ODESS detail — single call page; the active Pierre
    Fabre source already covers it.
  * Global South Opportunities — aggregator blog (returns careers/listicles), not
    a primary solicitation source.

Idempotent: skips a source already present in donor_sources (by listing URL).

    python scripts/migrate_verified_yaml_sources.py            # dry-run
    python scripts/migrate_verified_yaml_sources.py --commit    # apply
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from db.supabase_client import get_client  # noqa: E402

SOURCES = [
    {
        "donor_name": "Tenders Electronic Daily (EU TED)", "donor_code": "TED",
        "base_url": "https://ted.europa.eu/",
        "rfp_listing_url": "https://api.ted.europa.eu/v3/notices/search",
        "scrape_method": "rest_json", "source_class": "Primary source",
        "opportunity_types": ["Tender", "Procurement notice"],
        "ingestion_method": "API (JSON)", "has_api": True,
        "notes": "Migrated from sources.yaml (catalogue consolidation). EU public "
                 "procurement; handler _scan_ted. Geo gate keeps only in-scope.",
    },
    {
        "donor_name": "UK Find a Tender Service", "donor_code": "UK-FTS",
        "base_url": "https://www.find-tender.service.gov.uk/",
        "rfp_listing_url": "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
        "scrape_method": "rest_json", "source_class": "Primary source",
        "opportunity_types": ["Tender", "Procurement notice"],
        "ingestion_method": "API (JSON)", "has_api": True,
        "notes": "Migrated from sources.yaml. UK OCDS tenders (incl. FCDO dev "
                 "contracts); handler _scan_ocds. Geo gate keeps only in-scope.",
    },
    {
        "donor_name": "UK Contracts Finder", "donor_code": "UK-CF",
        "base_url": "https://www.contractsfinder.service.gov.uk/",
        "rfp_listing_url": "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search?order=desc",
        "scrape_method": "rest_json", "source_class": "Primary source",
        "opportunity_types": ["Tender", "Procurement notice"],
        "ingestion_method": "API (JSON)", "has_api": True,
        "notes": "Migrated from sources.yaml. UK OCDS contracts; handler "
                 "_scan_ocds. Geo gate keeps only in-scope.",
    },
    {
        "donor_name": "Canadian Institutes of Health Research (ResearchNet)",
        "donor_code": "CIHR",
        "base_url": "https://www.researchnet-recherchenet.ca/",
        "rfp_listing_url": "https://www.researchnet-recherchenet.ca/rnr16/fodRss.do?type=ALL&chanTyp=ALL&lang=E",
        "scrape_method": "rss", "source_class": "Primary source",
        "opportunity_types": ["Grant", "Award"],
        "ingestion_method": "RSS / feed", "has_api": False,
        "notes": "Migrated from sources.yaml. CIHR funding RSS (deadlines in feed); "
                 "handler _scan_researchnet. Primary class -> no aggregator "
                 "resolution (clean links). Geo gate drops Canada-only calls.",
    },
]


def _host(u: str) -> str:
    h = urlsplit(u).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def main(argv: list[str]) -> int:
    commit = "--commit" in argv
    sb = get_client()
    existing = {(r.get("rfp_listing_url") or "").strip()
                for r in sb.table("donor_sources").select("rfp_listing_url").execute().data or []}
    print(f"=== migrate {len(SOURCES)} verified yaml sources "
          f"{'(COMMIT)' if commit else '(DRY RUN)'} ===")
    for s in SOURCES:
        if s["rfp_listing_url"] in existing:
            print(f"  · skip (already in catalogue): {s['donor_name']}")
            continue
        host = _host(s["rfp_listing_url"])
        print(f"  + {s['donor_name']}  [{s['scrape_method']}]  host={host}")
        if not commit:
            continue
        # 1. catalogue (donor_sources) — source_uid auto via sequence (mig 043).
        sb.table("donor_sources").insert({
            "donor_name": s["donor_name"], "donor_code": s["donor_code"],
            "base_url": s["base_url"], "rfp_listing_url": s["rfp_listing_url"],
            "scrape_method": s["scrape_method"], "source_class": s["source_class"],
            "opportunity_types": s["opportunity_types"], "access_model": "Free",
            "is_active": True, "host": host, "notes": s["notes"],
        }).execute()
        # 2. registry (source_registry) — upsert host, flag in_catalogue.
        reg = {
            "host": host, "classification": "primary", "status": "confirmed",
            "source_class": s["source_class"], "ingestion_method": s["ingestion_method"],
            "has_api": s["has_api"], "sample_url": s["rfp_listing_url"],
            "donor_name": s["donor_name"], "donor_code": s["donor_code"],
            "opportunity_types": s["opportunity_types"], "in_catalogue": True,
            "verified_by": "catalogue-consolidation",
        }
        sb.table("source_registry").upsert(reg, on_conflict="host").execute()
    if commit:
        print("\n✓ Applied. Run scripts/backfill_source_uids.py to set donor links.")
    else:
        print("\nDry-run. Re-run with --commit to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
