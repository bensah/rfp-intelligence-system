"""RFPIS schema-migration runner.

Applies every `.sql` file under `db/migrations/` to Supabase Postgres in
filename order, tracking which have already been applied in a
`schema_migrations` bookkeeping table so re-runs are no-ops.

Why this exists
---------------
Migrations were previously applied by pasting each `db/migrations/NNN_*.sql`
into Supabase's web SQL Editor by hand. Easy to forget, easy to apply in
the wrong order, no audit trail of who applied what when. This runner is
the same idempotent pattern every database project eventually grows into.

Usage
-----
    # one-time setup: add the Postgres connection string to .env
    # (Supabase dashboard -> Project Settings -> Database -> Connection
    #  String, pick the "Transaction" pooler URL -- port 6543 -- and
    #  inline the password)
    SUPABASE_DB_URL=postgresql://postgres.xxxx:PASS@aws-0-region.pooler.supabase.com:6543/postgres

    # then, from the repo root:
    python scripts/migrate.py                # apply all pending migrations
    python scripts/migrate.py --status       # show applied / pending
    python scripts/migrate.py --dry-run      # print the plan, write nothing

Each migration runs in its own transaction. If a file fails, the
bookkeeping row for it is rolled back too -- re-running picks up where
you left off after you fix the SQL.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Run directly (python scripts/migrate.py) and sys.path[0] is scripts/, not the repo
# root — so put the root on the path BEFORE importing anything from core.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dotenv_compat import load_dotenv   # noqa: E402  (tolerates a venv without
#                                              python-dotenv installed)

# Importing psycopg2 lazily so the helpful error message below fires
# instead of an opaque ImportError when the dep isn't installed yet.
try:
    import psycopg2  # type: ignore
except ImportError:
    print(
        "[migrate] psycopg2 is not installed. Run:\n"
        "    pip install psycopg2-binary",
        file=sys.stderr,
    )
    sys.exit(2)


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
BOOKKEEPING_TABLE = "schema_migrations"


# -- DB connection -----------------------------------------------------------


def _connect():
    load_dotenv()
    dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        print(
            "[migrate] SUPABASE_DB_URL is not set.\n"
            "          Get it from Supabase dashboard -> Project Settings ->\n"
            "          Database -> Connection String -> \"Transaction\" pooler\n"
            "          (port 6543). Add it to your .env (gitignored).",
            file=sys.stderr,
        )
        sys.exit(2)
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


def _ensure_bookkeeping(conn) -> None:
    """Create the schema_migrations table if it doesn't already exist."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {BOOKKEEPING_TABLE} (
                filename       text PRIMARY KEY,
                applied_at_utc timestamptz NOT NULL DEFAULT now(),
                applied_by     text
            );
            """
        )
    conn.commit()


# -- Migration discovery -----------------------------------------------------


def _list_migrations() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        print(f"[migrate] no migrations directory at {MIGRATIONS_DIR}", file=sys.stderr)
        sys.exit(2)
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.is_file())


def _applied_filenames(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT filename FROM {BOOKKEEPING_TABLE}")
        return {row[0] for row in cur.fetchall()}


# -- Apply -------------------------------------------------------------------


def _apply(conn, path: Path, *, dry_run: bool) -> None:
    rel = path.name
    if dry_run:
        print(f"[migrate]   would apply {rel}")
        return
    sql_text = path.read_text(encoding="utf-8")
    actor = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            cur.execute(
                f"INSERT INTO {BOOKKEEPING_TABLE} (filename, applied_by) VALUES (%s, %s)",
                (rel, actor),
            )
        conn.commit()
        print(f"[migrate]   applied  {rel}  (by {actor})")
    except Exception as e:
        conn.rollback()
        print(f"[migrate]   FAILED   {rel}\n            {e}", file=sys.stderr)
        raise


# -- Entry -------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply pending RFPIS migrations.")
    ap.add_argument(
        "--status",
        action="store_true",
        help="List applied vs pending migrations; make no changes.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the apply plan but don't run any SQL.",
    )
    ap.add_argument(
        "--mark-applied",
        metavar="FILENAME",
        help=(
            "Mark a migration filename as applied without running its SQL. "
            "Escape hatch for migrations that were applied by hand or are "
            "non-idempotent on re-run. Example: --mark-applied 008_xxx.sql"
        ),
    )
    args = ap.parse_args()

    conn = _connect()
    try:
        _ensure_bookkeeping(conn)
        all_files = _list_migrations()
        applied = _applied_filenames(conn)
        pending = [p for p in all_files if p.name not in applied]

        if args.mark_applied:
            target = args.mark_applied
            if target in applied:
                print(f"[migrate] {target} is already marked applied.")
                return 0
            if not any(p.name == target for p in all_files):
                print(
                    f"[migrate] {target} does not exist in db/migrations/.",
                    file=sys.stderr,
                )
                return 2
            actor = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {BOOKKEEPING_TABLE} (filename, applied_by) "
                    f"VALUES (%s, %s)",
                    (target, f"{actor} (marked, not executed)"),
                )
            conn.commit()
            print(f"[migrate] marked {target} as applied (SQL was NOT executed).")
            return 0

        if args.status:
            print(f"[migrate] {len(applied)} applied, {len(pending)} pending")
            for p in all_files:
                marker = "*" if p.name in applied else " "
                print(f"  {marker}  {p.name}")
            return 0

        if not pending:
            print("[migrate] nothing to do -- already up to date.")
            return 0

        print(f"[migrate] applying {len(pending)} migration(s):")
        for p in pending:
            _apply(conn, p, dry_run=args.dry_run)
        if args.dry_run:
            print("[migrate] dry-run complete -- no rows written.")
        else:
            print("[migrate] all migrations applied.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
