"""Backfill: canonicalize call_geographic_scope so ISO-inverted country names resolve.

Why: sources emit inverted official names ("Congo, The Democratic Republic of the",
"Korea, Republic of", "Tanzania, United Republic of"). Before the geographies.canonical_geo
de-inversion fix these were stored verbatim, so the geo gate read them as unrecognised
free-text → 'silent' geography → PERMISSIVE pass, leaking off-scope calls into a
country-defined tenant's pipeline (BUG 2). This rewrites every stored scope term through
the (now fixed) canonicaliser so both the global store and the per-tenant Screened rows
carry comparable, gate-recognisable names.

Touches ONLY the call_geographic_scope array (idempotent — re-running is a no-op once
clean). It does NOT delete rows. After running this, run:
    python scripts/prune_ineligible_screened.py --apply
to drop Screened rows whose corrected geography now fails the eligibility gate (e.g. the
legacy DRC / Samoa rows in a Cameroon tenant).

Usage:
    python scripts/backfill_geo_canon.py            # dry-run (report only)
    python scripts/backfill_geo_canon.py --apply    # write the canonicalised scope
"""
from __future__ import annotations

import argparse
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

from core import geographies as geo
from db.supabase_client import service_client, safe_execute

_TABLES = (
    ("extracted_solicitations", "uid"),
    ("rfp_submissions", "uid"),
)


def _canon_scope(scope) -> list[str]:
    """Repair double-encoded scope + canonicalise each term; drop blanks; de-dup."""
    out: list[str] = []
    for term in geo.flatten_scope_terms(scope):
        c = geo.canonical_geo(str(term).strip())
        if c and c not in out:
            out.append(c)
    return out


def _backfill_table(sb, table: str, key: str, *, apply: bool) -> tuple[int, int]:
    rows = safe_execute(
        sb.table(table).select(f"{key}, call_geographic_scope")
    ).data or []
    changed = 0
    for r in rows:
        before = r.get("call_geographic_scope")
        after = _canon_scope(before)
        # Compare as ordered lists — a re-order or a rename both count as a change.
        if (before or []) == after:
            continue
        changed += 1
        if changed <= 25:
            print(f"  {table} {r.get(key)}: {before!r} -> {after!r}")
        if apply:
            try:
                sb.table(table).update(
                    {"call_geographic_scope": after}).eq(key, r.get(key)).execute()
            except Exception as exc:
                print(f"    ! update failed for {r.get(key)}: {exc}")
    if changed > 25:
        print(f"  … and {changed - 25} more")
    return len(rows), changed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    sb = service_client()          # RLS-bypass: cross-tenant maintenance backfill
    total_changed = 0
    for table, key in _TABLES:
        print(f"\n== {table} ==")
        n, changed = _backfill_table(sb, table, key, apply=args.apply)
        total_changed += changed
        print(f"  scanned {n}, {'updated' if args.apply else 'would update'} {changed}")
    verb = "Updated" if args.apply else "Would update"
    print(f"\n{verb} {total_changed} row(s).")
    if not args.apply and total_changed:
        print("Re-run with --apply to write. Then: "
              "python scripts/prune_ineligible_screened.py --apply")


if __name__ == "__main__":
    main()
