"""Prune stale, now-ineligible auto-rows from the Screened table (rfp_submissions).

Why: earlier screening leaked rows that the corrected eligibility gate now rejects
(US-domestic grants.gov calls, recognition prizes, forthcoming announcements). Those
rows won't self-delete — "My eligible funding" only adds. This prunes them.

SAFE BY DESIGN:
  * Only touches rows with source='auto' (never migration / human-entered rows).
  * Skips any row showing human review (a `decision`, decision_note, amount_requested,
    a non-default donor_decision, or a decision override).
  * Deletes ONLY when the row's CURATED-STORE counterpart (extracted_solicitations,
    matched by normalised link) now FAILS is_eligible(geo_org_gates=True). Orphans
    (no store row) are left alone.

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
from core.auto_scorer import is_eligible
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

    to_delete, skipped_human, no_store = [], 0, 0
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
        ok, reason = is_eligible(cand, pol, geo_org_gates=True)
        if not ok:
            to_delete.append((r, reason))

    print(f"Would delete {len(to_delete)} now-ineligible auto row(s). "
          f"(skipped {skipped_human} human-touched, {no_store} not-in-store/orphan)\n")
    for r, reason in to_delete[:40]:
        print(f"  ✗ {str(r.get('opportunity_title'))[:46]:46} "
              f"{str(r.get('funding_agency'))[:22]:22} — {reason[:48]}")
    if len(to_delete) > 40:
        print(f"  … and {len(to_delete) - 40} more")

    if not args.apply:
        print("\nDRY-RUN — nothing deleted. Re-run with --apply to delete.")
        return 0
    deleted = 0
    for r, _ in to_delete:
        try:
            sb.table("rfp_submissions").delete().eq("id", r["id"]).execute()
            deleted += 1
        except Exception as exc:
            print(f"  delete failed for {r.get('id')}: {exc}")
    print(f"\nDeleted {deleted} row(s). Run “My eligible funding” to refresh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
