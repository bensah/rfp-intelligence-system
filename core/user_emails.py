"""Transactional emails for user-account lifecycle.

Sends:
  * Welcome + temp-password email on account creation (Manage Users
    → Add User dialog)
  * Password-reset email when admin issues a new temp (Manage Users
    → Reset Password action)

Account setup and reset now carry a ONE-TIME LINK rather than a
password. A password sent by email stays valid in that inbox until
somebody changes it; a link expires, works once, and authorises only
setting a password — it does not sign the bearer in. See
core/password_tokens.py.

`must_change_password` remains on the users row as a second gate, so an
account that somehow acquires a password without going through a link is
still stopped at the Change Password screen.

Why a separate module rather than inlining the email body in the User
page: keeps the HTML template + branding logic out of the UI code and
makes it trivial to swap the channel later (Resend → SendGrid → SES).

Branding is resolved at send time from app_settings, never hardcoded:
`email_product_name` for the header, `email_product_short_name` for
subject lines. RFPIS is a multi-tenant product, so the deploying
organisation's name belongs in that deployment's configuration and not in
this repository.
"""
from __future__ import annotations

import os
from typing import Any

from core.mailer import MailerNotConfigured, send_email


def _html_escape(text: str) -> str:
    """Escape a value being interpolated into an email body.

    Everything these templates inject is operator-supplied — the product
    name comes from app_settings, the expiry hint from the caller — so an
    ampersand in a deployment's own name is the realistic case rather than
    an attack. Escaping it keeps the markup valid instead of producing an
    email that renders half a header.
    """
    import html as _html
    return _html.escape(text or "")


