"""Delete one or more RFP submissions by UID.

Useful for cleaning up test records.

    python scripts/delete_rfp.py BN-260601-1352
    python scripts/delete_rfp.py BN-260601-1352 NS-260601-1500
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.supabase_client import get_client  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("uids", nargs="+", help="UID(s) to delete, e.g. BN-260601-1352")
    args = ap.parse_args()

    sb = get_client()
    for uid in args.uids:
        res = sb.table("rfp_submissions").delete().eq("uid", uid).execute()
        deleted = len(res.data or [])
        print(f"  {uid}: deleted {deleted} row(s)")


if __name__ == "__main__":
    main()
