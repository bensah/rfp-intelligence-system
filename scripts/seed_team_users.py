"""Seed Business-Development team user accounts + correct submitter emails.

The authoritative Name -> email map lives in TEAM below (entered by the
account owner from the source workbook). Two independent jobs:

  --fix-emails        Overwrite rfp_submissions.submitted_by_email for every
                      migration row so it matches its `submitted_by` name.
                      Pure data correction (no credentials). Fixes legacy
                      cross-assignments (a row's email not matching its name).

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

# Roster (Name -> email). PLACEHOLDER example values — this is a public template.
# The deployment owner replaces these with their own team, or (preferred) points the
# script at a local, gitignored roster file / env so real names + emails never enter the
# repo. Each person has exactly one email, so name-keying is unambiguous.
_ROSTER_FILE = Path(__file__).resolve().parent.parent / "data" / "team_roster.local.json"


def _load_roster() -> list[tuple[str, str]]:
    """Load the real roster from a local, gitignored JSON ([["Name","email"], …]) when
    present; otherwise fall back to the example placeholders below. Keeps real PII out of
    the repo while staying runnable."""
    try:
        if _ROSTER_FILE.exists():
            import json as _json
            data = _json.loads(_ROSTER_FILE.read_text(encoding="utf-8"))
            pairs = [(str(n).strip(), str(e).strip()) for n, e in data if n and e]
            if pairs:
                return pairs
    except Exception:
        pass
    return [
        ("Example User One",   "user1@example.org"),
        ("Example User Two",   "user2@example.org"),
        ("Example User Three", "user3@example.org"),
    ]


TEAM: list[tuple[str, str]] = _load_roster()

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
