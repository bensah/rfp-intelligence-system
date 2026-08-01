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
from db.supabase_client import service_client, safe_execute

# An attachment-tagged or ALL-CAPS-heavy brief is raw source text, not a summary.
_ATTACH_TAG = re.compile(r"^\s*\[[^\]]+\.(pdf|docx?|xlsx?|zip)\]", re.I)


def _looks_raw(brief: str | None, raw_text: str | None) -> bool:
    b = (brief or "").strip()
    if not b:
        return True                              # empty → synthesise
    if _ATTACH_TAG.search(b):
        return True                              # "[X.pdf] …" attachment dump
    # A brief that's a verbatim prefix of raw_text was copied, not synthesised.
    rt = (raw_text or "").strip()
    if rt and rt[:120].lower() == b[:120].lower():
        return True
    # Heavy ALL-CAPS (legalese headings) → treat as raw.
    words = re.findall(r"[A-Za-z]{3,}", b)
    if words:
        caps = sum(1 for w in words if w.isupper())
        if caps / len(words) > 0.30:
            return True
    return False


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="max rows to synthesise (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.7,
                    help="seconds to pause between rows (paces the LLM endpoint; default 0.7)")
    ap.add_argument("--retries", type=int, default=3,
                    help="attempts per row on a transient LLM failure (default 3)")
    args = ap.parse_args()

    if not llm_synthesis.is_enabled():
        print("LLM synthesis is NOT enabled (set LLM_SYNTH_* or LLM_JUDGE_*). Aborting.")
        return

    sb = service_client()
    rows = safe_execute(sb.table("extracted_solicitations").select(
        "uid, opportunity_name, opportunity_url, funder_name, call_geographic_scope, "
        "deadline, grant_amount, currency, brief_description, raw_text")).data or []
    todo = [r for r in rows if _looks_raw(r.get("brief_description"), r.get("raw_text"))]
    print(f"store rows: {len(rows)} | raw/empty briefs: {len(todo)}")
    if args.limit:
        todo = todo[:args.limit]

    done = fail = 0
    total = len(todo)
    for i, r in enumerate(todo, 1):
        syn = None
        try:
            syn = _synth_with_retry(_candidate(r), retries=args.retries, pause=args.sleep)
        except Exception as exc:                    # never let one row abort the run
            print(f"  ! synth error {r.get('uid')}: {type(exc).__name__}: {exc}")
        brief = (syn or {}).get("brief_description")
        if not brief:
            fail += 1
        else:
            done += 1
            if done <= 10:
                print(f"  {r.get('uid')}: {str(r.get('brief_description'))[:55]!r} -> {brief[:80]!r}")
            if args.apply:
                try:
                    sb.table("extracted_solicitations").update(
                        {"brief_description": brief}).eq("uid", r.get("uid")).execute()
                except Exception as exc:
                    print(f"    ! update failed {r.get('uid')}: {exc}")
                    done -= 1
                    fail += 1
        if i % 25 == 0 or i == total:               # periodic progress on a long run
            print(f"  … {i}/{total} processed — {done} written, {fail} skipped")
        time.sleep(max(0.0, args.sleep))            # pace so the endpoint doesn't rate-limit us

    verb = "synthesised + wrote" if args.apply else "would synthesise"
    print(f"\n{verb} {done} brief(s); {fail} could not be synthesised (transient LLM "
          "errors — left as-is).")
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