def _app_url() -> str:
    """Where the user clicks to log in, and now the ORIGIN OF EVERY SETUP LINK.

    Same resolution order as `_product_name()` / `_short_name()` / `_powered_by()`:
    app_settings first, then env, then a localhost fallback for dev. It read env only,
    which was survivable while this string merely appeared as "Log in at …" in the footer
    and became a real hazard once account setup depends on it: with APP_PUBLIC_URL unset,
    every invite and reset link points at http://localhost:8501 and the recipient cannot
    set a password at all. A temporary password at least worked. Reading the setting means
    the deployment's own URL can be configured in the same place as the rest of its
    identity, rather than existing only as an environment variable somebody must remember.
    """
    try:
        from core.settings import get_setting
        v = (get_setting("app_public_url") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return (
        os.environ.get("APP_PUBLIC_URL")
        or os.environ.get("APP_URL")
        or "http://localhost:8501"
    )


def _powered_by() -> str:
    """Optional 'Powered by …' email-footer brand — NOT hardcoded, so the deploying
    company's name isn't baked into the code. Read from app_settings (key
    'email_powered_by'), falling back to the EMAIL_POWERED_BY env var; empty → the footer
    line is omitted entirely. Set it once (Settings/script) to restore branding."""
    try:
        from core.settings import get_setting
        v = (get_setting("email_powered_by") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return (os.environ.get("EMAIL_POWERED_BY") or "").strip()


def _product_name() -> str:
    """The product name in the email header.

    Resolved, not hardcoded, for the same reason `_powered_by()` is: RFPIS
    is a multi-tenant product and the deploying organisation's name must not
    be baked into the source. A deployment sets `email_product_name` in
    app_settings (or EMAIL_PRODUCT_NAME in env) to brand its own mail —
    e.g. prefixing the organisation's acronym — and the code stays generic.
    """
    try:
        from core.settings import get_setting
        v = (get_setting("email_product_name") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return (os.environ.get("EMAIL_PRODUCT_NAME") or "").strip() or "RFP Intelligence System"


def _short_name() -> str:
    """Short form used in subject lines, where width is scarce.

    Same resolution order as `_product_name()`. Kept separate so a
    deployment can have a long header ("… RFP Intelligence System") and a
    tight subject ("… RFPIS account") without one dictating the other.
    """
    try:
        from core.settings import get_setting
        v = (get_setting("email_product_short_name") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return (os.environ.get("EMAIL_PRODUCT_SHORT_NAME") or "").strip() or "RFPIS"


def _help_url() -> str:
    """Deep link to the in-app Help page.

    Streamlit serves pages under the url_path registered in App.py, so Help
    is always <app>/help. Built from _app_url() rather than written out, so
    a deployment that moves domain does not silently email a dead link.
    """
    return _app_url().rstrip("/") + "/help"


def _branded_html(*, recipient_name: str, body_html: str) -> str:
    """Wrap a body in the RFPIS-branded email shell. Inline styles only —
    most email clients strip <style> blocks."""
    import html as _html
    app_url = _app_url()
    help_url = _help_url()
    safe_name = recipient_name or "there"
    _pb = _powered_by()
    _powered_html = (
        f'<div style="font-size:12px; color:#94a3b8; margin-bottom:20px;">'
        f'Powered by {_html.escape(_pb)}</div>' if _pb else "")
    return f"""
<!doctype html>
<html>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#f5f7fa; padding:24px; margin:0;">
    <div style="max-width:560px; margin:0 auto; background:#ffffff; border-radius:8px; padding:32px 28px; border:1px solid #e3e7e3;">
      <div style="font-size:18px; font-weight:700; color:#1e3a8a; margin-bottom:4px;">{_html.escape(_product_name())}</div>
      {_powered_html}
      <div style="font-size:15px; color:#1f2937; line-height:1.55;">
        <p>Hi <strong>{safe_name}</strong>,</p>
        {body_html}
        <p style="margin-top:28px; font-size:13px; color:#475569;">
          Log in at <a href="{app_url}" style="color:#00703C;">{app_url}</a>
          &nbsp;·&nbsp;
          <a href="{help_url}" style="color:#00703C;">Help</a>
        </p>
        <p style="font-size:12px; color:#94a3b8; margin-top:24px;">
          This is an automated message. If you didn't expect this email,
          contact your administrator.
        </p>
      </div>
    </div>
  </body>
</html>
"""


def _code_fallback(url: str) -> str:
    """The token on its own, to be pasted into the app.

    A LINK IS NOT A RELIABLE CARRIER on a hosted Streamlit app. The host bootstraps a
    session before serving a cold request and the round trip drops the query string:

        app.example/?token=ABC
          -> share.streamlit.io/-/auth/app?redirect_uri=https%3A%2F%2Fapp.example%2F
          -> app.example/-/login?payload=...        (the token is gone)

    Clicking a link in an email IS the cold case, so the one visitor the link was written
    for is precisely the one it fails for. Printing the token as a code, and accepting it
    on the login screen, gives that person a route that does not depend on the query
    surviving. Same single secret either way - nothing weaker is introduced.
    """
    import html as _html
    token = ""
    try:
        from urllib.parse import urlsplit, parse_qs
        token = (parse_qs(urlsplit(url).query).get("token") or [""])[0]
    except Exception:
        token = ""
    if not token:
        return ""
    return f"""
        <p style="font-size:13px; color:#475569; margin-top:20px;">
          If that opens the app without asking for a password, choose
          <strong>&#128273; Have an activation or reset code?</strong> on the sign-in
          screen and paste this:
        </p>
        <p style="font-family:monospace; font-size:14px; background:#f8f9fa; padding:12px 14px; border-radius:6px; border:1px solid #e3e7e3; word-break:break-all; color:#1f2937;">
          {_html.escape(token)}
        </p>
    """


def _action_button(url: str, label: str) -> str:
    """A link styled as a button, with the URL also shown as text.

    Some corporate mail clients strip or rewrite anchors, and some users
    forward the mail to a phone where tapping is unreliable. Showing the URL
    underneath means the link is still usable when the button is not.
    """
    import html as _html
    safe = _html.escape(url, quote=True)
    return f"""
        <p style="margin:24px 0;">
          <a href="{safe}" style="background:#00703C; color:#ffffff; text-decoration:none; padding:12px 22px; border-radius:6px; font-weight:600; display:inline-block;">{_html.escape(label)}</a>
        </p>
        <p style="font-size:12px; color:#64748b; word-break:break-all;">
          If the button does not work, paste this into your browser:<br>{safe}
        </p>
    """


def send_welcome_email(
    *,
    to_email: str,
    to_name: str | None,
    setup_link: str,
    expires_hint: str = "7 days",
) -> dict[str, Any]:
    """Sent when an admin creates an account via Manage Users.

    Carries a one-time link, never a password. A password mailed in
    plaintext stays valid in that inbox — and in the sender's outbox, and
    on every mail server that relayed it — until somebody changes it. The
    link expires, works once, and authorises only setting a password: it
    does not sign anyone in.

    Raises MailerNotConfigured if the mail transport is unset; the caller
    decides whether to fall back to showing the link on screen.
    """
    # FRAMED AS ACTIVATION (owner, 2026-08-17). What the recipient is being asked to do is
    # turn on an account somebody made for them; choosing a password is how that happens,
    # not the point of the email. "Set up your account" also reads like configuration work,
    # and "Set my password" invites the question "which password?" from someone who has
    # never had one. Activate first, password second.
    short = _short_name()
    body = f"""
        <p>An administrator has created a {_html_escape(short)} account for you.</p>
        <p>Activate it and choose a password to finish:</p>
        {_action_button(setup_link, "Activate account")}
        {_code_fallback(setup_link)}
        <p style="font-size:13px; color:#475569;">
          This link works once and expires in {_html_escape(expires_hint)}.
          If it expires, ask your administrator to send a new one.
        </p>
    """
    return send_email(
        to=[to_email],
        subject=f"Activate your {short} account",
        html=_branded_html(recipient_name=to_name or "", body_html=body),
    )


def send_password_reset_email(
    *,
    to_email: str,
    to_name: str | None,
    reset_link: str,
    expires_hint: str = "2 hours",
) -> dict[str, Any]:
    """Sent when an admin resets a user's password from Manage Users.

    Shorter-lived than an invite: a reset is requested deliberately and
    acted on immediately, so there is no reason for the link to stay usable
    for days in an inbox.
    """
    short = _short_name()
    body = f"""
        <p>An administrator has started a password reset for your
        {_html_escape(short)} account.</p>
        <p>Choose a new password:</p>
        {_action_button(reset_link, "Change my password")}
        {_code_fallback(reset_link)}
        <p style="font-size:13px; color:#475569;">
          This link works once and expires in {_html_escape(expires_hint)}.
        </p>
        <p style="font-size:13px; color:#475569;">
          If you did NOT expect this, contact your administrator. Your
          existing password still works until this link is used, so nothing
          has changed yet.
        </p>
    """
    return send_email(
        to=[to_email],
        subject=f"Change your {short} password",
        html=_branded_html(recipient_name=to_name or "", body_html=body),
    )


def send_signup_received_email(
    *,
    to_email: str,
    to_name: str | None,
) -> dict[str, Any]:
    """Sent when a visitor self-registers via the Sign Up form on the
    login page. The account is created `is_active=false` and stays
    inactive until an admin approves it from Manage Users; this email
    just confirms receipt of the registration."""
    body = """
        <p>Thanks for registering for RFPIS.</p>
        <p>Your account has been created and is <strong>pending
        administrator approval</strong>. You'll be able to log in once
        an admin activates your account and assigns you a role.</p>
        <p style="font-size:13px; color:#475569;">
          We'll send another email when your account is approved.
          No action is needed from you in the meantime.
        </p>
    """
    return send_email(
        to=[to_email],
        subject=f"{_short_name()} — your registration is pending approval",
        html=_branded_html(recipient_name=to_name or "", body_html=body),
    )


def send_account_approved_email(
    *,
    to_email: str,
    to_name: str | None,
) -> dict[str, Any]:
    """Sent when an admin flips a previously-inactive self-signed-up
    account to active. Lets the user know they can now log in."""
    body = """
        <p>Good news — an administrator has <strong>approved your
        RFPIS account</strong>. You can now log in with the password
        you chose during registration.</p>
    """
    return send_email(
        to=[to_email],
        subject=f"{_short_name()} — your account is now active",
        html=_branded_html(recipient_name=to_name or "", body_html=body),
    )


__all__ = [
    "send_welcome_email",
    "send_password_reset_email",
    "send_signup_received_email",
    "send_account_approved_email",
    "MailerNotConfigured",
]
