"""Backfill LLM review-synthesis onto existing rfp_submissions rows — BLANK-ONLY, safe.

Every row in rfp_submissions has already passed the gate (Decline/Park/Proceed), so all
qualify. For each row we run core.llm_synthesis once and FILL ONLY the fields that are
currently blank — a populated value (human or prior) is NEVER overwritten:

  * brief_description        * how_to_apply            * key_risks
  * call_domain_areas        * compliance_requirements * decision_note (from rationale)
  * application_checklist    * eligibility_specifics   * apply_url (→ opportunity_link)

This script does NOT re-score: it never re-derives qualification/capacity/cofinancing and
never touches alignment_score or auto_recommendation. (An earlier version did, which could
flip a live decision from non-deterministic LLM output — see spawn_task task_29c95605 and
the memory note. Use the live pipeline / Review UI to re-score, not a bulk backfill.)

DRY-RUN by default: it only reports what it WOULD fill. Pass --apply to write. Scoped to
ONE tenant (synthesis reads that tenant's org profile) via --tenant; without it the run is
tenant-less and fail-closed get_client() will see no rows.

Usage:
    python scripts/backfill_synthesis.py --tenant <slug-or-id> [--limit N] [--workers 4]
    python scripts/backfill_synthesis.py --tenant <slug-or-id> --apply
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

# rfp_submissions column  ←  synthesize() output key. apply_url is special (falls back to
# the opportunity link, not an LLM field). Every one is filled ONLY when the row's current
# value is blank.
_FILL_FIELDS = {
    "brief_description": "brief_description",
    "call_domain_areas": "call_domain_areas",
    "how_to_apply": "how_to_apply",
    "compliance_requirements": "compliance_requirements",
    "application_checklist": "application_checklist",
    "eligibility_specifics": "eligibility_specifics",
    "key_risks": "key_risks",
    "decision_note": "decision_rationale",
    "apply_url": None,
}


def _is_blank(v) -> bool:
    """A field counts as blank (safe to fill) if None, an empty/whitespace string, or an
    empty list/dict. A populated value is left untouched."""
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, tuple, dict)):
        return len(v) == 0
    return False


def _one(row: dict, org: dict) -> tuple[str, dict | None, dict | None]:
    """Synthesise once; return a BLANK-ONLY update for this row (or None if nothing to
    fill). Never overwrites a populated field; never re-scores; never touches
    auto_recommendation / alignment_score / the eligibility gate."""
    crit = {k: row.get(k) for k in _CRIT}
    syn = llm_synthesis.synthesize(row, org, row.get("auto_recommendation"), crit)
    if not syn:
        return row["uid"], None, None
    usage = {"prompt_tokens": syn.get("_prompt_tokens"),
             "completion_tokens": syn.get("_completion_tokens")}
    upd: dict = {}
    for col, synkey in _FILL_FIELDS.items():
        if not _is_blank(row.get(col)):
            continue                                   # human / prior value wins
        val = row.get("opportunity_link") if col == "apply_url" else syn.get(synkey)
        if val:
            upd[col] = val
    return row["uid"], (upd or None), usage


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=None,
                    help="tenant slug or id to scope this backfill to (required to see rows)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--apply", action="store_true",
                    help="write changes (default: dry-run, report only)")
    args = ap.parse_args(argv)

    if not llm_synthesis.is_enabled():
        print("LLM synthesis disabled (no LLM_JUDGE_/LLM_SYNTH_ endpoint). Aborting.")
        return 1

    # Scope the whole run (reads, org profile, writes) to the target tenant.
    _tok = None
    if args.tenant:
        from auth import tenant_context as _tc
        tid = args.tenant
        try:
            resolved = _tc.resolve_tenant_by_key(args.tenant)
            if resolved and resolved.get("id"):
                tid = resolved["id"]
        except Exception:
            pass
        _tok = _tc.set_tenant_override(tid)
        print(f"Scoped to tenant {args.tenant} ({tid}).")
    else:
        print("WARNING: no --tenant given; fail-closed get_client() will likely see 0 rows.")

    try:
        sb = get_client()
        org = orgp.get_profile()
        rows = sb.table("rfp_submissions").select("*").order(
            "created_at", desc=True).limit(5000).execute().data or []
        if args.limit:
            rows = rows[:args.limit]
        print(f"Blank-only synthesis backfill on {len(rows)} screened row(s), "
              f"workers={args.workers}" + ("" if args.apply else "  [DRY-RUN]"))

        done = wrote = 0
        calls_with_usage = 0
        total_prompt = total_completion = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_one, r, org): r for r in rows}
            for f in as_completed(futs):
                uid, upd, usage = f.result()
                done += 1
                if usage and usage.get("prompt_tokens") is not None:
                    calls_with_usage += 1
                    total_prompt += usage["prompt_tokens"]
                    total_completion += usage.get("completion_tokens") or 0
                if upd:
                    if args.apply:
                        sb.table("rfp_submissions").update(upd).eq("uid", uid).execute()
                    wrote += 1
                    print(f"  [{done}/{len(rows)}] {uid}: fill {', '.join(sorted(upd.keys()))}")
                else:
                    print(f"  [{done}/{len(rows)}] {uid}: (nothing blank to fill)")
        print(f"\nDone. {wrote}/{len(rows)} rows {'updated' if args.apply else 'would be filled'}.")
        if calls_with_usage:
            print(
                f"Token cost — {calls_with_usage} call(s) reported usage: "
                f"prompt {total_prompt} total / {total_prompt / calls_with_usage:.0f} avg, "
                f"completion {total_completion} total / {total_completion / calls_with_usage:.0f} avg."
            )
        return 0
    finally:
        if _tok is not None:
            from auth import tenant_context as _tc
            _tc.reset_tenant_override(_tok)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
