"""One-time links for account setup and password reset.

Replaces emailing a temporary password. A password in an inbox stays valid
until somebody changes it, and it is sitting in the sender's outbox and in
every mail server that relayed it. A token expires, is single-use, and is
worthless afterwards.

# What the link can do

Exactly one thing: open the set-password screen for one account. It does
NOT sign anyone in. That is the deliberate trade — a forwarded invite lets
an attacker set a password, but it cannot be done invisibly, because the
real user's own login then fails and they say so. A link that authenticated
the bearer would hand over the account silently.

# What is stored

Only `sha256(token)`. The token exists in the email and nowhere else, so
this table leaking yields nothing usable — the same reasoning behind
storing `password_hash` rather than a password.

# Lifetimes

    invite   7 days   a new joiner may not read their email today
    reset    2 hours  requested deliberately and acted on at once, so a
                      short window limits how long a copy stays live

Issuing a new token for an account invalidates any outstanding one, so a
re-sent invite silently retires the first link rather than leaving two
valid ways in.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db.supabase_client import service_client

PURPOSE_INVITE = "invite"
PURPOSE_RESET = "reset"

_TTL = {
    PURPOSE_INVITE: timedelta(days=7),
    PURPOSE_RESET: timedelta(hours=2),
}

# 32 bytes of urandom, URL-safe. Long enough that guessing is not a strategy
# and short enough to survive a mail client wrapping the line.
_TOKEN_BYTES = 32


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_token(
    *,
    user_id: str,
    purpose: str,
    created_by: Optional[str] = None,
) -> tuple[str, datetime]:
    """Mint a link token for one account.

    Returns (raw_token, expires_at). The raw token is returned ONCE, to be
    put in the email — it is not recoverable afterwards, by us or by anyone
    who reads the database.

    Any outstanding token for the same user is retired first: re-sending an
    invite should replace the old link, not add a second working one.
    """
    if purpose not in _TTL:
        raise ValueError(f"Unknown token purpose {purpose!r}")

    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = _now() + _TTL[purpose]
    sb = service_client()

    # Retire anything still live for this user. Marked used rather than
    # deleted so the audit trail still shows a link was issued and
    # superseded.
    try:
        sb.table("user_password_tokens").update(
            {"used_at": _now().isoformat()}
        ).eq("user_id", user_id).is_("used_at", None).execute()
    except Exception:
        # A failure to retire old tokens must not block issuing a new one -
        # the new link is what the user is waiting on, and the old ones
        # still expire on their own.
        pass

    sb.table("user_password_tokens").insert({
        "user_id": user_id,
        "token_hash": _digest(raw),
        "purpose": purpose,
        "expires_at": expires_at.isoformat(),
        "created_by": created_by,
    }).execute()

    return raw, expires_at


def peek_token(raw_token: str) -> Optional[dict[str, Any]]:
    """Validate a token WITHOUT consuming it.

    Used to decide whether to render the set-password screen. Consuming
    here would burn the link on a page refresh, which is the most common
    thing a confused user does.

    Returns the joined token+user record, or None when the token is
    unknown, already used, or expired. The three are deliberately not
    distinguished to the caller: telling a stranger "that link expired"
    confirms the link once existed.
    """
    if not raw_token:
        return None
    sb = service_client()
    try:
        res = (
            sb.table("user_password_tokens")
            .select("id, user_id, purpose, expires_at, used_at")
            .eq("token_hash", _digest(raw_token))
            .limit(1)
            .execute()
        )
    except Exception:
        return None

    rows = res.data or []
    if not rows:
        return None
    row = rows[0]
    if row.get("used_at"):
        return None

    try:
        expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
    except Exception:
        return None
    if expires <= _now():
        return None

    try:
        ures = (
            sb.table("users")
            .select("id, email, name, is_active")
            .eq("id", row["user_id"])
            .limit(1)
            .execute()
        )
    except Exception:
        return None
    users = ures.data or []
    if not users:
        return None

    row["user"] = users[0]
    return row


def consume_token(raw_token: str) -> Optional[dict[str, Any]]:
    """Validate and burn a token in one step.

    Called only once the new password has been accepted. The update is
    conditional on `used_at is null`, so two tabs racing to submit cannot
    both succeed — whichever loses gets None and is told the link is spent.
    """
    record = peek_token(raw_token)
    if record is None:
        return None

    sb = service_client()
    try:
        res = (
            sb.table("user_password_tokens")
            .update({"used_at": _now().isoformat()})
            .eq("id", record["id"])
            .is_("used_at", None)
            .execute()
        )
    except Exception:
        return None

    if not (res.data or []):
        # Someone else consumed it between peek and update.
        return None
    return record


def invalidate_tokens_for_user(user_id: str) -> None:
    """Retire every outstanding link for an account.

    Called after a password is set by any route. A reset link that still
    works after the user has already chosen a password is a live credential
    nobody is watching.
    """
    try:
        service_client().table("user_password_tokens").update(
            {"used_at": _now().isoformat()}
        ).eq("user_id", user_id).is_("used_at", None).execute()
    except Exception:
        pass


def build_link(app_url: str, raw_token: str) -> str:
    """The URL that goes in the email.

    Streamlit reads `st.query_params`, so the token rides as a query string
    on the app root. Built from the deployment's own APP_PUBLIC_URL rather
    than a literal, so a domain change does not send people somewhere dead.
    """
    return f"{app_url.rstrip('/')}/?token={raw_token}"


__all__ = [
    "PURPOSE_INVITE",
    "PURPOSE_RESET",
    "issue_token",
    "peek_token",
    "consume_token",
    "invalidate_tokens_for_user",
    "build_link",
]
