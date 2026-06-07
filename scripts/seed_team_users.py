"""Seed Business-Development team user accounts + correct submitter emails.

The authoritative Name -> email map lives in TEAM below (entered by the
account owner from the source workbook). Two independent jobs:

  --fix-emails        Overwrite rfp_submissions.submitted_by_email for every
                      migration row so it matches its `submitted_by` name.
                      Pure data correction (no credentials). Fixes the legacy
                      cross-assignments (e.g. Chris Diaz had Sasha's email).

  --create-accounts   Create a users row for each team member who doesn't
                      already have one. SILENT — a direct DB insert sends no
                      email. Accounts are created with a random unusable
                      password + must_change_password=True, so nobody can log
                      in until an admin resets their password (which is when
                      the reset notification is sent). Existing accounts are
                      NEVER touched (no clobber of roles/passwords).

  --dry-run           Preview only.

Examples
    python scripts/seed_team_users.py --fix-emails --dry-run
    python scripts/seed_team_users.py --create-accounts
    python scripts/seed_team_users.py --create-accounts --fix-emails
"""
from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth.authenticator import hash_password  # noqa: E402
from db.supabase_client import get_client, safe_execute  # noqa: E402

# Authoritative roster from the source workbook's Email / Submitted By columns.
# Each person has exactly one email, so name-keying is unambiguous.
TEAM: list[tuple[str, str]] = [
    ("Jane Doe",        "youruser@example.org"),
    ("John Smith",         "ptata@example.org"),
    ("Alex Kim",       "mbudzi@example.org"),
    ("Sam Patel",    "siliassu@example.org"),
    ("Robin Lee",       "prowan@example.org"),
    ("Chris Diaz",         "bcisse.ic@example.org"),
    ("Pat Morgan",    "atraore@example.org"),
    ("Taylor Reed",  "jlambif@example.org"),
    ("Jordan Blake",    "ckuetchetakougang@example.org"),
    ("Casey Fox",       "ayonkeu@example.org"),
    ("Drew Hall",         "ysaidu@example.org"),
]

# New accounts start dormant: live row, but no usable password until an admin
# resets it. Flip to False if you'd rather they be inactive until activation.
NEW_ACCOUNT_ACTIVE = True


def _norm(t) -> str:
    return re.sub(r"\s+", " ", str(t or "").strip().lower())


def fix_emails(sb, dry: bool) -> None:
    name_to_email = {_norm(n): e for n, e in TEAM}
    rows = (
        safe_execute(
            sb.table("rfp_submissions")
            .select("uid,submitted_by,submitted_by_email")
            .eq("source", "migration")
        ).data or []
    )
    changed, unmatched = 0, set()
    for r in rows:
        want = name_to_email.get(_norm(r.get("submitted_by")))
        if not want:
            if r.get("submitted_by"):
                unmatched.add(r.get("submitted_by"))
            continue
        if (r.get("submitted_by_email") or "") == want:
            continue
        print(f"  {r['uid']:18} {r.get('submitted_by'):20} "
              f"{r.get('submitted_by_email') or '(blank)'}  ->  {want}")
        if not dry:
            safe_execute(sb.table("rfp_submissions")
                         .update({"submitted_by_email": want}).eq("uid", r["uid"]))
        changed += 1
    print(f"{'[dry-run] would correct' if dry else 'corrected'} {changed} email(s).")
    if unmatched:
        print(f"submitter names not in TEAM roster: {sorted(unmatched)}")


def create_accounts(sb, dry: bool) -> None:
    existing = {(_norm_email(u.get("email")))
                for u in (safe_execute(sb.table("users").select("email")).data or [])}
    created, skipped = 0, 0
    for name, email in TEAM:
        if _norm_email(email) in existing:
            skipped += 1
            continue
        print(f"  CREATE  {name:20} {email}  (collaborator, must_change_password)")
        if not dry:
            safe_execute(sb.table("users").insert({
                "email": email,
                "name": name,
                "role": "collaborator",
                "password_hash": hash_password(secrets.token_urlsafe(24)),
                "is_active": NEW_ACCOUNT_ACTIVE,
                "must_change_password": True,
            }))
        created += 1
    print(f"{'[dry-run] would create' if dry else 'created'} {created} account(s); "
          f"{skipped} already existed (untouched).")


def _norm_email(e) -> str:
    return str(e or "").strip().lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix-emails", action="store_true")
    ap.add_argument("--create-accounts", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.fix_emails or args.create_accounts):
        ap.error("choose at least one of --fix-emails / --create-accounts")

    sb = get_client()
    if args.create_accounts:
        print("== create accounts ==")
        create_accounts(sb, args.dry_run)
    if args.fix_emails:
        print("== fix submitter emails ==")
        fix_emails(sb, args.dry_run)


if __name__ == "__main__":
    main()
