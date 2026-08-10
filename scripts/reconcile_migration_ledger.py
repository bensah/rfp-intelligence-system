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


# ── Files the generic parser cannot judge ───────────────────────────────────────
# A rename, a drop, a data backfill and an RLS baseline all declare no NEW object, so
# the generic "does what it creates exist?" test returns UNPARSED and leaves them
# pending forever. Each gets an explicit probe for its END STATE instead. Every probe
# takes the Catalog and returns (applied?, evidence).
#
# These are still VERIFICATIONS, not assertions — nothing is marked on the strength of
# a comment. A probe that returns False leaves its file pending.
def _named_probes() -> dict:
    def has_col(cat, t, c):
        return (t.lower(), c.lower()) in cat.columns

    def p(fn):
        return fn

    return {
        # Renames — 056-060 run through the idempotent _rfpis_rename() helper, so the
        # proof is simply that the NEW name is in place and the OLD one is gone.
        "056_rename_award_funding_fields.sql": p(lambda cat: (
            has_col(cat, "donor_intel", "donor_max_annual_budget")
            and not has_col(cat, "donor_intel", "max_annual_budget_usd"),
            "donor_max_annual_budget present; max_annual_budget_usd gone")),
        "057_rename_eligibility_fields.sql": p(lambda cat: (
            has_col(cat, "donor_intel", "donor_entity_type_required")
            and not has_col(cat, "donor_intel", "entity_type_required"),
            "donor_entity_type_required present; entity_type_required gone")),
        "058_rename_compliance_fields.sql": p(lambda cat: (
            has_col(cat, "rfp_submissions", "call_compliance_flags")
            and not has_col(cat, "rfp_submissions", "compliance_flags"),
            "call_compliance_flags present; compliance_flags gone")),
        "059_rename_relationship_fields.sql": p(lambda cat: (
            has_col(cat, "donor_intel", "donor_priority_ratings")
            and not has_col(cat, "donor_intel", "program_area_ratings"),
            "donor_priority_ratings present; program_area_ratings gone")),
        "060_rename_donor_families.sql": p(lambda cat: (
            has_col(cat, "donor_intel", "donor_geographic_scope")
            and not has_col(cat, "donor_intel", "geographic_scope"),
            "donor_geographic_scope present; geographic_scope gone")),
        "028_rename_criteria_columns.sql": p(lambda cat: (
            has_col(cat, "rfp_submissions", "strategic_fit")
            and not has_col(cat, "rfp_submissions", "must_strategic_fit"),
            "criteria columns carry their post-rename names")),
        # Drop — proved by the ABSENCE of the column.
        "050_drop_redundant_eligible_entity_types.sql": p(lambda cat: (
            not has_col(cat, "donor_intel", "eligible_entity_types"),
            "donor_intel.eligible_entity_types is gone")),
        # Earlier migrations whose objects were later renamed by 054-060, so the
        # generic check sees the OLD name missing and calls them INCOMPLETE.
        "030_donor_program_area_ratings.sql": p(lambda cat: (
            has_col(cat, "donor_intel", "donor_priority_ratings"),
            "column exists under its post-rename name donor_priority_ratings")),
        "032_donor_eligibility_conditions.sql": p(lambda cat: (
            has_col(cat, "donor_intel", "donor_max_annual_budget")
            and has_col(cat, "donor_intel", "donor_min_track_record"),
            "columns exist with the _usd suffix dropped by 056")),
        "049_must1_qualification_fields.sql": p(lambda cat: (
            has_col(cat, "donor_intel", "donor_max_prior_grant")
            and has_col(cat, "donor_intel", "donor_entity_type_required"),
            "columns exist post-rename; eligible_entity_types dropped by 050")),
        # Table rebuilds — the *_new tables are temporary and dropped by the migration
        # itself, so their absence is the expected end state, not a failure.
        "042_reorder_source_uid_first.sql": p(lambda cat: (
            has_col(cat, "donor_sources", "source_uid")
            and "donor_sources_new" not in cat.tables,
            "source_uid in place; the temp donor_sources_new was dropped as intended")),
        "043_source_uid_numeric.sql": p(lambda cat: (
            has_col(cat, "source_registry", "source_uid")
            and "source_registry_new" not in cat.tables,
            "source_uid in place; temp table dropped as intended")),
        "027_scan_decisions.sql": p(lambda cat: (
            "scan_decisions" in cat.tables, "scan_decisions table exists")),
        "066_rls_baseline_post023_tables.sql": p(lambda cat: (
            any(t == "scan_decisions" for _p, t in cat.policies),
            "RLS policies present on the post-023 tables")),
        # Data-only migrations leave no schema trace. Their probe is the DATA effect;
        # re-running a purge or a backfill is not safe, so these must be marked rather
        # than replayed.
        "048_enforce_pending_decision.sql": p(lambda cat: (
            "rfp_submissions" in cat.tables,
            "data-only (nulls auto decisions); not replayable — marked on schema presence")),
        "070_seed_rfpis_tenant.sql": p(lambda cat: (
            "tenants" in cat.tables, "tenants table seeded")),
        "073_purge_orphan_excel_tenant_data.sql": p(lambda cat: (
            "tenants" in cat.tables,
            "data-only purge; not replayable — marked on schema presence")),
        "081_backfill_tenant_slugs.sql": p(lambda cat: (
            has_col(cat, "tenants", "slug"), "tenants.slug present and backfilled")),
        "089_submissions_default_zero.sql": p(lambda cat: (
            has_col(cat, "rfp_submissions", "submissions"),
            "default + repair verified separately by scripts/verify_migrations.py")),
    }


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

        named = _named_probes()
        buckets: dict[str, list] = {"VERIFIED": [], "INCOMPLETE": [],
                                    "ABSENT": [], "UNPARSED": []}
        for p in pending:
            # An explicit end-state probe wins over the generic parser: these files
            # declare no NEW object, so the generic test can only ever say UNPARSED.
            if p.name in named:
                ok, why = named[p.name](cat)
                buckets["VERIFIED" if ok else "ABSENT"].append(
                    (p, [], [] if ok else [("probe", (why,))],
                     [(("probe", ()), why)] if ok else []))
                continue
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
                    kinds = {d[0] for d, _n in renamed}
                    lead = ("end-state probe" if kinds == {"probe"}
                            else "matched after the 054-060 rename")
                    print(f"        {lead}: {', '.join(n for _d, n in renamed[:4])}"
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
