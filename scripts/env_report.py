"""Print this machine's environment snapshot — the SAME report the deployed app shows
under Settings → Accounts → Deployment (and `?diag=1`).

The point is comparison. When the published app behaves differently from a local run, the
code is rarely the difference: the environment is. Run this locally, open the panel on the
deployed app, and read the two side by side — project ref, key kind, whether the
multi-tenant master switch is readable, which commit is served, which tenant the login
resolves to. The first line that differs is the explanation.

No secret values are printed. Secrets appear as present/absent, where they were read
from, their length, and a short sha256 fingerprint — identical fingerprints mean identical
values, which is all a comparison needs.

Usage:
    python scripts/env_report.py
    python scripts/env_report.py --email someone@example.org   # include their landing tenant
"""
from __future__ import annotations

import argparse
import json
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

from core.env_diag import snapshot, verdicts   # noqa: E402


def _user_by_email(email: str) -> dict:
    """The app_user-shaped dict the landing logic takes, so the report can answer 'which
    tenant would THIS login land in here?'. Empty dict if the lookup fails — the rest of
    the report still stands."""
    try:
        from db.supabase_client import service_client
        rows = (service_client().table("users").select("id,email,role,last_tenant_id")
                .eq("email", email).limit(1).execute().data or [])
        return rows[0] if rows else {}
    except Exception as exc:
        print(f"(could not look up {email}: {type(exc).__name__}: {exc})", file=sys.stderr)
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Environment snapshot for this runtime.")
    ap.add_argument("--email", help="report the landing tenant for this account too")
    args = ap.parse_args()

    user = _user_by_email(args.email) if args.email else None
    snap = snapshot(user)
    print(json.dumps(snap, indent=2, default=str))
    print("\n--- verdicts ---")
    for level, msg in verdicts(snap):
        print(f"[{level.upper()}] {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
