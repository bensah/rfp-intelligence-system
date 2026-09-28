"""Prune stale, now-ineligible auto-rows from the Screened table (rfp_submissions).

Why: earlier screening leaked rows that the corrected eligibility gate now rejects
(US-domestic grants.gov calls, recognition prizes, forthcoming announcements). Those
rows won't self-delete — "My eligible funding" only adds. This prunes them.

SAFE BY DESIGN:
  * Only touches rows with source='auto' (never migration / human-entered rows).
  * Skips any row showing human review (a `decision`, decision_note, amount_requested,
    a non-default donor_decision, or a decision override).
  * Deletes ONLY when the row's CURATED-STORE counterpart (extracted_solicitations,
    matched by normalised link) now FAILS the gate. Orphans (no store row) are
    left alone.
  * TOMBSTONES BEFORE DELETING, and skips any row it could not tombstone. A
    deleted row is remembered only in rfp_seen; without that record the next scan
    re-ingests exactly what this script just removed.

THIS SCRIPT NEARLY DELETED GENUINE CALLS, and the reason is worth keeping in view.
It used to call `is_eligible(cand, pol, geo_org_gates=True)` with no adjudicator.
The theme gate reads `_full_text` - title + brief + scope + funder, ~1100 chars -
and its page-text rescue only runs when the LLM judge is available. So both
Wellcome career-award schemes, whose pages carry 12,000 characters saying "health"
twenty times, came back "theme: no required theme keyword matched" and were listed
for deletion. With the adjudicator they are (True, 'eligible').

A leak costs a reviewer minutes. This costs them the opportunity. So:
  * the gate now runs WITH llm_adjudicate / llm_theme, and
  * a row rejected ONLY on theme is HELD, never deleted, when the judge is
    unavailable - because that verdict was reached without reading the page.

ORDER: run **Run Extraction** first so the store carries the corrected geography
(grants.gov US-default) + prize tags, THEN run this, THEN "My eligible funding".

Usage:
    python scripts/prune_ineligible_screened.py             # dry-run (report only)
    python scripts/prune_ineligible_screened.py --apply     # actually delete
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

from core import extracted_store, scan_pipeline
from core.auto_scorer import insufficient_data_reject, is_eligible
from core.policies import get_policies
from db.supabase_client import get_client


def _human_touched(row: dict) -> bool:
    """True if a person has engaged with the row — never auto-delete those.
    NOTE: `decision` is auto-filled (= auto_recommendation), so it is NOT a
    human-review signal. Real signals: a written rationale, a manual override, a
    requested amount, a non-default donor_decision, or a moved stage/progress."""
    if (row.get("decision_note") or "").strip():
        return True
    if row.get("decision_overridden_by"):
        return True
    if row.get("amount_requested") not in (None, "", 0, "0"):
        return True
    if (row.get("donor_decision") or "").strip() not in ("", "Not submitted"):
        return True
    if (row.get("stage") or "").strip() not in ("", "Identification & screening"):
        return True
    if (row.get("progress_status") or "").strip() not in ("", "Not Started"):
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete (default is a dry-run report).")
    args = ap.parse_args()
    sb = get_client()
    pol = get_policies()

    # Curated store, keyed by normalised link.
    store = {}
    for r in extracted_store.list_extracted(limit=5000):
        link = extracted_store.normalize_url(r.get("opportunity_url") or "")
        if link:
            store[link] = r

    rows = (sb.table("rfp_submissions")
            .select("id,uid,opportunity_title,opportunity_link,funding_agency,source,"
                    "decision,decision_note,decision_overridden_by,amount_requested,"
                    "donor_decision,stage,progress_status,is_duplicate")
            .eq("source", "auto").limit(5000).execute().data or [])
    print(f"auto rows: {len(rows)} · curated store rows: {len(store)}\n")

    # Can the theme gate read the page? If not, a theme-only reject is a verdict
    # reached on ~1100 characters and must not delete anything.
    try:
        from core import llm_judge
        judge_ok = llm_judge.is_enabled()
    except Exception:
        judge_ok = False
    print(f"theme adjudicator available: {judge_ok}"
          + ("" if judge_ok else "  -> theme-only rejects will be HELD, not deleted"))

    to_delete, held, skipped_human, no_store = [], [], 0, 0
    for r in rows:
        if _human_touched(r):
            skipped_human += 1
            continue
        link = extracted_store.normalize_url(r.get("opportunity_link") or "")
        srow = store.get(link)
        if not srow:
            no_store += 1
            continue
        cand = scan_pipeline._candidate_from_extracted(srow)
        cand["_source_class"] = "primary"
        # WITH the adjudicator, so the theme gate can consult the page rather than
        # ruling on the summary alone.
        ok, reason = is_eligible(cand, pol, geo_org_gates=True,
                                 llm_adjudicate=True, llm_theme=True)
        if ok:
            # The same data-sufficiency gate the scan runs for a tenant pipeline:
            # a row we still cannot verify as a real, open call does not belong.
            bad, why = insufficient_data_reject(cand)
            if not bad:
                continue
            reason = why
        if not judge_ok and reason.startswith("theme:"):
            held.append((r, reason))
            continue
        to_delete.append((r, reason, cand))

    print(f"\nWould delete {len(to_delete)} now-ineligible auto row(s). "
          f"(skipped {skipped_human} human-touched, {no_store} not-in-store/orphan, "
          f"{len(held)} held)\n")
    for r, reason, _ in to_delete[:60]:
        print(f"  x {str(r.get('opportunity_title'))[:44]:44} "
              f"{str(r.get('funding_agency'))[:20]:20} - {reason[:52]}")
    if len(to_delete) > 60:
        print(f"  ... and {len(to_delete) - 60} more")
    if held:
        print(f"\nHELD ({len(held)}) - theme reject with no adjudicator, so the page "
              f"was never read. Not deleted:")
        for r, reason in held[:20]:
            print(f"  ? {str(r.get('opportunity_title'))[:44]:44} "
                  f"{str(r.get('funding_agency'))[:20]:20} - {reason[:40]}")

    if not args.apply:
        print("\nDRY-RUN - nothing deleted. Re-run with --apply to delete.")
        return 0

    from core import seen_ledger
    deleted, untombstoned = 0, 0
    for r, _reason, cand in to_delete:
        # Tombstone FIRST. A row we delete without one comes straight back.
        if not seen_ledger.record_decision(r, "declined", reason="pruned_ineligible") \
                and not seen_ledger.record([r], reason="pruned_ineligible"):
            untombstoned += 1
            print(f"  ! not tombstoned, so NOT deleted: {r.get('uid')}")
            continue
        try:
            sb.table("rfp_submissions").delete().eq("id", r["id"]).execute()
            deleted += 1
        except Exception as exc:
            print(f"  delete failed for {r.get('id')}: {exc}")
    print(f"\nDeleted {deleted} row(s); {untombstoned} skipped because they could "
          f"not be tombstoned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
