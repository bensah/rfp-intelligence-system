"""Backfill LLM review-synthesis onto existing rfp_submissions rows.

Every row in rfp_submissions has already passed the gate (Decline/Park/Proceed),
so all qualify (rejected candidates were never inserted). For each row we run
core.llm_synthesis once and write:
  * brief_description  — LLM synthesis, ≤1000 chars (replaces the copied site text)
  * program_area       — LLM taxonomy classification (replaces keyword guess)
  * key_risks          — set ONLY if currently blank (human edits win)
  * decision_note      — set ONLY if currently blank (draft rationale; human wins)

Usage:
    python scripts/backfill_synthesis.py [--limit N] [--workers 4] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from core import llm_synthesis, org_profile as orgp
from db.supabase_client import get_client

_CRIT = ("qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing",
         "funding_quality", "funder_relationship", "competitiveness", "bid_effort")


def _one(row: dict, org: dict, sb=None, org_set: dict | None = None) -> tuple[str, dict | None]:
    crit = {k: row.get(k) for k in _CRIT}
    syn = llm_synthesis.synthesize(row, org, row.get("auto_recommendation"), crit)
    if not syn:
        return row["uid"], None
    upd: dict = {}
    if syn.get("brief_description"):
        upd["brief_description"] = syn["brief_description"]
    if syn.get("program_areas"):
        upd["program_area"] = syn["program_areas"]
    if syn.get("key_risks") and not (row.get("key_risks") or "").strip():
        upd["key_risks"] = syn["key_risks"]
    if syn.get("decision_rationale") and not (row.get("decision_note") or "").strip():
        upd["decision_note"] = syn["decision_rationale"]
    if syn.get("how_to_apply"):
        upd["how_to_apply"] = syn["how_to_apply"]
    if syn.get("compliance_requirements"):
        upd["compliance_requirements"] = syn["compliance_requirements"]
    if not (row.get("apply_url") or "").strip():
        upd["apply_url"] = row.get("opportunity_link")   # portal URL (fallback to call link)
    # Feed LLM-extracted RFP compliance flags into MUST-5 → re-derive + re-score
    # (auto fields only; the human's decision/notes are untouched).
    _flags = syn.get("compliance_flags") or {}
    if _flags:
        import json as _json
        upd["compliance_flags"] = _json.dumps(_flags)   # persist for Review re-merge
    if _flags and sb is not None:
        try:
            from core import criteria_derive as _cdv, matching as _mm
            from core.auto_scorer import recommend_from_composite as _rec
            _dn = None
            _fa = (row.get("funding_agency") or "").strip()
            if _fa:
                _dq = (sb.table("donor_intel").select("*")
                       .ilike("donor", _fa).limit(1).execute().data or [])
                _dn = _dq[0] if _dq else None
            # Re-derive BOTH call-flag-sensitive labels: MUST-1 qualification +
            # MUST-5 cofinancing. Recompute the composite + gate if either changed.
            _cv = {k: row.get(k) for k in _CRIT}
            _changed = False
            _newqual = _cdv.derive_qualification(org, row, _dn, org_set or {},
                                                 rfp_compliance=_flags)
            if _newqual and _newqual != row.get("qualification"):
                _cv["qualification"] = _newqual
                upd["qualification"] = _newqual
                _changed = True
            _newcap = _cdv.derive_capacity(org, row, _dn, org_set or {},
                                           rfp_compliance=_flags)
            if _newcap and _newcap != row.get("capacity"):
                _cv["capacity"] = _newcap
                upd["capacity"] = _newcap
                _changed = True
            _newcof = _cdv.derive_cofinancing(org, row, _dn, rfp_compliance=_flags,
                                              org_settings=org_set or {})
            if _newcof and _newcof != row.get("cofinancing"):
                _cv["cofinancing"] = _newcof
                upd["cofinancing"] = _newcof
                _changed = True
            if _changed:
                _m = _mm.composite_match({**row, **_cv}, org, _dn, org_set or {})
                _isf, _ = _cdv.fatal_decline(org, row, _dn, org_set or {},
                                             rfp_compliance=_flags)
                upd["alignment_score"] = round(_m["composite"], 1)
                upd["auto_recommendation"] = _rec(_cv, _m["composite"], fatal=_isf)
        except Exception:
            pass
    return row["uid"], (upd or None)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not llm_synthesis.is_enabled():
        print("LLM synthesis disabled (no LLM_JUDGE_/LLM_SYNTH_ endpoint). Aborting.")
        return 1
    sb = get_client()
    org = orgp.get_profile()
    try:
        from core import settings as _settings
        org_set = _settings.get_org()
    except Exception:
        org_set = {}
    rows = sb.table("rfp_submissions").select("*").order(
        "created_at", desc=True).limit(5000).execute().data or []
    if args.limit:
        rows = rows[:args.limit]
    print(f"Backfilling synthesis on {len(rows)} screened row(s), workers={args.workers}"
          + (" [DRY-RUN]" if args.dry_run else ""))

    done = wrote = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_one, r, org, sb, org_set): r for r in rows}
        for f in as_completed(futs):
            uid, upd = f.result()
            done += 1
            if upd:
                if not args.dry_run:
                    sb.table("rfp_submissions").update(upd).eq("uid", uid).execute()
                wrote += 1
                print(f"  [{done}/{len(rows)}] {uid}: {', '.join(upd.keys())}")
            else:
                print(f"  [{done}/{len(rows)}] {uid}: (no change)")
    print(f"\nDone. {wrote}/{len(rows)} rows {'would be ' if args.dry_run else ''}updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
