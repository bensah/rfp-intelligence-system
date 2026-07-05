"""Scanner orchestrator.

Iterates over the ACTIVE rows of the donor_sources catalogue (the single source
of truth — config/sources.yaml and donor_matrix seeds are no longer folded in),
calls the scraper for each, and writes one scan_logs row per source.

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
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env so the scan subprocess has LLM_JUDGE_* (and other) creds — without
# this the LLM extraction fallback (core.extract) silently stays disabled in scans.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

# Force UTF-8 stdout/stderr. When launched as a subprocess on Windows the child
# inherits a cp1252 stream, so a single non-ASCII print (e.g. the "∩" in the dedup
# summary) raises UnicodeEncodeError and crashes the whole scan with exit code 1.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.scraper import scan_source  # noqa: E402
from core.scan_pipeline import ingest_candidates  # noqa: E402
from core import aggregators  # noqa: E402  — primary/aggregator class for ingest ordering
from core.page_monitor import check_manual_sources, summarize_change_events  # noqa: E402
from db.supabase_client import get_client  # noqa: E402

SOURCES_YAML = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"

# Concurrency default. Tuned for typical home / cloud bandwidth — higher
# is rarely worth it because the slowest single source becomes the floor
# and donor sites start rate-limiting around 10+ concurrent.
DEFAULT_WORKERS = int(os.environ.get("SCAN_PARALLELISM", "8"))

# Whether to fold the researched donor_source_seeds (from the donor matrix) into
# the scan as extra HTML sources. On by default; set SCAN_INCLUDE_SEEDS=0 to
# disable if it ever gets noisy.
INCLUDE_SEEDS = os.environ.get("SCAN_INCLUDE_SEEDS", "1") != "0"

# A seed URL is worth auto-scanning only if it looks like an opportunities /
# calls page — not a homepage or a known dead-end. A positive signal (grant /
# call / rfp / funding / apply ...) anywhere in the path wins; hard-negatives
# below catch pages that contain a granty word but are still useless to scan
# (past awards, FAQs, news, social profiles).
_SEED_POS_RE = re.compile(
    r"(grant|funding|fund-|/funds|call[-_ ]?for|/calls?\b|rfp|rfq|rfa"
    r"|request[-_ ]for|propos|tender|procure|opportunit|solicit|/cfp|/eoi"
    r"|expression[-_ ]of[-_ ]interest|/apply\b)", re.I)
_SEED_HARD_NEG_RE = re.compile(
    r"(awarded|awardees|grantees|/faq\b|applicant-faq|/news/|/news$|/blog/"
    r"|facebook\.com|twitter\.com|x\.com|linkedin\.com|youtube\.com"
    r"|instagram\.com)", re.I)


def _norm_url(url: str) -> str:
    """Lower-case, drop fragment + trailing slash for dedup comparisons."""
    u = (url or "").strip().lower().split("#", 1)[0]
    return u[:-1] if u.endswith("/") else u


def _seed_is_scannable(url: str) -> bool:
    """True only for opportunity/calls-style pages (skip homepages + dead-ends)."""
    if not url:
        return False
    u = url.strip().lower()
    if _SEED_HARD_NEG_RE.search(u):       # past awards / FAQ / news / social
        return False
    p = urlparse(u)
    if len(p.path.strip("/")) < 2:        # bare domain / homepage
        return False
    return bool(_SEED_POS_RE.search(p.path + ("?" + p.query if p.query else "")))


# Scrape-method richness — when the SAME url appears in both the yaml base list
# and donor_sources (they overlap heavily), keep the richer one so each url is
# scanned ONCE. Structured APIs > feeds > static HTML > JS render > manual stub.
_METHOD_RANK = {"rest_json": 4, "rss": 3, "html": 2, "html_js": 1, "manual": 0}

# Scan segregation (2026-06-20): the CURRENT scan covers donor RFPs/grants/tenders
# only. A source tagged in donor_sources.opportunity_types EXCLUSIVELY with
# career/job types is excluded so jobs/fellowships/etc. don't enter the donor
# scan; UNTAGGED sources are treated as donor-RFP (backward compatible). The other
# verticals get their own scans later (not built yet).
_DONOR_RFP_TYPES = {"grant", "award", "rfp", "cfp", "rfi", "eoi", "tender",
                    "procurement notice", "contract award",
                    "cooperative agreement", "seed fund", "loi"}
_CAREER_TYPES = {"job", "consultancy", "internship", "fellowship",
                 "scholarship", "training"}


def _is_donor_rfp_source(types) -> bool:
    """True if a source belongs in the current donor-RFP scan. Untagged → yes
    (default). Excluded only when tagged EXCLUSIVELY with career/job types."""
    t = {str(x).strip().lower() for x in (types or []) if str(x).strip()}
    if not t or (t & _DONOR_RFP_TYPES):
        return True
    return not (t <= _CAREER_TYPES)


def _dedup_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse exact-URL duplicates across yaml + donor_sources + seeds, keeping
    the entry with the richest scrape method. Non-destructive (touches neither the
    yaml nor the DB); preserves first-seen order and any URL-less entries."""
    winner: dict[str, dict[str, Any]] = {}
    seq: list[tuple[str, dict[str, Any]]] = []
    for s in sources:
        key = _norm_url(s.get("url", ""))
        if not key:
            seq.append(("", s))
            continue
        seq.append((key, s))
        cur = winner.get(key)
        if cur is None or (_METHOD_RANK.get((s.get("method") or "").lower(), -1)
                           > _METHOD_RANK.get((cur.get("method") or "").lower(), -1)):
            winner[key] = s
    out, seen = [], set()
    for key, s in seq:
        if not key:
            out.append(s)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(winner[key])
    dropped = len(sources) - len(out)
    if dropped:
        print(f"Deduped {dropped} exact-URL duplicate source(s) "
              "(yaml ∩ donor_sources ∩ seeds).")
    return out


