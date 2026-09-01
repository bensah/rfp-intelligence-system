"""Targeted rescore for the submissions whose award values were just backfilled
(scripts/backfill_award_ranges.py). Award size feeds below_award_floor / PREFER-6 /
MUST-3, so their STORED auto_recommendation + alignment_score + criteria can be stale
until the next scan. This re-derives them IN PLACE — each row against ITS OWN tenant's
org profile (the 6 rows span several tenants) — using the SAME `_evaluate` the general
rescore uses, so stored == the live Review derivation.

NEVER touches the human decision / notes / risks (all these rows are decision=None anyway).
Dry-run by default; --apply to write.

USAGE:
  python scripts/rescore_backfilled_awards.py
  python scripts/rescore_backfilled_awards.py --apply
  python scripts/rescore_backfilled_awards.py --uids AS-...,AS-...
"""
from __future__ import annotations

import argparse
import sys
import io
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from db.supabase_client import service_client
from core import org_profile as orgp
from core import settings as settings_mod
from scripts.rescore_existing import _evaluate, _write_with_retry

# The submissions repaired by the award backfill (2026-09-01).
DEFAULT_UIDS = [
    "AS-260901-0732104", "AS-260814-0748326", "AS-260724-084000",
    "AS-260801-000127", "AS-260814-0748325", "AS-260801-000126",
]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--uids", default="", help="comma-separated override")
    args = ap.parse_args(argv)
    uids = [u.strip() for u in args.uids.split(",") if u.strip()] or DEFAULT_UIDS

    sb = service_client()
    rows = (sb.table("rfp_submissions").select("*").in_("uid", uids).execute().data or [])
    donors = (sb.table("donor_intel").select("*").limit(5000).execute().data or [])
    print(f"{'APPLY' if args.apply else 'DRY-RUN'} — rescoring {len(rows)} row(s) "
          f"against {len(donors)} donor profiles, per-tenant. Human decision untouched.\n")

    by_tenant: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_tenant[r.get("tenant_id")].append(r)

    wrote = changed = 0
    for tid, trows in by_tenant.items():
        org = orgp.get_profile(tid)
        try:
            org_set = settings_mod.get_org(tid)
        except Exception:
            org_set = {}
        print(f"tenant {tid} — {len(trows)} row(s)")
        for row in trows:
            # rederive=True so the 9 criteria pick up the new award size, then the
            # fatal-gate + composite bands re-run (identical to a fresh scan).
            uid, upd, label = _evaluate(row, org, org_set, donors, rederive=True)
            if not upd:
                print(f"  = {uid}: no change ({label})")
                continue
            changed += 1
            old_rec = row.get("auto_recommendation") or "—"
            old_sc = row.get("alignment_score")
            print(f"  → {uid}: {old_rec}/{old_sc} ⇒ {label}  cols={list(upd)}")
            if args.apply and _write_with_retry(sb, uid, upd):
                wrote += 1

    print(f"\n{'WROTE ' + str(wrote) if args.apply else 'WOULD CHANGE ' + str(changed)}"
          f" of {len(rows)} row(s){'' if args.apply else ' — re-run with --apply'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
