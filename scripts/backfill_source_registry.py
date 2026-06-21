"""Backfill the source registry from URLs we've ALREADY seen.

The registry (migration 034) normally fills during a scan. This seeds it
immediately from every link already in `scan_decisions` (rejects + decisions) and
`rfp_submissions`, classified by core.aggregators — so there's a real list of
aggregator vs primary hosts to review without waiting for the next scan.

  python scripts/backfill_source_registry.py            # preview (counts only)
  python scripts/backfill_source_registry.py --commit   # write to source_registry

Idempotent: record_encounters upserts by host (bumps hits; never overwrites a
human-confirmed classification). Accepted records (rfp_submissions) count as
'primary' evidence; rejected scan_decisions carry the detector's kind.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import aggregators, source_registry                      # noqa: E402
from db.supabase_client import get_client, safe_execute            # noqa: E402


def _fetch_all(table: str, cols: str, *, eq: dict | None = None) -> list[dict]:
    sb = get_client()
    out, start, page = [], 0, 1000
    while True:
        q = sb.table(table).select(cols)
        for k, v in (eq or {}).items():
            q = q.eq(k, v)
        rows = safe_execute(q.range(start, start + page - 1)).data or []
        out.extend(rows)
        if len(rows) < page:
            break
        start += page
    return out


def main(commit: bool) -> int:
    encounters: list[dict] = []

    # rfp_submissions — accepted opportunities → strong PRIMARY evidence.
    for r in _fetch_all("rfp_submissions",
                        "opportunity_link,opportunity_title,is_duplicate"):
        link = r.get("opportunity_link")
        if link:
            encounters.append({"url": link, "title": r.get("opportunity_title"),
                               "detected": "primary", "accepted": True})

    # scan_decisions — rejects/decisions; classify each link with the detector.
    for r in _fetch_all("scan_decisions",
                        "opportunity_link,opportunity_title,event_type"):
        link = r.get("opportunity_link")
        if not link:
            continue
        accepted = r.get("event_type") in ("human_decision",)
        try:
            kind = aggregators.classify(link, r.get("opportunity_title"))[0]
        except Exception:
            kind = "unknown"
        encounters.append({"url": link, "title": r.get("opportunity_title"),
                           "detected": kind, "accepted": accepted})

    # Preview: aggregate per host the way record_encounters will.
    hosts: dict[str, str] = {}
    for e in encounters:
        h = source_registry.normalize_host(e.get("url"))
        if not h:
            continue
        det = e["detected"]
        cur = hosts.get(h, "unknown")
        if det in ("aggregator", "blog", "listing"):
            hosts[h] = det
        elif cur not in ("aggregator", "blog", "listing"):
            hosts[h] = "primary" if (e.get("accepted") or cur == "primary") else \
                (det if cur == "unknown" else cur)
    print(f"URLs scanned: {len(encounters)}   distinct hosts: {len(hosts)}")
    print("by classification:", dict(Counter(hosts.values())))
    print("\nsample non-primary hosts:")
    for h, k in sorted(hosts.items(), key=lambda x: x[1]):
        if k != "primary":
            print(f"  {k:<11} {h}")

    if not commit:
        print("\nDRY RUN — re-run with --commit to write to source_registry.")
        return 0
    n = source_registry.record_encounters(encounters)
    print(f"\nWrote {n} host rows to source_registry. "
          f"Review: python scripts/review_sources.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--commit" in sys.argv))