def _load_yaml_sources() -> list[dict[str, Any]]:
    with SOURCES_YAML.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("sources", []) or []


def _load_donor_sources(sb) -> list[dict[str, Any]]:
    res = sb.table("donor_sources").select("*").eq("is_active", True).execute()
    out = []
    skipped = 0
    for r in res.data or []:
        # Donor-RFP scan only: skip sources tagged exclusively as career/job.
        if not _is_donor_rfp_source(r.get("opportunity_types")):
            skipped += 1
            continue
        # Use the FULL donor_name. Previously prefixed with donor_code
        # which is a short truncation ("Google" for "Google Alert — RFPs",
        # "Fondation" for "Fondation Pierre Fabre") — confusing in
        # scan_logs and unhelpful for users.
        donor_name = (r.get("donor_name") or "").strip() or "(unnamed donor)"
        # Curated class drives scan routing: primary -> crawl+extract directly;
        # aggregator -> crawl+resolve-to-primary before the gate (never rejected).
        _sc = (r.get("source_class") or "").lower()
        kind = ("aggregator" if "aggreg" in _sc
                else "primary" if ("primary" in _sc or "procurement platform" in _sc)
                else "")
        out.append(
            {
                "name": f"{donor_name} — donor catalog",
                "method": r["scrape_method"],
                "url": r["rfp_listing_url"],
                "donor_source_id": r["id"],
                "source_class": kind,
                "origin": "donor_sources",
            }
        )
    if skipped:
        print(f"Skipped {skipped} career/job-tagged donor source(s) — "
              "current scan is donor-RFP only.")
    return out


def _load_seed_sources(sb, existing_urls: set[str]) -> list[dict[str, Any]]:
    """Fold researched donor_source_seeds (from the donor matrix) into the scan
    as generic HTML sources — but only opportunity-style URLs not already
    covered by sources.yaml / donor_sources. This bridges the donor-intel
    research into discovery without a code change per donor."""
    try:
        res = sb.table("donor_source_seeds").select("donor,url,source_type").execute()
    except Exception:
        return []
    seen = set(existing_urls)
    out: list[dict[str, Any]] = []
    for r in res.data or []:
        url = (r.get("url") or "").strip()
        norm = _norm_url(url)
        if not url or norm in seen or not _seed_is_scannable(url):
            continue
        seen.add(norm)
        donor = (r.get("donor") or "").strip() or "(unnamed donor)"
        out.append({
            "name": f"{donor} — seed",
            "method": "html",
            "url": url,
            "origin": "donor_seeds",
        })
    return out


def build_scan_sources(sb) -> list[dict[str, Any]]:
    """The exact set of sources a scan processes: active donor_sources (donor-RFP
    only — career/job-tagged rows excluded), de-duplicated by URL. Single source of
    truth shared by run() and the banner count so the two can never drift.

    Catalogue-only by design: the curated donor_sources catalogue is the single
    point of entry (managed in Verify → Source registry → pushed to the catalogue).
    The legacy config/sources.yaml keyword list and donor_matrix seeds are NOT
    folded in — add or remove a catalogue row and the scan set changes 1:1."""
    return _dedup_sources(_load_donor_sources(sb))


