"""Scanner orchestrator.

Iterates over the configured RFP sources (config/sources.yaml + active rows
in donor_sources), calls the scraper for each, and writes one scan_logs
row per source.

Used by:
  * The Friday GitHub Actions cron (triggered_by='cron')
  * The Admin > Manual Scan button (triggered_by='manual')

Usage:
    python scripts/run_scan.py                # full run
    python scripts/run_scan.py --triggered-by manual
    python scripts/run_scan.py --dry-run      # log nothing, just print
    python scripts/run_scan.py --source "NIH Guide"   # restrict to one source
    python scripts/run_scan.py --workers 4    # tune concurrency (default 8)

Execution model
---------------
Two phases, by design:

  1. **Parallel scrape** — every source's network fetch runs in its own
     thread (ThreadPoolExecutor, default 8 workers). Sources are I/O
     bound (HTTP requests), so threads give a near-linear speedup.

  2. **Sequential ingest** — once all sources have returned, candidates
     are processed one source at a time. This step is sequential because
     `ingest_candidates` uses an in-memory `existing` set for dedup —
     running it concurrently could insert the same RFP twice from two
     different sources that happened to surface it at the same time.

End result: a full ~35-source scan that used to take 5+ minutes now
typically completes in 30-90 seconds (limited by the slowest single
source).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.scraper import scan_source  # noqa: E402
from core.scan_pipeline import ingest_candidates  # noqa: E402
from core.page_monitor import check_manual_sources, summarize_change_events  # noqa: E402
from db.supabase_client import get_client  # noqa: E402

SOURCES_YAML = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"

# Concurrency default. Tuned for typical home / cloud bandwidth — higher
# is rarely worth it because the slowest single source becomes the floor
# and donor sites start rate-limiting around 10+ concurrent.
DEFAULT_WORKERS = int(os.environ.get("SCAN_PARALLELISM", "8"))


def _load_yaml_sources() -> list[dict[str, Any]]:
    with SOURCES_YAML.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("sources", []) or []


def _load_donor_sources(sb) -> list[dict[str, Any]]:
    res = sb.table("donor_sources").select("*").eq("is_active", True).execute()
    out = []
    for r in res.data or []:
        # Use the FULL donor_name. Previously prefixed with donor_code
        # which is a short truncation ("Google" for "Google Alert — RFPs",
        # "Fondation" for "Fondation Pierre Fabre") — confusing in
        # scan_logs and unhelpful for users.
        donor_name = (r.get("donor_name") or "").strip() or "(unnamed donor)"
        out.append(
            {
                "name": f"{donor_name} — donor catalog",
                "method": r["scrape_method"],
                "url": r["rfp_listing_url"],
                "donor_source_id": r["id"],
                "origin": "donor_sources",
            }
        )
    return out


def _log_scan(sb, *, source: str, triggered_by: str,
              found: int, new: int, dup: int, rejected: int,
              duration: float, errors: str | None = None) -> None:
    sb.table("scan_logs").insert(
        {
            "source": source,
            "triggered_by": triggered_by,
            "rfps_found": found,
            "rfps_new": new,
            "rfps_duplicate": dup,
            "rfps_rejected": rejected,
            "duration_sec": round(duration, 3),
            "errors": errors,
        }
    ).execute()


def _scrape_one(source: dict[str, Any]) -> dict[str, Any]:
    """Pure scrape — no DB, no ingest. Safe to run in a worker thread.

    Returns the source's results plus timing/error metadata so the
    sequential ingest phase can process it consistently.
    """
    name = source.get("name") or source.get("url") or "(unnamed)"
    t0 = time.time()
    err = None
    try:
        results = scan_source(source)
    except Exception as exc:
        results = []
        err = f"{type(exc).__name__}: {exc}"
    return {
        "name": name,
        "source": source,
        "results": results,
        "err": err,
        "duration": time.time() - t0,
    }


def run(
    triggered_by: str = "cron",
    dry_run: bool = False,
    source_filter: str | None = None,
    workers: int = DEFAULT_WORKERS,
) -> dict:
    """Orchestrate a full scan. Returns aggregate counts dict."""
    sb = None if dry_run else get_client()
    yaml_sources = _load_yaml_sources()
    donor_sources = [] if dry_run else _load_donor_sources(sb)
    all_sources = yaml_sources + donor_sources

    if source_filter:
        all_sources = [s for s in all_sources if (s.get("name") or "") == source_filter]

    if not all_sources:
        print("No sources to scan.")
        return {"sources": 0, "found": 0, "new": 0, "duplicate": 0,
                "rejected": 0, "errors": 0}

    # -------------------------------------------------------------------
    # Phase 0: page-change check for manual sources
    # Manual sources don't get scraped, but we monitor them for content
    # changes so the team knows when to re-check the page by hand.
    # Each change emits a scan_logs row labelled "PAGE CHANGED".
    # -------------------------------------------------------------------
    manual_sources = [
        s for s in all_sources
        if (s.get("method") or "").lower() == "manual"
    ]
    if manual_sources and not dry_run:
        try:
            events = check_manual_sources(manual_sources)
            change_summary = summarize_change_events(events)
            print(f"Manual-source page check: {change_summary}")
            # Surface changes in scan_logs so they appear in Admin → Manual Scan
            # history. One row per changed page.
            for ev in events:
                if ev.get("changed"):
                    _log_scan(
                        sb,
                        source=f"PAGE CHANGED: {ev['name']}",
                        triggered_by=triggered_by,
                        found=0, new=0, dup=0, rejected=0,
                        duration=ev.get("duration", 0.0),
                        errors=(
                            f"Manual source updated since last scan. "
                            f"Old hash: {(ev.get('old_hash') or '')[:12]} → "
                            f"new: {ev['new_hash'][:12]}. URL: {ev['url']}"
                        ),
                    )
        except Exception as exc:
            print(f"  (page-change check failed: {exc})")

    # Cap workers at the number of sources — no benefit to having more
    # threads than tasks.
    effective_workers = max(1, min(workers, len(all_sources)))
    wall_start = time.time()
    print(
        f"Scan started · triggered_by={triggered_by} · "
        f"{len(all_sources)} source(s) · workers={effective_workers} · "
        f"dry_run={dry_run}"
    )

    # -------------------------------------------------------------------
    # Phase 1: parallel scrape
    # -------------------------------------------------------------------
    scraped: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=effective_workers) as ex:
        futures = {ex.submit(_scrape_one, s): s for s in all_sources}
        for fut in as_completed(futures):
            try:
                scraped.append(fut.result())
            except Exception as exc:
                # _scrape_one already catches scraper exceptions; this only
                # triggers on truly unexpected thread-level failures.
                src = futures[fut]
                scraped.append({
                    "name": src.get("name") or "(unnamed)",
                    "source": src,
                    "results": [],
                    "err": f"thread crash: {type(exc).__name__}: {exc}",
                    "duration": 0.0,
                })
    scrape_seconds = time.time() - wall_start

    # Sort by original source order so the printed log matches the YAML
    # ordering (more readable than completion order).
    name_index = {s.get("name") or s.get("url") or "(unnamed)": i
                  for i, s in enumerate(all_sources)}
    scraped.sort(key=lambda b: name_index.get(b["name"], 99999))

    # -------------------------------------------------------------------
    # Phase 2: sequential ingest (preserves dedup state)
    # -------------------------------------------------------------------
    totals = {"sources": 0, "found": 0, "new": 0, "duplicate": 0,
              "rejected": 0, "errors": 0}
    for batch in scraped:
        name = batch["name"]
        err = batch["err"]
        if err:
            totals["errors"] += 1

        # Run the dedup + insert pipeline. Skipped on dry-run (still counts).
        new = 0
        dup = 0
        rejected = 0
        if batch["results"]:
            try:
                new, dup, rejected = ingest_candidates(
                    batch["results"], dry_run=dry_run,
                )
            except Exception as exc:
                err = (err + " | " if err else "") + f"ingest: {type(exc).__name__}: {exc}"
                totals["errors"] += 1

        duration = batch["duration"]
        found = len(batch["results"])
        totals["found"] += found
        totals["new"] += new
        totals["duplicate"] += dup
        totals["rejected"] += rejected
        totals["sources"] += 1

        status = "ERR" if err else "ok "
        print(
            f"  [{status}] {name:40} found={found}  new={new}  dup={dup}  "
            f"declined={rejected}  ({duration:.2f}s)"
            + (f"  {err}" if err else "")
        )

        if not dry_run:
            _log_scan(sb, source=name, triggered_by=triggered_by,
                      found=found, new=new, dup=dup, rejected=rejected,
                      duration=duration, errors=err)

    wall = time.time() - wall_start
    serial_estimate = sum(b["duration"] for b in scraped)
    speedup = serial_estimate / wall if wall > 0 else 1.0
    print(
        f"Scan done · {totals['sources']} source(s) · "
        f"{totals['found']} found · {totals['new']} new · "
        f"{totals['duplicate']} dup · {totals['rejected']} declined · "
        f"{totals['errors']} error(s) · "
        f"wall={wall:.1f}s (serial would be ~{serial_estimate:.0f}s, "
        f"speedup ×{speedup:.1f})"
    )
    return totals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--triggered-by", default="cron",
        help="Free-form label written to scan_logs.triggered_by. "
             "Examples: 'cron', 'manual', 'manual:user@example.com', "
             "'startup', 'test'.",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default=None, help="Restrict to one source name")
    ap.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=(
            f"Parallel scraper workers (default {DEFAULT_WORKERS}, "
            f"env: SCAN_PARALLELISM). Higher than 10 risks rate-limiting "
            f"on small donor servers."
        ),
    )
    args = ap.parse_args()
    run(
        triggered_by=args.triggered_by,
        dry_run=args.dry_run,
        source_filter=args.source,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
