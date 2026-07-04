"""Measure LLM synthesis TOKEN COST on real long-RFP sources — no DB writes.

Unlike ``backfill_synthesis.py`` (which re-synthesises stored rows, whose ``_page_text``
was never persisted, so it only ever sees the short ``brief_description``), this runs the
LIVE scrape + deep-read path so each candidate carries the FULL page text + folded RFP PDF
in ``_page_text`` — exactly the long-document case the excerpt/anchoring logic in
``core.llm_synthesis._build_excerpt`` exists for. It then calls ``synthesize()`` directly
and reports per-candidate token usage, so the cost/quality tradeoff is measurable on the
inputs that actually trigger it.

Nothing is written to the database: no scan_logs, no rfp_submissions, no extracted store.
The only DB touch is a read of the org profile for the prompt's ORG CONTEXT block.

Requires Chromium (Playwright) for the deep-read PDF fold, and LLM_SYNTH_*/LLM_JUDGE_*
creds for synthesis (same as a real scan). Bounded by --limit and RFPIS_DEEP_READ_MAX.

Usage:
    # By catalogue source name (matched against the active donor_sources set):
    python scripts/measure_synthesis_cost.py --source "Grand Challenges — donor catalog"

    # By a single RFP DETAIL page (most robust — skips listing scrape; best for a
    # known long RFP whose detail page links an attached multi-page RFP PDF):
    python scripts/measure_synthesis_cost.py \
        --detail-url https://gcgh.grandchallenges.org/challenge/estimating-global-burden-diarrheal-diseases

    # By direct listing URL (method defaults to html; per-source handlers route by URL):
    python scripts/measure_synthesis_cost.py --url https://gcgh.grandchallenges.org/challenges

    # Compare NEW anchored-excerpt cost against the OLD flat-16k truncation on the SAME RFPs:
    python scripts/measure_synthesis_cost.py --detail-url <long-rfp-page> --compare
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

# A long RFP (5k+ input tokens through a reasoning model on a cloud endpoint) can
# exceed the pipeline's tight default 60s synthesis timeout — and this tool isn't
# latency-sensitive. Give each call more headroom unless the env already asks for more.
try:
    if int(os.environ.get("LLM_JUDGE_TIMEOUT", "60") or 60) < 180:
        os.environ["LLM_JUDGE_TIMEOUT"] = "180"
except ValueError:
    os.environ["LLM_JUDGE_TIMEOUT"] = "180"

from core import deep_read, llm_synthesis, org_profile as orgp
from core.scraper import scan_source

# The old behaviour before the anchored-excerpt change: a flat prefix slice at the
# previous default cap. Used by --compare to show the before/after token delta on the
# identical long RFP.
_OLD_FLAT_CHARS = 16000


def _resolve_source(source_name: str | None, url: str | None,
                    method: str) -> dict | None:
    """A source dict for scan_source: a catalogue row matched by name, or a
    synthetic one built from --url. Returns None if a named source isn't found."""
    if url:
        return {"name": url, "url": url, "method": method}
    from db.supabase_client import get_client
    from scripts.run_scan import build_scan_sources
    srcs = build_scan_sources(get_client())
    for s in srcs:
        if (s.get("name") or "") == source_name:
            return s
    # Fall back to a case-insensitive substring match for convenience.
    lc = (source_name or "").lower()
    for s in srcs:
        if lc and lc in (s.get("name") or "").lower():
            return s
    print(f"Source not found: {source_name!r}. Available (first 40):")
    for s in srcs[:40]:
        print(f"  - {s.get('name')}")
    return None


def _synth_and_report(cand: dict, org: dict, idx: int, total: int) -> dict | None:
    """Run synthesis on ONE enriched candidate; print token usage + sizes. No DB."""
    title = (cand.get("opportunity_title") or cand.get("opportunity_link") or "?")[:70]
    body = str(cand.get("_page_text") or cand.get("raw_text")
               or cand.get("brief_description") or "")
    excerpt = llm_synthesis._build_excerpt(body, cand)
    anchored = len(body) > llm_synthesis._MAX_INPUT_CHARS
    syn = llm_synthesis.synthesize(cand, org, None, None)
    if not syn:
        print(f"  [{idx}/{total}] {title}\n"
              f"      page_text={len(body)}c  excerpt={len(excerpt)}c  "
              f"anchored={anchored}  → synthesis returned None (disabled/failed)")
        return None
    pt = syn.get("_prompt_tokens")
    ct = syn.get("_completion_tokens")
    print(f"  [{idx}/{total}] {title}\n"
          f"      page_text={len(body)}c  excerpt_sent={len(excerpt)}c  "
          f"anchored_path={anchored}  prompt_tokens={pt}  completion_tokens={ct}")
    return {"prompt": pt or 0, "completion": ct or 0, "has_usage": pt is not None,
            "page_chars": len(body), "excerpt_chars": len(excerpt)}


