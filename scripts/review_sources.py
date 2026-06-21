"""Review + confirm the source registry (aggregator vs primary host log).

The scanner logs every host it meets to `source_registry` (migration 034) with a
detector guess and status='pending'. This is the human-in-the-loop tool to review
new hosts and CONFIRM their classification — confirmed rows become authoritative
(core.aggregators defers to them), so known aggregators get rejected + resolved
and known primary sources are trusted, every future scan.

  python scripts/review_sources.py                       # list pending hosts
  python scripts/review_sources.py --all                 # list everything
  python scripts/review_sources.py --class aggregator    # filter by class
  python scripts/review_sources.py --confirm grantbite.com aggregator
  python scripts/review_sources.py --confirm edctp.org primary
        (classification ∈ aggregator | primary | blog | listing | unknown)

Read-only unless --confirm is passed. Bernard runs this himself.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core.source_registry import VALID_CLASS                     # noqa: E402
from db.supabase_client import get_client, safe_execute          # noqa: E402

_TABLE = "source_registry"


def _list(status: str | None, klass: str | None) -> int:
    q = get_client().table(_TABLE).select(
        "host,classification,status,detected_as,hits,sample_title,last_seen")
    if status:
        q = q.eq("status", status)
    if klass:
        q = q.eq("classification", klass)
    rows = safe_execute(q.order("hits", desc=True).limit(1000)).data or []
    if not rows:
        print("(no matching hosts)")
        return 0
    print(f"{'HOST':<42} {'CLASS':<11} {'STATUS':<10} {'HITS':>5}  SAMPLE")
    print("-" * 100)
    for r in rows:
        print(f"{(r.get('host') or '')[:41]:<42} "
              f"{(r.get('classification') or '')[:10]:<11} "
              f"{(r.get('status') or '')[:9]:<10} "
              f"{int(r.get('hits') or 0):>5}  "
              f"{(r.get('sample_title') or '')[:42]}")
    print(f"\n{len(rows)} hosts. Confirm one with:\n"
          f"  python scripts/review_sources.py --confirm <host> <classification>")
    return 0


def _confirm(host: str, klass: str) -> int:
    klass = klass.lower().strip()
    if klass not in VALID_CLASS:
        print(f"classification must be one of {VALID_CLASS}")
        return 1
    sb = get_client()
    existing = safe_execute(sb.table(_TABLE).select("host").eq("host", host)).data or []
    payload = {
        "host": host, "classification": klass, "status": "confirmed",
        "verified_by": "review_sources", "verified_at":
            datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        sb.table(_TABLE).update(payload).eq("host", host).execute()
    else:
        sb.table(_TABLE).insert({**payload, "detected_as": klass}).execute()
    print(f"confirmed {host} = {klass}. Future scans treat it as authoritative.")
    return 0


def main(argv: list[str]) -> int:
    if "--confirm" in argv:
        i = argv.index("--confirm")
        if i + 2 >= len(argv):
            print("usage: --confirm <host> <classification>")
            return 1
        return _confirm(argv[i + 1], argv[i + 2])
    klass = None
    if "--class" in argv:
        klass = argv[argv.index("--class") + 1]
    status = None if "--all" in argv else "pending"
    return _list(status, klass)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
