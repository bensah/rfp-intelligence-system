"""Reconcile `schema_migrations` with the schema that is actually deployed.

THE PROBLEM. Two sources disagree about what has been applied:

    scripts/migrate.py --status   ->  ~49 migrations "pending"
    the live schema               ->  those objects already exist

The ledger stops at 041. Everything after that was applied by hand in the SQL editor and
never recorded. So `python scripts/migrate.py` would try to RE-APPLY ~49 files, and it
would fail on 086 — a table rename cannot be re-applied once the old table is gone —
leaving the run half-done.

Marking them applied blind would be just as bad: a migration that genuinely never ran
would be silently written off, and the object it creates would stay missing forever.

WHAT THIS DOES. For every pending file it reads the SQL, extracts the objects that file
DECLARES (tables, columns, indexes, constraints, policies), and asks the live catalog
whether each one exists:

    VERIFIED     every declared object is present  -> safe to mark applied
    INCOMPLETE   some present, some missing        -> needs a human; never marked
    ABSENT       nothing it declares exists        -> probably really pending
    UNPARSED     no object declaration recognised  -> needs a human; never marked

Only VERIFIED files are marked, and only with --apply. Nothing is ever EXECUTED here —
marking writes a bookkeeping row and nothing else, exactly like `migrate.py
--mark-applied`, which is the same operation one file at a time.

Usage:
    python scripts/reconcile_migration_ledger.py            # report only
    python scripts/reconcile_migration_ledger.py --apply    # mark the VERIFIED ones
    python scripts/reconcile_migration_ledger.py -v         # list every object checked
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.dotenv_compat import load_dotenv                          # noqa: E402

load_dotenv(_ROOT / ".env")

try:
    import psycopg2
except ImportError:
    print("psycopg2 is required: python -m pip install psycopg2-binary", file=sys.stderr)
    raise SystemExit(2)

MIGRATIONS = _ROOT / "db" / "migrations"
LEDGER = "schema_migrations"

# Object declarations we can verify against the catalog. Deliberately conservative:
# anything not matched here leaves the file UNPARSED for a human rather than guessing.
_RE_CREATE_TABLE = re.compile(
    r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?([a-z0-9_]+)", re.I)
_RE_ADD_COLUMN = re.compile(
    r"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?:public\.)?([a-z0-9_]+)"
    r"[\s\S]{0,400}?\badd\s+column\s+(?:if\s+not\s+exists\s+)?([a-z0-9_]+)", re.I)
_RE_RENAME_TABLE = re.compile(
    r"\balter\s+table\s+(?:if\s+exists\s+)?(?:public\.)?([a-z0-9_]+)\s+"
    r"rename\s+to\s+(?:public\.)?([a-z0-9_]+)", re.I)
_RE_RENAME_COL = re.compile(
    r"\balter\s+table\s+(?:if\s+exists\s+)?(?:public\.)?([a-z0-9_]+)\s+"
    r"rename\s+(?:column\s+)?([a-z0-9_]+)\s+to\s+([a-z0-9_]+)", re.I)
_RE_CREATE_INDEX = re.compile(
    r"\bcreate\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?"
    r"([a-z0-9_]+)", re.I)
_RE_CREATE_POLICY = re.compile(
    r"\bcreate\s+policy\s+\"?([a-z0-9_ \-]+)\"?\s+on\s+(?:public\.)?([a-z0-9_]+)", re.I)


# Words a loose regex can mistake for an identifier ("… ADD COLUMN IF NOT EXISTS x"
# mis-parsed as a column called "if").
_SQL_NOISE = {"if", "not", "exists", "only", "public", "table", "column", "concurrently"}


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*[\s\S]*?\*/", " ", sql)
    return "\n".join(re.sub(r"--.*$", "", ln) for ln in sql.splitlines())


def _declared(sql: str) -> list[tuple[str, tuple]]:
    """[(kind, identifier-tuple)] this file claims to create.

    Parsed STATEMENT BY STATEMENT. A regex allowed to span several hundred characters
    happily matched the table of one statement against the column of the next — that is
    where a phantom "scan_decisions_rfpis_baseline.scan_decisions" came from."""
    out: list[tuple[str, tuple]] = []
    for stmt in _strip_sql_comments(sql).split(";"):
        if not stmt.strip():
            continue
        out += [("table", (m,)) for m in _RE_CREATE_TABLE.findall(stmt)]
        out += [("column", (t, c)) for t, c in _RE_ADD_COLUMN.findall(stmt)]
        out += [("table", (new,)) for _old, new in _RE_RENAME_TABLE.findall(stmt)]
        out += [("column", (t, new)) for t, _old, new in _RE_RENAME_COL.findall(stmt)]
        out += [("index", (m,)) for m in _RE_CREATE_INDEX.findall(stmt)]
        out += [("policy", (p.strip(), t)) for p, t in _RE_CREATE_POLICY.findall(stmt)]
    seen, uniq = set(), []
    for kind, ident in out:
        low = tuple(x.lower() for x in ident)
        if any(x in _SQL_NOISE or not x for x in low):
            continue
        if (kind, low) not in seen:
            seen.add((kind, low))
            uniq.append((kind, ident))
    return uniq


class Catalog:
    """One round-trip per object KIND, then answer from memory."""

    def __init__(self, cur):
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'")
        self.tables = {r[0].lower() for r in cur.fetchall()}
        cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema='public'")
        self.columns = {(r[0].lower(), r[1].lower()) for r in cur.fetchall()}
        cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
        self.indexes = {r[0].lower() for r in cur.fetchall()}
        try:
            cur.execute("SELECT policyname, tablename FROM pg_policies "
                        "WHERE schemaname='public'")
            self.policies = {(r[0].lower(), r[1].lower()) for r in cur.fetchall()}
        except Exception:
            self.policies = set()

    # Migrations 054-060 renamed the data model to source-prefixed columns
    # (summary_description -> donor_summary_description, compliance_flags ->
    # call_compliance_flags, …). A migration that ran BEFORE that rename declares the
    # OLD name, so a literal lookup reports it missing and the file looks unapplied when
    # it plainly is. Try the prefixed forms before concluding anything.
    _RENAME_PREFIXES = ("donor_", "call_", "org_")

    def has(self, kind: str, ident: tuple) -> tuple[bool, str]:
        """(found, note) — note explains a non-literal match."""
        low = tuple(x.lower() for x in ident)
        if kind == "table":
            return (low[0] in self.tables, "")
        if kind == "column":
            if low in self.columns:
                return (True, "")
            for p in self._RENAME_PREFIXES:
                if (low[0], p + low[1]) in self.columns:
                    return (True, f"renamed to {low[0]}.{p}{low[1]}")
            return (False, "")
        if kind == "index":
            return (low[0] in self.indexes, "")
        if kind == "policy":
            return (low in self.policies, "")
        return (False, "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="mark the VERIFIED migrations as applied (writes bookkeeping "
                         "rows only — never executes a migration)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="list every object checked")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        print("SUPABASE_DB_URL is not set (.env) — see scripts/migrate.py's docstring.",
              file=sys.stderr)
        return 2

    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {LEDGER} ("
                        f"filename text PRIMARY KEY,"
                        f"applied_at_utc timestamptz NOT NULL DEFAULT now(),"
                        f"applied_by text)")
            conn.commit()
            cur.execute(f"SELECT filename FROM {LEDGER}")
            recorded = {r[0] for r in cur.fetchall()}
            cat = Catalog(cur)

        files = sorted(p for p in MIGRATIONS.glob("*.sql")
                       if p.is_file() and "sync-conflict" not in p.name)
        pending = [p for p in files if p.name not in recorded]

        print(f"migrations on disk : {len(files)}")
        print(f"recorded in ledger : {len(recorded)}  (highest: "
              f"{max(recorded) if recorded else '—'})")
        print(f"pending per ledger : {len(pending)}\n")

        buckets: dict[str, list] = {"VERIFIED": [], "INCOMPLETE": [],
                                    "ABSENT": [], "UNPARSED": []}
        for p in pending:
            decl = _declared(p.read_text(encoding="utf-8"))
            if not decl:
                buckets["UNPARSED"].append((p, [], [], []))
                continue
            present, missing, renamed = [], [], []
            for d in decl:
                ok, note = cat.has(*d)
                (present if ok else missing).append(d)
                if ok and note:
                    renamed.append((d, note))
            state = ("VERIFIED" if not missing else
                     "ABSENT" if not present else "INCOMPLETE")
            buckets[state].append((p, present, missing, renamed))

        for state in ("VERIFIED", "INCOMPLETE", "ABSENT", "UNPARSED"):
            rows = buckets[state]
            print(f"── {state}  ({len(rows)})")
            for p, present, missing, renamed in rows:
                print(f"     {p.name}")
                if args.verbose and present:
                    print(f"        present: {', '.join('.'.join(i) for _k, i in present)}")
                if renamed:
                    print(f"        matched after the 054-060 rename: "
                          f"{', '.join(n for _d, n in renamed[:4])}"
                          + (f" (+{len(renamed) - 4} more)" if len(renamed) > 4 else ""))
                if missing:
                    print(f"        MISSING: {', '.join('.'.join(i) for _k, i in missing)}")
            print()

        safe = [p for p, _, _, _ in buckets["VERIFIED"]]
        if not args.apply:
            print(f"DRY RUN — nothing written. {len(safe)} file(s) would be marked "
                  f"applied.\nRe-run with --apply. INCOMPLETE / ABSENT / UNPARSED are "
                  f"never marked; review those by hand.")
            return 0

        actor = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
        with conn.cursor() as cur:
            for p in safe:
                cur.execute(
                    f"INSERT INTO {LEDGER} (filename, applied_by) VALUES (%s, %s) "
                    f"ON CONFLICT (filename) DO NOTHING",
                    (p.name, f"{actor} (reconciled: verified against live schema)"))
        conn.commit()
        print(f"marked {len(safe)} migration(s) as applied. Nothing was executed.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