def _synth_old_flat(cand: dict, org: dict) -> dict | None:
    """Re-run synthesis with the OLD flat-truncation behaviour by monkeypatching
    _build_excerpt to a plain prefix slice — for the --compare before/after delta."""
    orig = llm_synthesis._build_excerpt
    llm_synthesis._build_excerpt = lambda b, c: b[:_OLD_FLAT_CHARS]  # type: ignore
    try:
        syn = llm_synthesis.synthesize(cand, org, None, None)
    finally:
        llm_synthesis._build_excerpt = orig  # type: ignore
    if not syn:
        return None
    return {"prompt": syn.get("_prompt_tokens") or 0,
            "completion": syn.get("_completion_tokens") or 0,
            "has_usage": syn.get("_prompt_tokens") is not None}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--source", help="Catalogue source name (exact or substring match)")
    g.add_argument("--url", help="Scrape this listing URL directly")
    g.add_argument("--detail-url", dest="detail_url",
                   help="Treat this as a single RFP DETAIL page: build one candidate "
                        "and enrich it directly (skips the listing scrape)")
    ap.add_argument("--method", default="html", help="Scrape method for --url (default html)")
    ap.add_argument("--limit", type=int, default=3,
                    help="Max candidates to synthesise (bounds cost/time; default 3)")
    ap.add_argument("--compare", action="store_true",
                    help="Also run the OLD flat-16k truncation on the same RFPs and "
                         "print the before/after token delta")
    args = ap.parse_args(argv)

    # Surface WHY a call produced nothing (timeout / non-JSON) instead of a silent None.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("core.llm_synthesis").setLevel(logging.INFO)

    if not llm_synthesis.is_enabled():
        print("LLM synthesis disabled (no LLM_SYNTH_/LLM_JUDGE_ endpoint). Aborting.")
        return 1
    if not deep_read.available():
        print("WARNING: Chromium/Playwright unavailable — deep-read PDF fold is SKIPPED, "
              "so _page_text will be the scraper's text only (likely short). The anchored "
              "excerpt path may not trigger. Install Playwright + chromium to measure the "
              "true long-RFP case.")

    try:
        org = orgp.get_profile()
    except Exception:
        org = {}

    if args.detail_url:
        # Single detail page — build one candidate; deep_read.enrich follows the
        # page + folds its attached RFP PDF into _page_text.
        cands = [{"opportunity_link": args.detail_url,
                  "opportunity_title": args.detail_url.rsplit("/", 1)[-1].replace("-", " ")}]
        print(f"Detail page: {args.detail_url}\n  → enriching + synthesising …\n")
    else:
        source = _resolve_source(args.source, args.url, args.method)
        if not source:
            return 1
        print(f"Scraping source: {source.get('name')} …")
        cands = scan_source(source)
        print(f"  → {len(cands)} candidate(s) scraped; enriching + synthesising up to "
              f"{args.limit} …\n")

    rows, done = [], 0
    old_rows = []
    for cand in cands:
        if done >= args.limit:
            break
        # Populate _page_text with the FULL page + folded RFP PDF (no DB writes).
        try:
            deep_read.enrich(cand)
        except Exception as exc:
            print(f"  (deep_read.enrich failed: {type(exc).__name__}: {exc})")
        if cand.get("_dead_page"):
            continue                     # skip dead/soft-404 pages, as the scan would
        done += 1
        r = _synth_and_report(cand, org, done, args.limit)
        if r:
            rows.append(r)
        if args.compare:
            o = _synth_old_flat(cand, org)
            if o:
                old_rows.append(o)

    used = [r for r in rows if r["has_usage"]]
    if not used:
        print("\nNo token usage reported (synthesis returned nothing, or the provider "
              "omits usage). Nothing to summarise.")
        return 0

    n = len(used)
    tp = sum(r["prompt"] for r in used)
    tc = sum(r["completion"] for r in used)
    avg_page = sum(r["page_chars"] for r in used) / n
    avg_exc = sum(r["excerpt_chars"] for r in used) / n
    print(f"\nNEW (anchored excerpt) — {n} call(s) with usage:")
    print(f"  prompt     {tp} total / {tp / n:.0f} avg")
    print(f"  completion {tc} total / {tc / n:.0f} avg")
    print(f"  page_text  {avg_page:.0f}c avg   excerpt_sent {avg_exc:.0f}c avg")

    if args.compare:
        oused = [r for r in old_rows if r["has_usage"]]
        if oused:
            on = len(oused)
            otp = sum(r["prompt"] for r in oused)
            otc = sum(r["completion"] for r in oused)
            print(f"\nOLD (flat {_OLD_FLAT_CHARS}-char truncation) — {on} call(s):")
            print(f"  prompt     {otp} total / {otp / on:.0f} avg")
            print(f"  completion {otc} total / {otc / on:.0f} avg")
            if on == n:
                dp = tp - otp
                print(f"\nDelta (new − old): prompt {dp:+d} total / {dp / n:+.0f} avg "
                      f"per call  ({(tp / otp - 1) * 100:+.0f}% prompt tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
