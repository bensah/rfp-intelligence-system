"""Backfill the two-axis split (run migration 039 first).

Machine does the work so no human redo:
  * scan_decisions (auto-rejected) + rfp_submissions (inserted): auto-DETECT
    solicitation_type + instrument_type from title/URL (+ funding instrument for
    inserted). Where a HUMAN type pick exists (type_label), it OVERRIDES the
    auto guess on the correct axis — so prior manual work is preserved, not lost.
  * source_registry: split curated opportunity_types[] into solicitation_types[]
    + instrument_types[].

Bulk upserts (fast). DRY-RUN by default; --commit to write. Re-runnable.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import source_registry as sr, type_detect as td   # noqa: E402
from core.type_detect import SOLICITATION_TYPES               # noqa: E402
from db.supabase_client import get_client                     # noqa: E402

# legacy type value (old single vocab / human type_label) -> new axis
_OLD_SOL = set(SOLICITATION_TYPES)                            # RFP/CFP/CFA/NOFO/…
_OLD_INST = {"Grant": "Grant", "Cooperative Agreement": "Cooperative Agreement",
             "Award": "Prize/Award", "Prize/Award": "Prize/Award",
             "Seed Fund": "Seed fund", "Seed fund": "Seed fund",
             "Contract award": "Contract", "Contract": "Contract",
             "Fellowship": "Fellowship", "Scholarship": "Scholarship",
             "Loan": "Loan", "Equity/Investment": "Equity/Investment"}


def _human_axes(label: str | None) -> tuple[str | None, str | None]:
    """Split a single human type pick into (solicitation, instrument)."""
    if not label:
        return None, None
    return (label if label in _OLD_SOL else None,
            _OLD_INST.get(label))


def _backfill_table(sb, table: str, key: str, extra_cols: str,
                    event: str | None = None) -> list:
    human = {}  # link -> human type_label
    try:
        for r in (sb.table("scan_decisions")
                  .select("opportunity_link,label,created_at")
                  .eq("event_type", "type_label")
                  .order("created_at", desc=True).limit(5000).execute().data or []):
            lk = (r.get("opportunity_link") or "").strip()
            if lk and lk not in human and (r.get("label") or "").strip():
                human[lk] = r["label"].strip()
    except Exception:
        pass
    q = (sb.table(table)
         .select(f"{key},opportunity_title,opportunity_link{extra_cols}"))
    if event:
        q = q.eq("event_type", event)
    rows = q.limit(5000).execute().data or []
    ups = []
    for row in rows:
        h_sol, h_inst = _human_axes(human.get((row.get("opportunity_link") or "").strip()))
        sol = h_sol or td.detect_solicitation(row)
        inst = h_inst or td.detect_instrument(row)
        if not sol and not inst:
            continue
        ups.append({key: row[key], "solicitation_type": sol,
                    "instrument_type": inst})
    print(f"  {table}: {len(rows)} scanned -> {len(ups)} to set")
    return ups


def main(commit: bool) -> int:
    sb = get_client()

    # 1. Records: auto-detect (+ human override) on both axes.
    rej = _backfill_table(sb, "scan_decisions", "id", "", event="system_reject")
    ins = _backfill_table(sb, "rfp_submissions", "uid", ",funding_window")
    # 2. Registry: split curated opportunity_types.
    reg_ups = []
    for r in sr.list_rows():
        old = r.get("opportunity_types") or []
        if not old:
            continue
        sols = sorted({t for t in old if t in _OLD_SOL})
        insts = sorted({_OLD_INST[t] for t in old if t in _OLD_INST})
        if sols or insts:
            reg_ups.append({"host": r["host"],
                            "solicitation_types": sols or None,
                            "instrument_types": insts or None})
    print(f"  source_registry: {len(reg_ups)} to split")

    if not commit:
        print("\nDRY RUN — re-run with --commit.")
        return 0
    # UPDATE per row (not upsert — these tables have NOT-NULL cols an upsert-insert
    # path would trip; we only ever touch the two type columns of existing rows).
    for tbl, key, ups in [("scan_decisions", "id", rej),
                          ("rfp_submissions", "uid", ins),
                          ("source_registry", "host", reg_ups)]:
        done = 0
        for u in ups:
            fields = {k: v for k, v in u.items() if k != key}
            try:
                sb.table(tbl).update(fields).eq(key, u[key]).execute()
                done += 1
            except Exception as exc:
                print(f"    {tbl} {u[key]} failed: {str(exc)[:60]}")
        print(f"  {tbl}: updated {done}/{len(ups)}")
    print("COMMITTED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
