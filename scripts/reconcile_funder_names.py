"""Reconcile funder spellings already stored in rfp_submissions.

One donor written two ways is two donors to anything that groups on the literal string.
The report's funders chart showed exactly that — `BMGF - Gates Foundation` (5 calls) drawn
beside `BMGF – Gates Foundation` (4 calls, EN DASH), a funder with nine calls appearing as
two smaller ones. `core.funder_names` now canonicalises at every write and the chart groups
by identity, so this script is for the rows written before both: it rewrites each stored
name to the canonical spelling of its group.

Conservative by design. It repairs the dash family and whitespace and nothing else — it
never merges names that differ in their actual words, never edits case, and never invents
a spelling: the target is the variant already most used for that donor. Rows already
canonical are skipped, so re-running does nothing.

DRY RUN BY DEFAULT. Nothing is written without --apply.

Scope. By default it touches only donors that are actually SPLIT — two spellings, one
funder — because that is the reported problem and the smallest correct fix. `--all` also
canonicalises donors written consistently in a non-canonical way ("UN — UNICEF", em dash,
on every row). That is worth doing once the write path is canonical, because otherwise the
next scan stores "UN - UNICEF" and creates the very split this script exists to remove;
but it rewrites names nobody complained about, so it is opt-in.

Usage:
    python scripts/reconcile_funder_names.py                     # report what would change
    python scripts/reconcile_funder_names.py --tenant <slug|id>  # one tenant only
    python scripts/reconcile_funder_names.py --apply             # write the duplicate fix
    python scripts/reconcile_funder_names.py --all --apply       # also unify the rest
"""
from __future__ import annotations

import argparse
import sys
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

from core.funder_names import canonical_funder, funder_key, group_by_funder  # noqa: E402
from db.supabase_client import service_client  # noqa: E402

TABLE = "rfp_submissions"
COLUMN = "funding_agency"


def _resolve_tenant(key: str | None) -> str | None:
    if not key:
        return None
    from auth.tenant_context import resolve_tenant_by_key
    row = resolve_tenant_by_key(key)
    if not row:
        raise SystemExit(f"No tenant matches {key!r}.")
    return str(row["id"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--tenant", help="restrict to one tenant (slug or id)")
    ap.add_argument("--all", action="store_true", dest="all_names",
                    help="also canonicalise donors that are consistently non-canonical "
                         "(not currently split), so future writes cannot split them")
    args = ap.parse_args()

    tenant_id = _resolve_tenant(args.tenant)
    sb = service_client()          # identity-level maintenance across tenants
    query = sb.table(TABLE).select(f"uid, {COLUMN}, tenant_id")
    if tenant_id:
        query = query.eq("tenant_id", tenant_id)
    rows = query.limit(10000).execute().data or []
    print(f"Read {len(rows)} rows from {TABLE}"
          + (f" (tenant {args.tenant})" if tenant_id else " (all tenants)"))

    groups = group_by_funder(r.get(COLUMN) for r in rows)
    split = {k: g for k, g in groups.items() if len(g["variants"]) > 1}
    print(f"{len(groups)} distinct funders, {len(split)} split across spellings\n")

    # The target for a group is its dominant spelling; for everything else it is simply
    # the canonical form of what is already there (repairs a lone stray dash too).
    planned: list[tuple[str, str, str]] = []       # (uid, before, after)
    deferred = 0
    for r in rows:
        before = (r.get(COLUMN) or "").strip()
        if not before:
            continue
        key = funder_key(before)
        group = groups.get(key)
        after = (group or {}).get("label") or canonical_funder(before)
        if not after or after == before:
            continue
        if key in split or args.all_names:
            planned.append((r["uid"], before, after))
        else:
            deferred += 1                # consistently non-canonical: only with --all

    for key, group in sorted(split.items(), key=lambda kv: -kv[1]["count"]):
        print(f"  {group['label']}  ({group['count']} rows)")
        for raw, n in sorted(group["variants"].items(), key=lambda kv: -kv[1]):
            mark = "keep" if raw == group["label"] else "->  " + group["label"]
            print(f"      {n:>3}x  {raw!r}   {mark}")
    if not split:
        print("  (no donor is split across spellings)")

    print(f"\n{len(planned)} rows would change"
          + (" (split donors only)" if not args.all_names else " (all names)") + ".")
    if deferred:
        print(f"{deferred} further rows are consistently non-canonical (one spelling, but "
              f"not the canonical one — e.g. an em dash used everywhere for that donor). "
              f"They split nothing today, but the write path is now canonical, so the next "
              f"row for one of those donors WILL split. Re-run with --all to unify them.")
    if not planned:
        return 0
    if not args.apply:
        print("DRY RUN — nothing written. Re-run with --apply to write.")
        return 0

    changed = 0
    for uid, before, after in planned:
        try:
            sb.table(TABLE).update({COLUMN: after}).eq("uid", uid).execute()
            changed += 1
        except Exception as exc:
            print(f"  FAILED {uid}: {type(exc).__name__}: {exc}")
    print(f"Updated {changed} of {len(planned)} rows.")
    return 0 if changed == len(planned) else 1


if __name__ == "__main__":
    raise SystemExit(main())
