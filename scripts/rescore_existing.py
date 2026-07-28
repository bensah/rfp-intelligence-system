"""Re-apply the 2026-06-26 decision rule to EXISTING rfp_submissions rows.

The new rule (fatal-factor gate + composite bands) only fires on the NEXT scan;
rows already in the table keep what they were stored with. This re-evaluates
every row IN PLACE.

Two modes:
  * default        — rewrite ONLY auto_recommendation + alignment_score (re-run
                     the fatal-gate + bands over the STORED criteria labels).
  * --rederive     — ALSO re-derive the 9 criteria from the current org × RFP ×
                     donor for ALL rows, so the STORED columns equal the single
                     live derivation the Review screen shows (stored == displayed
                     everywhere). The criteria are the system's objective auto-
                     assessment; the human's DECISION, rationale and risks are in
                     separate columns and are NEVER touched. This clears stale auto
                     labels (e.g. an old "Current/past grantee" that no longer
                     matches funder_history) even on human-reviewed rows.

NEVER touches the human `decision`, key_risks, decision_note, decision_overridden_by,
or other curated fields. Reliable on Windows: donors are prefetched once, scoring runs in threads
(no DB), and writes happen serially in the main thread with retry (the Supabase
HTTP/2 client is not safe for concurrent writes → WinError 10035).

Usage:
    python scripts/rescore_existing.py --dry-run
    python scripts/rescore_existing.py
    python scripts/rescore_existing.py --rederive          # also refresh stale auto labels
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
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

from core import criteria_derive as cdv
from core import matching as mm
from core import org_profile as orgp
from core.auto_scorer import (recommend_from_composite, _is_blank_cheque,
                              _full_text, _CRITERION_DB_VOCAB)
from core.scorer import CRITERIA
from db.supabase_client import get_client


def _norm(s) -> str:
    return str(s or "").strip().lower()


def _match_donor(donors: list[dict], funding_agency: str | None) -> dict | None:
    """Best donor_intel row for a funding agency — mirrors the live `.ilike`
    lookup, against a prefetched list (no per-row DB call → thread-safe)."""
    fa = _norm(funding_agency)
    if not fa:
        return None
    for d in donors:
        dn = _norm(d.get("donor"))
        if dn and (dn == fa or dn in fa or fa in dn):
            return d
    return None


def _evaluate(row: dict, org: dict, org_set: dict, donors: list[dict],
              rederive: bool) -> tuple[str, dict | None, str]:
    """Pure-CPU: returns (uid, update-or-None, label). No DB access."""
    donor = _match_donor(donors, row.get("funding_agency"))
    crit = {k: row.get(k) for k in CRITERIA}
    upd: dict = {}

    # Re-derive the 9 criteria so the STORED columns match the single live
    # derivation (the Review screen reads the same derivation, so stored ==
    # displayed everywhere). Applies to ALL rows: the criteria are the system's
    # objective auto-assessment — the human's DECISION, rationale and risks live in
    # separate columns and are NEVER touched here. (A row's stale auto criteria
    # persist even on human-reviewed rows, which is exactly the drift this fixes.)
    if rederive:
        try:
            derived = cdv.derive_criteria(row, org, donor, org_set, policies={})
            for k, v in derived.items():
                if v is None:
                    continue
                db_v = _CRITERION_DB_VOCAB.get(v, v)
                crit[k] = v
                if db_v != row.get(k):
                    upd[k] = db_v
        except Exception:
            pass

    m = mm.composite_match({**row, **crit}, org, donor, org_set)
    is_fatal, trigger = cdv.fatal_decline(org, row, donor, org_set)
    new_rec = recommend_from_composite(
        crit, m["composite"], fatal=is_fatal,
        below_award_floor=cdv.below_award_floor(row, org))
    new_score = round(m["composite"], 1)

    # Parity with auto_score's post-rules so a re-score never diverges from a
    # fresh scan (thin row not hard-declined; true blank-cheque declined).
    if not is_fatal and new_rec == "Decline" and len(_full_text(row).strip()) < 200:
        new_rec = "Park"
    if _is_blank_cheque(row):
        new_rec = "Decline"

    if new_rec != (row.get("auto_recommendation") or None):
        upd["auto_recommendation"] = new_rec
    try:
        if abs(float(row.get("alignment_score") or 0) - new_score) >= 0.05:
            upd["alignment_score"] = new_score
    except (TypeError, ValueError):
        upd["alignment_score"] = new_score
    reason = f"🔒 {trigger}" if is_fatal else f"composite {new_score}"
    return row["uid"], (upd or None), f"{new_rec} ({reason})"


def _write_with_retry(sb, uid: str, upd: dict, tries: int = 4) -> bool:
    for i in range(tries):
        try:
            sb.table("rfp_submissions").update(upd).eq("uid", uid).execute()
            return True
        except Exception as exc:
            if i == tries - 1:
                print(f"  ! {uid}: write failed after {tries} tries — {exc}")
                return False
            time.sleep(1.5 * (i + 1))
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rederive", action="store_true",
                    help="also re-derive the 9 criteria for ALL rows so stored == "
                         "the live derivation (decision/notes/risks untouched)")
    args = ap.parse_args(argv)

    sb = get_client()
    org = orgp.get_profile()
    try:
        from core import settings as _settings
        org_set = _settings.get_org()
    except Exception:
        org_set = {}

    rows = (sb.table("rfp_submissions").select("*")
            .order("created_at", desc=True).limit(5000).execute().data or [])
    if args.limit:
        rows = rows[:args.limit]
    donors = (sb.table("donor_intel").select("*").limit(5000).execute().data or [])
    print(f"Re-scoring {len(rows)} row(s) against {len(donors)} donor profiles"
          f"{' [DRY-RUN]' if args.dry_run else ''}"
          f"{' [+rederive]' if args.rederive else ''} — human decision untouched.")

    # 1) compute everything in parallel (no DB in here → thread-safe)
    results: list[tuple[str, dict | None, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_evaluate, r, org, org_set, donors, args.rederive)
                for r in rows]
        for f in as_completed(futs):
            results.append(f.result())

    # 2) write serially with retry (Supabase HTTP/2 isn't concurrency-safe)
    transitions: Counter = Counter()
    by_uid = {r["uid"]: r for r in rows}
    changed = wrote = 0
    for uid, upd, label in results:
        if not upd:
            continue
        changed += 1
        if "auto_recommendation" in upd:
            old = by_uid[uid].get("auto_recommendation") or "—"
            transitions[f"{old} → {upd['auto_recommendation']}"] += 1
        if args.dry_run:
            print(f"  {uid}: {', '.join(upd)} → {label}")
        elif _write_with_retry(sb, uid, upd):
            wrote += 1
            print(f"  {uid}: {', '.join(upd)} → {label}")

    if args.dry_run:
        print(f"\nWould change {changed} of {len(rows)} row(s).")
    else:
        print(f"\nWrote {wrote}/{changed} changed row(s) of {len(rows)}.")
    if transitions:
        print("Auto-decision transitions:")
        for k, v in sorted(transitions.items(), key=lambda x: -x[1]):
            print(f"  {v:>4}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
