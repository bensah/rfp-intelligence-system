"""Backfill award VALUES / RANGES for already-stored calls whose amounts live only
on the detail page the original scan never fetched.

Root cause (see core.scraper._scan_grandchallenges / _enrich_candidate): structured
listing handlers stored only the ~1800-char listing summary, so a call that publishes
its award only in a detail-page "Award Structure and Funding Level" tier table
(e.g. Grand Challenges "up to US$300,000 … US$800,000") came out with a blank Value.
The scan code now fetches that body going forward; this script repairs existing rows.

For each target row it:
  1. re-fetches the FULL detail body (Grand Challenges via __NEXT_DATA__),
  2. runs the deterministic LLM judge (temp 0) to read funding_tiers + amount,
  3. derives floor/ceiling with core.extract.tiers_to_bounds (same logic as the scan),
  4. grounds every figure in the fetched text (drops anything not literally present),
  5. FILLS ONLY BLANK value columns on extracted_solicitations AND the matching
     rfp_submissions rows (never overwrites a human-entered figure).

Scoring is NOT re-run here (award size feeds PREFER-6 / MUST-3): that is a separate,
deliberate step — this script only repairs the stored facts. The synthesised brief
(which may still say "no amount disclosed") regenerates on the next full scan.

USAGE:
  python scripts/backfill_award_ranges.py            # DRY-RUN (default) — prints plan
  python scripts/backfill_award_ranges.py --apply     # write the changes
  python scripts/backfill_award_ranges.py --source grandchallenges.org   # URL filter
"""
from __future__ import annotations

import argparse
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from db.supabase_client import service_client
from core.scraper import _gc_challenge_text
from core import extract, llm_judge

VALUE_COLS_STORE = ("grant_amount", "currency", "call_award_floor",
                    "call_award_ceiling", "funding_tiers")
VALUE_COLS_SUB = ("call_award_value", "currency", "call_award_floor",
                  "call_award_ceiling", "funding_tiers")


def _detail_text(url: str) -> str | None:
    if "grandchallenges.org" in (url or "").lower():
        return _gc_challenge_text(url)
    # Generic fallback: a plain fetch + strip. (GC is the known case; extend as needed.)
    try:
        import requests
        import re
        from bs4 import BeautifulSoup
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)[:20000]
    except Exception:
        return None


def _extract_values(title: str, text: str) -> dict | None:
    """Judge the fetched text → grounded {grant_amount, currency, floor, ceiling, tiers}."""
    j = llm_judge.judge({"opportunity_title": title, "_page_text": text,
                         "brief_description": text}, {})
    if not j:
        return None
    tiers = j.get("funding_tiers") or []
    floor, ceil = extract.tiers_to_bounds(tiers)
    amt = extract._amount_val(j.get("call_award_value")) or ceil
    # Ground every figure in the fetched text — drop hallucinations.
    if amt is not None and not extract._amount_grounded(amt, text):
        amt = None
    if floor is not None and not extract._amount_grounded(floor, text):
        floor = None
    if ceil is not None and not extract._amount_grounded(ceil, text):
        ceil, tiers = None, []
    if not any((amt, floor, ceil)):
        return None
    return {"grant_amount": amt, "call_award_value": amt,
            "currency": j.get("currency") or "USD",
            "call_award_floor": floor, "call_award_ceiling": ceil,
            "funding_tiers": tiers}


def _fill_blank_updates(existing: dict, new: dict, cols) -> dict:
    """Only columns that are currently blank on the row and have a new value."""
    out = {}
    for k in cols:
        cur = existing.get(k)
        blank = cur in (None, "", 0, "0") or cur == []
        if blank and new.get(k) not in (None, "", [], 0):
            out[k] = new[k]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--source", default="grandchallenges.org",
                    help="opportunity_url substring filter")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    c = service_client()

    rows = (c.table("extracted_solicitations")
            .select("uid,opportunity_name,opportunity_url,grant_amount,currency,"
                    "call_award_floor,call_award_ceiling,funding_tiers")
            .ilike("opportunity_url", f"%{args.source}%")
            .is_("call_award_ceiling", "null")
            .limit(args.limit).execute()).data or []
    print(f"{'APPLY' if args.apply else 'DRY-RUN'} — {len(rows)} store rows "
          f"matching '{args.source}' with no range\n")

    store_updated = sub_updated = 0
    for row in rows:
        url = row.get("opportunity_url") or ""
        name = (row.get("opportunity_name") or "")[:70]
        text = _detail_text(url)
        if not text:
            print(f"  · SKIP (no detail text)  {name}")
            continue
        vals = _extract_values(row.get("opportunity_name") or "", text)
        if not vals:
            print(f"  · SKIP (no grounded amount)  {name}")
            continue
        rng = (extract._amount_val(vals["call_award_floor"]),
               extract._amount_val(vals["call_award_ceiling"]))
        disp = (f"{rng[0]:,.0f}–{rng[1]:,.0f}" if rng[0] and rng[1] and rng[0] != rng[1]
                else f"{(vals['grant_amount'] or rng[1] or 0):,.0f}")
        store_upd = _fill_blank_updates(row, vals, VALUE_COLS_STORE)
        print(f"  · {name}\n      → {vals['currency']} {disp}  "
              f"(tiers: {len(vals['funding_tiers'])})  store cols: {list(store_upd)}")

        # matching submissions (by URL — extraction_uid is not populated for these)
        subs = (c.table("rfp_submissions")
                .select("uid," + ",".join(VALUE_COLS_SUB))
                .eq("opportunity_link", url).execute()).data or []
        sub_plans = [(s["uid"], _fill_blank_updates(s, vals, VALUE_COLS_SUB)) for s in subs]
        sub_plans = [(u, upd) for u, upd in sub_plans if upd]
        for u, upd in sub_plans:
            print(f"        submission {u}: {list(upd)}")

        if args.apply:
            if store_upd:
                c.table("extracted_solicitations").update(store_upd).eq(
                    "uid", row["uid"]).execute()
                store_updated += 1
            for u, upd in sub_plans:
                c.table("rfp_submissions").update(upd).eq("uid", u).execute()
                sub_updated += 1

    print(f"\n{'WROTE' if args.apply else 'WOULD WRITE'}: "
          f"{store_updated if args.apply else 'n/a'} store rows, "
          f"{sub_updated if args.apply else 'n/a'} submissions "
          f"({'dry-run — re-run with --apply' if not args.apply else 'done'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
