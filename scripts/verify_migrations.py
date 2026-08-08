"""Check which migrations are actually APPLIED to the live database.

Migrations here are not tracked in a table — the files in db/migrations are applied by
hand in the SQL editor, so the only reliable answer to "is 090 applied?" is to ask the
database whether the column exists. This does exactly that, per migration, and prints
the SQL to run for any that are missing.

Read-only. It never applies anything: the DDL has to go through the SQL editor because
the PostgREST client cannot execute ALTER TABLE.

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
