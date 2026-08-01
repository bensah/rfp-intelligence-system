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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="max rows to synthesise (0 = all)")
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
    for r in todo:
        syn = None
        try:
            syn = llm_synthesis.synthesize_store(_candidate(r))
        except Exception as exc:
            print(f"  ! synth error {r.get('uid')}: {exc}")
        brief = (syn or {}).get("brief_description")
        if not brief:
            fail += 1
            continue
        done += 1
        if done <= 10:
            print(f"  {r.get('uid')}: {str(r.get('brief_description'))[:60]!r} -> {brief[:80]!r}")
        if args.apply:
            try:
                sb.table("extracted_solicitations").update(
                    {"brief_description": brief}).eq("uid", r.get("uid")).execute()
            except Exception as exc:
                print(f"    ! update failed {r.get('uid')}: {exc}")
    verb = "synthesised + wrote" if args.apply else "would synthesise"
    print(f"\n{verb} {done} brief(s); {fail} could not be synthesised (left as-is).")
    if not args.apply and done:
        print("Re-run with --apply to write. Then re-run scripts/backfill_synthesis.py "
              "for the per-tenant rfp_submissions rows.")


if __name__ == "__main__":
    main()
