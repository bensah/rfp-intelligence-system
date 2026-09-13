"""Backfill: replace RAW/empty store briefs with a clean synthesised summary.

Why (BUG 3): extracted_solicitations.brief_description historically held the RAW attachment
text ("[General_conditions.pdf] GENERAL CONDITIONS OF CONTRACT…") or nothing, because
synthesis only ran later at the per-tenant insert. build_record now synthesises a clean,
sentence-case brief for the STORE, but existing rows still carry the raw text. This walks
the store, and for every row whose brief looks RAW or is empty, synthesises a fresh brief
from the row's raw_text and writes it back. Screening then copies the clean brief.

Needs the LLM synthesis env (LLM_SYNTH_* or LLM_JUDGE_*). Rows are skipped (left as-is) when
synthesis fails, so a partial run never blanks a good brief.

Usage:
    python scripts/backfill_store_synthesis.py            # dry-run (report only)
    python scripts/backfill_store_synthesis.py --apply    # write synthesised briefs
    python scripts/backfill_store_synthesis.py --apply --limit 50
"""
from __future__ import annotations

import argparse
from collections import Counter
import re
import sys
import time
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

from core import llm_synthesis
from core.records import looks_raw_brief as _looks_raw   # single source of truth
from db.supabase_client import service_client, safe_execute


def _candidate(row: dict) -> dict:
    return {
        "opportunity_title": row.get("opportunity_name"),
        "opportunity_link": row.get("opportunity_url"),
        "funding_agency": row.get("funder_name"),
        "call_geographic_scope": row.get("call_geographic_scope"),
        "call_submission_deadline": row.get("deadline"),
        "call_award_value": row.get("grant_amount"),
        "currency": row.get("currency"),
        "_page_text": row.get("raw_text") or row.get("brief_description") or "",
    }


def _synth_with_retry(cand: dict, *, retries: int, pause: float):
    """Org-neutral synthesis with retry+backoff on TRANSIENT LLM-endpoint failures.

    Calls synthesize() DIRECTLY (not synthesize_store) so the backfill is NOT bounded by
    the per-process store cap — a bulk backfill is a deliberate, complete pass. synthesize()
    swallows exceptions and returns None on any failure (timeout / connection drop / rate
    limit), so we retry a few times with an increasing sleep to ride out the endpoint
    rate-limiting that a rapid bulk run provokes. Returns the synthesis dict or None."""
    for attempt in range(max(1, retries)):
        res = llm_synthesis.synthesize(cand, {}, None)
        if res and res.get("brief_description"):
            return res
        if attempt < retries - 1:
            time.sleep(pause * (attempt + 2))     # 2x, 3x, … backoff between retries
    return None


def _syn_text(syn: dict, *keys) -> str | None:
    """First non-blank synthesis value across `keys`, joined when several are set.

    Mirrors core.extract.build_record's helper of the same name so a backfilled row and a
    freshly-extracted one carry identically-shaped fields."""
    parts = []
    for k in keys:
        v = syn.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if v and v.lower() not in ("none stated", "none", "n/a", "not stated"):
            parts.append(v)
    return chr(10).join(parts) or None