def count_scannable_sources() -> int:
    """How many sources a scan will actually scrape (non-manual) — live from the
    active catalogue, so it tracks every add/remove. Returns 0 on error."""
    try:
        srcs = build_scan_sources(get_client())
    except Exception:
        return 0
    return sum(1 for s in srcs if (s.get("method") or "").lower() != "manual")


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
    # Stamp each candidate with its source's curated class so the ingest pipeline
    # routes primaries (extract directly) vs aggregators (resolve→primary) and
    # never rejects a configured primary as "non-primary".
    _sc = source.get("source_class") or ""
    if _sc:
        for c in results:
            c.setdefault("_source_class", _sc)
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
    extract_only: bool = False,
) -> dict:
    """Orchestrate a full scan. Returns aggregate counts dict.

    extract_only=True → PURE extraction: crawl + extract into the global store, NO
    per-tenant Screened insert/scoring (geography off the gate). That screening is
    the separate "My eligible funding" run (core.scan_pipeline.run_screening)."""
    # Catalogue-only scan: the active donor_sources rows ARE the scan set (the
    # legacy sources.yaml keyword list + donor_matrix seeds are no longer folded
    # in). A read-only client is fine on dry-run — writes stay gated on dry_run.
    sb = get_client()
    all_sources = build_scan_sources(sb)

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

    # Phase-2 ingest is SEQUENTIAL and dedup state accumulates, so the FIRST batch
    # carrying a given call wins. Order batches so that when the same call is
    # republished in several places, the PRIMARY donor source ingests BEFORE any
    # aggregator copy (the aggregator's is then suppressed as a duplicate, keeping
    # the donor's own page as the canonical record). Order: primary → unknown →
    # aggregator/blog; within each class the LARGEST repositories first (more
    # listings ⇒ more chances to be the canonical first-seen, and they set the
    # dedup baseline). Original catalogue order breaks ties so the log stays stable.
    _CLASS_RANK = {"primary": 0, "unknown": 1, "aggregator": 2, "blog": 2}
    name_index = {s.get("name") or s.get("url") or "(unnamed)": i
                  for i, s in enumerate(all_sources)}

    def _ingest_rank(b: dict) -> tuple:
        src = b.get("source") or {}
        try:
            kind = aggregators.classify(src.get("url"), None)[0]
        except Exception:
            kind = "unknown"
        return (_CLASS_RANK.get(kind, 1),
                -len(b.get("results") or []),
                name_index.get(b["name"], 99999))

    scraped.sort(key=_ingest_rank)

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
                    batch["results"], dry_run=dry_run, extract_only=extract_only,
                )
            except Exception as exc:
                err = (err + " | " if err else "") + f"ingest: {type(exc).__name__}: {exc}"
                totals["errors"] += 1

        duration = batch["duration"]
        # "Found" = every candidate the pipeline EVALUATED = new + duplicate +
        # rejected. Not len(batch["results"]): when a listing/aggregator page is
        # expanded into child calls, those children are gated too, so counting
        # only the top-level results made rejected exceed found (e.g.
        # DevelopmentAid 40 top-level but 85 evaluated). This keeps the breakdown
        # internally consistent (found >= rejected, found = new + dup + rejected).
        found = new + dup + rejected
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
            try:
                _log_scan(sb, source=name, triggered_by=triggered_by,
                          found=found, new=new, dup=dup, rejected=rejected,
                          duration=duration, errors=err)
            except Exception as _le:           # telemetry must never crash the scan
                print(f"  (scan_logs insert failed for {name}: {_le})",
                      file=sys.stderr)

    # Refresh the registry's in_catalogue flags once per scan so any PRIMARY newly
    # discovered via aggregator resolution this run surfaces as "pending · not yet in
    # catalogue" for a verifier — without waiting for a manual Sync. Best-effort.
    if not dry_run:
        try:
            from core import source_registry as _sr
            _rc = _sr.reconcile_in_catalogue()
            if _rc:
                print(f"Registry reconcile · {_rc}")
        except Exception as _re:
            print(f"  (registry reconcile skipped: {_re})", file=sys.stderr)

    # Duplicate reconciliation — the ingest gate only dedups a NEW candidate against
    # existing rows; it can't merge two rows that are BOTH already stored (e.g. a
    # migration stub + a later auto-scan of the same call). Sweep all rows post-scan so
    # such pairs collapse to one canonical. Screening runs only (extract_only writes the
    # separate global store, not rfp_submissions).
    if not dry_run and not extract_only:
        try:
            from core.deduplicator import reconcile_duplicates
            _dr = reconcile_duplicates(dry_run=False)
            if _dr.get("flagged"):
                print(f"Duplicate reconcile · flagged {_dr['flagged']} · "
                      f"gap-filled {_dr['filled']} canonical(s)")
        except Exception as _de:
            print(f"  (duplicate reconcile skipped: {_de})", file=sys.stderr)

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
    ap.add_argument("--extract-only", action="store_true",
                    help="Pure extraction: crawl + extract into the global store, "
                         "no per-tenant Screened insert (screening is a separate run).")
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
        extract_only=args.extract_only,
    )
    # The scan COMPLETED (per-source errors are already caught + counted above).
    # Flush, then HARD-exit 0 so a flaky native-lib teardown (Playwright /
    # asyncio) or atexit hook can't raise during interpreter shutdown and set a
    # non-zero exit code — which was tripping the "Extraction exited with errors"
    # banner on otherwise-clean runs. If run() itself failed it raised above and
    # we never reach here (so a real failure still surfaces as a non-zero exit).
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
