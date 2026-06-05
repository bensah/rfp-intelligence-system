"""Thin wrapper around Resend.

Single send_email() helper. Reads RESEND_API_KEY + RESEND_FROM_EMAIL from
env / Streamlit secrets. Raises a clear error when not configured so the UI
can show a friendly message.
"""
from __future__ import annotations

import os
from typing import Any, Iterable


class MailerNotConfigured(RuntimeError):
    pass


def _secret(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    try:
        import streamlit as st  # type: ignore
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return None


def send_email(
    *,
    to: Iterable[str],
    subject: str,
    html: str,
    reply_to: str | None = None,
) -> dict[str, Any]:
    api_key = _secret("RESEND_API_KEY")
    sender = _secret("RESEND_FROM_EMAIL")
    if not api_key or not sender:
        raise MailerNotConfigured(
            "RESEND_API_KEY and RESEND_FROM_EMAIL must be set in env or secrets."
        )
    import resend  # local import; the package warns about being optional at import time

    resend.api_key = api_key
    payload: dict[str, Any] = {
        "from": sender,
        "to": list(to),
        "subject": subject,
        "html": html,
    }
    if reply_to:
        payload["reply_to"] = reply_to
    return resend.Emails.send(payload)
