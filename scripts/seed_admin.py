"""Seed the first admin user.

Run once after the schema is applied so you can log into the deployed app.

    python scripts/seed_admin.py --email you@chai.org --name "Your Name" --password "..."

You can re-run safely; it upserts on email.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth.authenticator import hash_password  # noqa: E402
from db.supabase_client import get_client  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--role", default="admin", choices=["admin", "reviewer", "collaborator"])
    args = ap.parse_args()

    sb = get_client()
    row = {
        "email": args.email,
        "name": args.name,
        "role": args.role,
        "password_hash": hash_password(args.password),
        "is_active": True,
    }
    sb.table("users").upsert(row, on_conflict="email").execute()
    print(f"Seeded {args.role}: {args.email}")


if __name__ == "__main__":
    main()
