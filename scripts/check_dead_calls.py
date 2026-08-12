"""Find catalogue calls whose page no longer exists, and close them.

    python -m scripts.check_dead_calls              # dry run
    python -m scripts.check_dead_calls --apply

WHY. 34 rows currently sit in the catalogue as OPEN — their deadline has not passed and their
status says Open — while their URL returns 404. A notice can be withdrawn long before its
advertised deadline (a tender board pulls it, a portal rotates the id), and no gate we have
catches that: the deadline rule cannot fire on a date still in the future, and the stale-posting
rule needs a posting date. So the app shows a live opportunity that cannot be applied to, in the
Live Opportunity Feed and on the Featured card, which is exactly where somebody goes looking for
something to bid on.

WHAT COUNTS AS DEAD, deliberately narrowly:
  * 404 / 410 — the page is gone. That is the funder's own statement.
Nothing else. A 403 is usually a bot block, a 500 is somebody's bad afternoon, and a timeout is
the network — none of them mean the call ended, and closing a live call is worse than leaving a
dead one visible. Two passes are required before a row is closed, so a one-off blip cannot do it.

The row is CLOSED, never deleted: funding_status = 'Closed'. The extraction stays readable, the
deadline gate then does the rest, and a human can see what happened.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Only these mean "the funder took it down".
DEAD_CODES = (404, 410)
_PASSES = 2                 # a single blip must not close a call
_PAUSE = 0.4                # be polite to the host


def _load_env() -> None:
    path = os.path.join(_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _status(url: str) -> Any:
    import requests
    from core.scraper import USER_AGENT, HTTP_TIMEOUT
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT},
                         timeout=HTTP_TIMEOUT, allow_redirects=True)
        return r.status_code
    except Exception as exc:
        return f"ERR:{type(exc).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write funding_status = Closed")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    _load_env()
    from datetime import date
    from db.supabase_client import service_client

    sb = service_client()
    rows = (sb.table("extracted_solicitations")
            .select("uid,opportunity_url,opportunity_name,deadline,funding_status")
            .limit(5000).execute().data or [])

    today = date.today()

    def live(r):
        if str(r.get("funding_status") or "").strip().lower() == "closed":
            return False
        d = str(r.get("deadline") or "")[:10]
        if not d:
            return True
        try:
            return date.fromisoformat(d) >= today
        except ValueError:
            return True

    candidates = [r for r in rows if live(r) and r.get("opportunity_url")][:args.limit]
    print(f"catalogue      : {len(rows)} rows")
    print(f"live to check  : {len(candidates)}")
    print(f"mode           : {'APPLY (writes funding_status)' if args.apply else 'DRY RUN'}\n")

    dead, other = [], {}
    for i, r in enumerate(candidates, 1):
        codes = []
        for _ in range(_PASSES):
            codes.append(_status(r["opportunity_url"]))
            if codes[-1] not in DEAD_CODES:
                break              # not dead — no need for a second look
            time.sleep(_PAUSE)
        if all(c in DEAD_CODES for c in codes) and len(codes) == _PASSES:
            dead.append((r, codes[-1]))
            print(f"  {i:4d}. DEAD {codes[-1]}  {r['uid'][:24]}  "
                  f"{str(r.get('opportunity_name'))[:52]}")
        else:
            other[str(codes[-1])] = other.get(str(codes[-1]), 0) + 1

    print(f"\ndead (confirmed twice): {len(dead)}")
    print(f"other responses       : {other}")
    if dead and args.apply:
        for r, _c in dead:
            sb.table("extracted_solicitations").update(
                {"funding_status": "Closed"}).eq("uid", r["uid"]).execute()
        print(f"\nclosed {len(dead)} row(s). The extraction stays readable; the deadline gate "
              "does the rest.")
    elif dead:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
