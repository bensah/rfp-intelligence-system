"""Import Bernard's curated Source-registry CSV into source_registry.

Reads the CSV exported from the Verify > Source registry tab and re-curated by
hand (with two added columns Donor + Code to match the donor catalogue). Columns:
    Donor, Code, host, Hits, Sample, Source class, Verification, Access, Ingestion

Normalises the free-text the curation uses onto our clean vocab, derives the
coarse classification + status, dedups by host, and UPSERTS. DRY-RUN by default.

  python scripts/import_source_registry_csv.py --csv "C:/Users/.../srcreg_csv.csv"
  python scripts/import_source_registry_csv.py --csv "..." --commit
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.source_registry import normalize_host   # noqa: E402
from db.supabase_client import get_client          # noqa: E402

# Unified "Method" vocabulary (same dropdown both tables). Maps 1:1 to the scan
# dispatch value (donor_sources.scrape_method) that push_primaries will set.
METHOD_OPTS = ["API", "RSS / feed", "Page crawl", "JS page crawl", "Manual"]


def norm_method(s: str) -> str:
    t = (s or "").lower()
    if "api" in t:
        return "API"
    if "rss" in t or "feed" in t or "newsletter" in t:
        return "RSS / feed"
    if "dynamic" in t or "js" in t or "playwright" in t:
        return "JS page crawl"
    if "manual" in t or "licensed" in t or "linked" in t or "resource crawl" in t:
        return "Manual"
    return "Page crawl"


def norm_source_class(s: str) -> str:
    t = (s or "").lower()
    if "aggregat" in t:
        return "Opportunity Aggregator"
    if "application" in t or "resource host" in t or "document" in t or "artifact" in t:
        return "Application/resource host"
    if "procurement platform" in t or "primary" in t:
        return "Primary source"
    return "Primary source" if "official" in t else "Unknown"


def norm_access(s: str) -> str:
    t = (s or "").lower()
    if "paid" in t and "free" not in t:
        return "Paid"
    if "freemium" in t:
        return "Freemium"
    if "free" in t:
        return "Free"
    return "Unknown"


def derive_class(source_class: str, verification: str) -> str:
    sc, vf = (source_class or "").lower(), (verification or "").lower()
    if "aggregat" in sc or "aggregat" in vf:
        return "aggregator"
    if "application" in sc or "resource host" in sc or "document" in sc or "artifact" in sc:
        return "aggregator"          # non-canonical host → resolve to the funder
    if "needs" in vf or "not canonical" in vf:
        return "aggregator"
    if "primary" in sc or "primary" in vf or "procurement platform" in sc:
        return "primary"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    def _get(row: dict, *names: str) -> str:
        """Case-insensitive column lookup with aliases (Host/host, Listings URL/
        Sample, Method/Ingestion) so any header variant works."""
        low = {(k or "").strip().lower(): v for k, v in row.items()}
        for n in names:
            v = low.get(n.lower())
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    text = Path(args.csv).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    rows, skipped = {}, 0
    for r in reader:
        raw_host = _get(r, "host")
        host = normalize_host(raw_host) or raw_host.lower()
        if not host:
            skipped += 1
            continue
        sc_raw = _get(r, "source class")
        vf = _get(r, "verification")
        sample = _get(r, "listings url", "sample")
        if not sample.lower().startswith("http"):
            sample = ""              # non-URL note (e.g. docs.google placeholder)
        rows[host] = {              # keyed by host → dedups automatically
            "host": host,
            "donor_name": _get(r, "donor") or None,
            "donor_code": _get(r, "code") or None,
            "sample_url": sample or None,
            "source_class": norm_source_class(sc_raw),
            "classification": derive_class(sc_raw, vf),
            "status": "confirmed" if ("verified" in vf.lower()
                                      and "needs" not in vf.lower()) else "pending",
            "access_model": norm_access(_get(r, "access")),
            "ingestion_method": norm_method(_get(r, "method", "ingestion")),
            "verified_by": "csv-import",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
    payload = list(rows.values())

    # Resilience: if migration-037 columns (donor_name/donor_code) aren't present
    # yet, drop them so the rest still imports — and tell the user.
    try:
        get_client().table("source_registry").select("donor_name").limit(1).execute()
    except Exception:
        for p in payload:
            p.pop("donor_name", None)
            p.pop("donor_code", None)
        print("!! source_registry.donor_name/donor_code MISSING — run the 037 "
              "ALTERs to capture Donor/Code, then re-run this import.\n")
    print(f"rows: {len(payload)}  (skipped {skipped} without host)")
    print("source_class:", dict(Counter(p["source_class"] for p in payload)))
    print("classification:", dict(Counter(p["classification"] for p in payload)))
    print("status:", dict(Counter(p["status"] for p in payload)))
    print("method:", dict(Counter(p["ingestion_method"] for p in payload)))
    print("access:", dict(Counter(p["access_model"] for p in payload)))
    print("\nsample mapping (eyeball):")
    for p in payload[:8]:
        print(f"  {p['host']:<34} {p['donor_code'] or '—':<14} "
              f"{p['source_class']:<24} {p['classification']:<10} "
              f"{p['status']:<9} {p['ingestion_method']}")
    if not args.commit:
        print("\nDRY RUN — re-run with --commit to upsert.")
        return 0
    sb = get_client()
    for i in range(0, len(payload), 100):
        sb.table("source_registry").upsert(payload[i:i + 100],
                                           on_conflict="host").execute()
    print(f"\nUpserted {len(payload)} rows into source_registry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