def _updates(syn: dict, row: dict) -> dict:
    """The fields to write back, BLANK-ONLY beyond the brief itself.

    The original version wrote `brief_description` and nothing else, which left the row
    unable to pass the screening THEME gate: that gate reads `call_domain_areas`, and a row
    whose synthesis failed at scan time has it empty. So the backfill "succeeded" and the
    call stayed invisible — the Coefficient Giving Launchpad RFP sat in the store in exactly
    that state. The synthesis already returns these fields and they were already paid for in
    tokens; this stops discarding them. Existing non-blank values are never overwritten, so
    a re-run cannot degrade a row a human or a better extraction has since improved.
    """
    out: dict = {}
    brief = syn.get("brief_description")
    if brief:
        out["brief_description"] = brief
    areas = [a for a in (syn.get("call_domain_areas") or []) if str(a).strip()]
    if areas and not (row.get("call_domain_areas") or []):
        out["call_domain_areas"] = areas
    if not (row.get("eligibility_other") or "").strip():
        _e = _syn_text(syn, "eligibility_specifics", "compliance_requirements")
        if _e:
            out["eligibility_other"] = _e
    if not (row.get("submission_format") or "").strip():
        _h = _syn_text(syn, "how_to_apply")
        if _h:
            out["submission_format"] = _h
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="max rows to synthesise (0 = all)")
    ap.add_argument("--status", default="Open",
                    help="only rows with this funding_status (default: Open; '*' for all). "
                         "A Closed row cannot be bid on, so synthesising it spends tokens "
                         "on something no reviewer will ever see.")
    ap.add_argument("--sleep", type=float, default=0.7,
                    help="seconds to pause between rows (paces the LLM endpoint; default 0.7)")
    ap.add_argument("--retries", type=int, default=3,
                    help="attempts per row on a transient LLM failure (default 3)")
    args = ap.parse_args()

    if not llm_synthesis.is_enabled():
        print("LLM synthesis is NOT enabled (set LLM_SYNTH_* or LLM_JUDGE_*). Aborting.")
        return

    sb = service_client()
    q = sb.table("extracted_solicitations").select(
        "uid, opportunity_name, opportunity_url, funder_name, call_geographic_scope, "
        "call_domain_areas, eligibility_other, submission_format, funding_status, "
        "deadline, grant_amount, currency, brief_description, raw_text")
    if args.status != "*":
        q = q.eq("funding_status", args.status)
    rows = safe_execute(q).data or []
    todo = [r for r in rows if _looks_raw(r.get("brief_description"), r.get("raw_text"))]
    scope = "" if args.status == "*" else f" (funding_status='{args.status}')"
    print(f"store rows{scope}: {len(rows)} | raw/empty briefs: {len(todo)}")
    if args.limit:
        todo = todo[:args.limit]

    if not args.apply:
        # A DRY RUN MUST NOT CALL THE LLM. It used to synthesise every row and merely skip
        # the write, so "let me just check what this would do" cost a full bulk run in
        # tokens and minutes, and printed nothing until it finished.
        print(f"{chr(10)}Dry run — {len(todo)} row(s) WOULD be synthesised. No LLM calls made.")
        for r in todo[:20]:
            _b = (r.get("brief_description") or "").strip()
            print(f"   {r['uid']}  raw={len(r.get('raw_text') or ''):>6}  "
                  f"brief={'RAW' if _b else 'EMPTY':<5}  "
                  f"areas={len(r.get('call_domain_areas') or [])}  "
                  f"{(r.get('opportunity_name') or '')[:46]}")
        if len(todo) > 20:
            print(f"   … and {len(todo) - 20} more")
        print(chr(10) + "Re-run with --apply to synthesise.")
        return

    done = fail = 0
    fields = Counter()
    total = len(todo)
    for i, r in enumerate(todo, 1):
        syn = None
        try:
            syn = _synth_with_retry(_candidate(r), retries=args.retries, pause=args.sleep)
        except Exception as exc:                    # never let one row abort the run
            print(f"  ! synth error {r.get('uid')}: {type(exc).__name__}: {exc}")
        upd = _updates(syn or {}, r)
        if not upd.get("brief_description"):
            fail += 1
        else:
            done += 1
            if done <= 10:
                print(f"  {r.get('uid')}: +{sorted(upd)} -> "
                      f"{upd['brief_description'][:70]!r}")
            try:
                sb.table("extracted_solicitations").update(upd).eq(
                    "uid", r.get("uid")).execute()
                for k in upd:
                    fields[k] += 1
            except Exception as exc:
                print(f"    ! update failed {r.get('uid')}: {exc}")
                done -= 1
                fail += 1
        if i % 25 == 0 or i == total:               # periodic progress on a long run
            print(f"  … {i}/{total} processed — {done} written, {fail} skipped")
        time.sleep(max(0.0, args.sleep))            # pace so the endpoint doesn't rate-limit us

    print(f"{chr(10)}synthesised + wrote {done} row(s); {fail} could not be synthesised "
          "(transient LLM errors — left as-is).")
    print("  fields written: " + ", ".join(f"{k}={v}" for k, v in sorted(fields.items())))
    if fail:
        print("The skipped rows are usually transient endpoint timeouts/rate-limits. This "
              "script is idempotent (it only touches raw/empty briefs), so just RE-RUN it to "
              "pick up the leftovers; raise --sleep (e.g. 1.5) if failures persist, or set "
              "LLM_SYNTH_TIMEOUT higher for very long RFPs.")
    if not args.apply and done:
        print("Re-run with --apply to write. Then re-run scripts/backfill_synthesis.py "
              "for the per-tenant rfp_submissions rows.")


if __name__ == "__main__":
    main()
