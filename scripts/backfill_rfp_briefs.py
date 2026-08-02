"""Backfill: replace RAW/empty briefs on SCREENED rfp_submissions rows with a clean summary.

Why: rfp_submissions.brief_description is copied from the store at screening time. Rows
screened BEFORE the store carried a synthesised brief kept the raw attachment/legalese text
("[General_conditions.pdf] … 1. Legal Status … 1.1 …"). This walks rfp_submissions and, for
every row whose brief looks RAW (core.records.looks_raw_brief), writes a CLEAN brief.

Brief-ONLY by design: it writes exactly one column — brief_description (plain `text`). It
never touches the jsonb / text[] synthesis fields (call_domain_areas, application_checklist,
…), so it side-steps the jsonb double-encoding writer issue that blocked the full
backfill_synthesis.py. Run that separately once the jsonb writer is fixed if you also want
the richer fields backfilled.

Source of the clean brief, in order:
  1. COPY the matching store row's brief (extracted_solicitations.opportunity_url ==
     rfp.opportunity_link) when that store brief is itself clean — reuses the store's
     synthesis (and its better raw_text grounding), no LLM call.
  2. SYNTHESISE from the store row's raw_text (best grounding) when the store brief is raw.
  3. SYNTHESISE from the rfp row's own brief_description as a last resort.
Rows are left as-is when nothing clean can be produced, so a partial run never blanks a good
brief. Idempotent — only touches raw/empty briefs.

Usage:
    python scripts/backfill_rfp_briefs.py                 # dry-run (report only)
    python scripts/backfill_rfp_briefs.py --apply         # write clean briefs
    python scripts/backfill_rfp_briefs.py --apply --limit 50 --sleep 1.0
"""
from __future__ import annotations

import argparse
import sys
import time
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

from core import llm_synthesis
from core.records import looks_raw_brief, clean_brief
from db.supabase_client import service_client, safe_execute


def _candidate_from_store(store_row: dict) -> dict:
    """Synthesis input grounded on the STORE row's raw_text (the fullest text we have)."""
    return {
        "opportunity_title": store_row.get("opportunity_name"),
        "opportunity_link": store_row.get("opportunity_url"),
        "funding_agency": store_row.get("funder_name"),
        "call_geographic_scope": store_row.get("call_geographic_scope"),
        "call_submission_deadline": store_row.get("deadline"),
        "call_award_value": store_row.get("grant_amount"),
        "currency": store_row.get("currency"),
        "_page_text": (store_row.get("raw_text")
                       or store_row.get("brief_description") or ""),
    }


def _candidate_from_rfp(rfp_row: dict) -> dict:
    """Last-resort synthesis input when there's no matching store row."""
    return {
        "opportunity_title": rfp_row.get("opportunity_title"),
        "opportunity_link": rfp_row.get("opportunity_link"),
        "funding_agency": rfp_row.get("funding_agency"),
        "call_geographic_scope": rfp_row.get("call_geographic_scope"),
        "call_submission_deadline": rfp_row.get("call_submission_deadline"),
        "call_award_value": rfp_row.get("call_award_value"),
        "currency": rfp_row.get("currency"),
        "_page_text": rfp_row.get("brief_description") or "",
    }


def _synth_with_retry(cand: dict, *, retries: int, pause: float) -> str | None:
    """Org-neutral synthesis with retry+backoff on TRANSIENT LLM failures. Calls synthesize()
    directly (not synthesize_store) so a deliberate bulk pass is NOT bounded by the per-process
    store cap. Returns a clean brief string or None."""
    for attempt in range(max(1, retries)):
        res = llm_synthesis.synthesize(cand, {}, None)
        brief = (res or {}).get("brief_description")
        if brief and not looks_raw_brief(brief):
            return brief
        if attempt < retries - 1:
            time.sleep(pause * (attempt + 2))
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="max rows to fix (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.7,
                    help="pause between synthesised rows (paces the LLM; default 0.7)")
    ap.add_argument("--retries", type=int, default=3,
                    help="attempts per row on a transient LLM failure (default 3)")
    args = ap.parse_args()

    sb = service_client()
    rfp_rows = safe_execute(sb.table("rfp_submissions").select(
        "uid, opportunity_title, opportunity_link, funding_agency, call_geographic_scope, "
        "call_submission_deadline, call_award_value, currency, brief_description")).data or []
    todo = [r for r in rfp_rows if looks_raw_brief(r.get("brief_description"))]
    print(f"rfp_submissions: {len(rfp_rows)} | raw/empty briefs: {len(todo)}")
    if args.limit:
        todo = todo[:args.limit]

    # Index the store by opportunity_url for the COPY path (reuse of the store's synthesis).
    store_rows = safe_execute(sb.table("extracted_solicitations").select(
        "opportunity_url, opportunity_name, funder_name, call_geographic_scope, deadline, "
        "grant_amount, currency, brief_description, raw_text")).data or []
    by_url: dict[str, dict] = {}
    for s in store_rows:
        u = (s.get("opportunity_url") or "").strip()
        if u and u not in by_url:
            by_url[u] = s

    copied = synthd = fail = 0
    total = len(todo)
    if not llm_synthesis.is_enabled():
        print("NOTE: LLM synthesis disabled — only the COPY-from-store path can run "
              "(rows needing synthesis will be skipped).")

    for i, r in enumerate(todo, 1):
        brief = None
        link = (r.get("opportunity_link") or "").strip()
        store = by_url.get(link)
        source = ""
        # 1. Copy a clean store brief (no LLM).
        if store and not looks_raw_brief(store.get("brief_description"), store.get("raw_text")):
            brief = clean_brief(store.get("brief_description"), store.get("raw_text"))
            source = "copy"
        # 2/3. Synthesise (store grounding preferred, else the rfp row's own text).
        if not brief and llm_synthesis.is_enabled():
            cand = _candidate_from_store(store) if store else _candidate_from_rfp(r)
            try:
                brief = _synth_with_retry(cand, retries=args.retries, pause=args.sleep)
                source = "synth"
            except Exception as exc:
                print(f"  ! synth error {r.get('uid')}: {type(exc).__name__}: {exc}")
            time.sleep(max(0.0, args.sleep))

        if not brief:
            fail += 1
            continue
        if source == "copy":
            copied += 1
        else:
            synthd += 1
        if (copied + synthd) <= 12:
            print(f"  {r.get('uid')} [{source}]: "
                  f"{str(r.get('brief_description'))[:50]!r} -> {brief[:80]!r}")
        if args.apply:
            try:
                sb.table("rfp_submissions").update(
                    {"brief_description": brief}).eq("uid", r.get("uid")).execute()
            except Exception as exc:
                print(f"    ! update failed {r.get('uid')}: {exc}")
                if source == "copy":
                    copied -= 1
                else:
                    synthd -= 1
                fail += 1
        if i % 25 == 0 or i == total:
            print(f"  … {i}/{total} — {copied} copied, {synthd} synthesised, {fail} skipped")

    verb = "wrote" if args.apply else "would write"
    print(f"\n{verb} {copied + synthd} clean brief(s) "
          f"({copied} copied from store, {synthd} synthesised); {fail} skipped.")
    if fail:
        print("Skipped rows are usually transient LLM timeouts/rate-limits or rows with no "
              "usable text. Idempotent — RE-RUN to pick up leftovers; raise --sleep if "
              "failures persist.")
    if not args.apply and (copied + synthd):
        print("Re-run with --apply to write.")


if __name__ == "__main__":
    main()
