"""Re-validate EXISTING rfp_submissions rows against the CURRENT scan gate.

Detection logic tightens over time (e.g. 2026-06-26: Coefficient Giving fund
PARENT pages are not RFPs; stronger closed-call detection). Rows screened before
a fix stay in the list as false flags. This re-runs `auto_scorer.is_eligible`
(the universal keep-set: not-an-rfp / parent-overview / closed / past-deadline /
off-theme / wrong-language — geography & org gates skipped) over every stored row
and reports the ones that NO LONGER qualify.

Safe by default: --dry-run only REPORTS. --delete removes the failing rows, but
NEVER a row a human has already decided on (decision set) — those are reported as
"kept (human-reviewed)" so you can act on them manually.

Usage:
    python scripts/revalidate_existing.py                 # dry-run report
    python scripts/revalidate_existing.py --delete        # remove auto false-flags
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
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

from core.auto_scorer import is_eligible
from core.policies import get_policies
from db.supabase_client import get_client

# Fields the gate reads — mapped straight from the stored row.
_GATE_FIELDS = ("opportunity_link", "opportunity_title", "brief_description",
                "notes", "call_geographic_scope", "focus_theme", "funding_agency",
                "date_posted", "call_submission_deadline")


def _delete_with_retry(sb, uid: str, tries: int = 4) -> bool:
    for i in range(tries):
        try:
            sb.table("rfp_submissions").delete().eq("uid", uid).execute()
            return True
        except Exception as exc:
            if i == tries - 1:
                print(f"  ! {uid}: delete failed — {exc}")
                return False
            time.sleep(1.5 * (i + 1))
    return False


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delete", action="store_true",
                    help="remove failing rows (skips human-decided rows)")
    args = ap.parse_args(argv)
    dry = not args.delete

    sb = get_client()
    policies = get_policies()
    rows = (sb.table("rfp_submissions").select("*")
            .order("created_at", desc=True).limit(5000).execute().data or [])
    if args.limit:
        rows = rows[:args.limit]
    print(f"Re-validating {len(rows)} row(s) against the current gate"
          f"{' [DRY-RUN]' if dry else ' [DELETE]'}.")

    reasons: Counter = Counter()
    failing, kept_reviewed, deleted = 0, 0, 0
    for row in rows:
        cand = {k: row.get(k) for k in _GATE_FIELDS}
        try:
            ok, reason = is_eligible(cand, policies, geo_org_gates=False)
        except Exception as exc:
            print(f"  ? {row['uid']}: gate error — {exc}")
            continue
        if ok:
            continue
        failing += 1
        reasons[reason.split(" (")[0][:48]] += 1
        # TRUE human-review marker is decision_overridden_by — NOT `decision`,
        # which migration 013 auto-fills from auto_recommendation (so it's set even
        # when no human touched the row).
        reviewed = bool((row.get("decision_overridden_by") or "").strip())
        tag = " [human-reviewed → kept]" if reviewed else ""
        print(f"  ✗ {row['uid']}: {reason}{tag}")
        print(f"      {row.get('opportunity_link')}")
        if reviewed:
            kept_reviewed += 1
            continue
        if not dry and _delete_with_retry(sb, row["uid"]):
            deleted += 1

    print(f"\n{failing} row(s) fail the current gate "
          f"({kept_reviewed} human-reviewed, left in place).")
    if not dry:
        print(f"Deleted {deleted} auto false-flag row(s).")
    if reasons:
        print("By reason:")
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {v:>4}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
