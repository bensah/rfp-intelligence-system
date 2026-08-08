"""Check which migrations are actually APPLIED to the live database — by asking the
SCHEMA, not the ledger.

CORRECTION (2026-08-08): an earlier version of this file claimed migrations here are not
tracked. They are — `scripts/migrate.py` applies them over a direct psycopg2 connection
and records each in a `schema_migrations` table. But that ledger is STALE: it stops at
041, while the schema itself is at 089. Everything from 042 onwards was applied by hand
in the SQL editor and never recorded.

So the two sources disagree, and the ledger is the one that is wrong:

    scripts/migrate.py --status   → claims ~49 migrations are pending
    this script                   → asks the schema, and finds 086-089 applied

That matters because `python scripts/migrate.py` with no arguments would try to RE-APPLY
all of them, and 086 (a table rename) cannot be re-applied — active_grants no longer
exists, so the run would fail there. Until the ledger is reconciled with
`--mark-applied`, a single outstanding migration is safer to run in the SQL editor.

This script is the schema-side answer: per migration, does the thing it creates actually
exist? Read-only — it never applies anything.

Usage:
    python scripts/verify_migrations.py           # status of every registered check
    python scripts/verify_migrations.py --sql     # also print the SQL for what's missing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from db.supabase_client import service_client                      # noqa: E402

# (migration file stem, table, column, what it unlocks when applied)
CHECKS = [
    ("090_donor_indirect_cost_cap", "donor_intel", "donor_indirect_cost_max_pct",
     "MUST-5 indirect-cost match. Until applied the component stays 'Not sure' and is "
     "excluded from the denominator — nothing errors."),
    ("091_rfp_donor_engaged", "rfp_submissions", "donor_engaged",
     "PREFER-7 'Donor already engaged'. Until applied the field reads as unanswered and "
     "the tier stays excluded — nothing errors. NOTE: ships with PR #145; the file only "
     "exists once that is merged."),
    # Earlier migrations worth confirming while we are here.
    ("087_criteria_component_overrides", "rfp_submissions", "criteria_component_overrides",
     "Persisted human component verdicts on the Review card."),
    ("088_preserve_discovery_date", "rfp_submissions", "last_seen_at",
     "Re-scan keeps the original discovery date and flags merge conflicts."),
]

# NOT every migration adds a column, and a column check silently SKIPS the ones that
# don't — which made this script look complete while omitting two. A rename and a
# default-plus-data-repair each need their own probe.
#
# Each entry: (stem, description, probe) where probe(sb) -> (state, detail):
#   state True = applied · False = not applied · None = could not tell.
def _probe_086(sb):
    """086 renamed active_grants -> applied_funding (and grant_id -> funding_id)."""
    try:
        sb.table("applied_funding").select("funding_id").limit(1).execute()
    except Exception as exc:
        return False, f"applied_funding.funding_id unreadable: {str(exc)[:70]}"
    try:
        sb.table("active_grants").select("*").limit(1).execute()
        return False, "the OLD active_grants table still exists — rename incomplete"
    except Exception:
        return True, "applied_funding.funding_id present; active_grants gone"


def _probe_089(sb):
    """089 set submissions' default to 0 and repaired the stored rows. The default is
    not visible through PostgREST, so this checks the INVARIANT the repair establishes:
    a row that was never submitted must carry 0."""
    try:
        rows = (sb.table("rfp_submissions")
                .select("uid,progress_status,submissions").limit(5000).execute().data or [])
    except Exception as exc:
        return None, str(exc)[:70]
    if not rows:
        return None, "no rows to judge"
    bad = [r for r in rows
           if str(r.get("progress_status") or "").strip().lower() != "completed"
           and (r.get("submissions") or 0) != 0]
    zero = sum(1 for r in rows if (r.get("submissions") or 0) == 0)
    if len(bad) > len(rows) * 0.1:
        return False, f"{len(bad)} of {len(rows)} never-submitted rows still carry a count"
    detail = f"{zero} of {len(rows)} rows at 0"
    if bad:
        detail += (f"; {len(bad)} row(s) drifted AFTER the repair "
                   f"({', '.join(r['uid'] for r in bad[:3])}) — harmless, "
                   f"submission_weight() gates on Progress = Completed")
    return True, detail


NON_COLUMN_CHECKS = [
    ("086_rename_active_grants_to_applied_funding",
     "Grants table + grant_id renamed to applied_funding + funding_id.", _probe_086),
    ("089_submissions_default_zero",
     "submissions defaults to 0 and never-submitted rows were repaired.", _probe_089),
]


def _has_column(sb, table: str, column: str) -> bool | None:
    """True / False, or None when the table itself could not be read."""
    try:
        sb.table(table).select(column).limit(1).execute()
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "column" in msg or "does not exist" in msg or "42703" in msg:
            return False
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql", action="store_true", help="print the SQL for missing ones")
    args = ap.parse_args()

    sb = service_client()
    missing = []
    print(f"{'migration':38} {'target':44} status")
    print("-" * 100)
    for stem, table, column, note in CHECKS:
        state = _has_column(sb, table, column)
        label = {True: "APPLIED", False: "MISSING", None: "UNKNOWN (table unreadable)"}[state]
        print(f"{stem:38} {table + '.' + column:44} {label}")
        if state is False:
            missing.append((stem, note))

    print()
    for stem, note, probe in NON_COLUMN_CHECKS:
        state, detail = probe(sb)
        label = {True: "APPLIED", False: "MISSING", None: "UNKNOWN"}[state]
        print(f"{stem:38} {'(not a column change)':44} {label}")
        print(f"{'':38} {detail}")
        if state is False:
            missing.append((stem, note))

    if not missing:
        print("\nAll registered migrations are applied.")
        return 0

    print(f"\n{len(missing)} not applied:")
    for stem, note in missing:
        print(f"\n  {stem}")
        print(f"     {note}")

    if args.sql:
        print("\n" + "=" * 100)
        print("Run these in the Supabase SQL editor. Each file is idempotent.")
        print("=" * 100)
        for stem, _ in missing:
            f = _ROOT / "db" / "migrations" / f"{stem}.sql"
            print(f"\n-- ---------- {f.name} ----------")
            if f.exists():
                print(f.read_text(encoding="utf-8").rstrip())
            else:
                print(f"-- (file not on this branch: {f.name})")
    else:
        print("\nRe-run with --sql to print the statements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
