"""Transactional emails for user-account lifecycle.

Sends:
  * Welcome + temp-password email on account creation (Manage Users
    → Add User dialog)
  * Password-reset email when admin issues a new temp (Manage Users
    → Reset Password action)

Both emails carry the user's temp password in plaintext (one-time use)
and tell them to log in + change it immediately. The `must_change_
password` flag on the users row is set in tandem with the email send,
so the next login is gated by `ensure_logged_in()` into the Change
Password screen until the user picks their own password.

Why a separate module rather than inlining the email body in the User
page: keeps the HTML template + branding logic out of the UI code and
makes it trivial to swap the channel later (Resend → SendGrid → SES).
"""
from __future__ import annotations

import os
from typing import Any

from core.mailer import MailerNotConfigured, send_email


def _app_url() -> str:
    """Where the user clicks to log in. Pulled from APP_PUBLIC_URL in env
    (set by the deployment), with a localhost fallback for dev."""
    return (
        os.environ.get("APP_PUBLIC_URL")
        or os.environ.get("APP_URL")
        or "http://localhost:8501"
    )


def _branded_html(*, recipient_name: str, body_html: str) -> str:
    """Wrap a body in the RFPIS-branded email shell. Inline styles only —
    most email clients strip <style> blocks."""
    app_url = _app_url()
    safe_name = recipient_name or "there"
    return f"""
<!doctype html>
<html>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background:#f5f7fa; padding:24px; margin:0;">
    <div style="max-width:560px; margin:0 auto; background:#ffffff; border-radius:8px; padding:32px 28px; border:1px solid #e3e7e3;">
      <div style="font-size:18px; font-weight:700; color:#1e3a8a; margin-bottom:4px;">RFP Intelligence System</div>
      <div style="font-size:12px; color:#94a3b8; margin-bottom:20px;">Powered by Taadom Digital</div>
      <div style="font-size:15px; color:#1f2937; line-height:1.55;">
        <p>Hi <strong>{safe_name}</strong>,</p>
        {body_html}
        <p style="margin-top:28px; font-size:13px; color:#475569;">
          Log in at <a href="{app_url}" style="color:#00703C;">{app_url}</a>.
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


def send_welcome_email(
    *,
    to_email: str,
    to_name: str | None,
    temp_password: str,
) -> dict[str, Any]:
    """Sent when admin/super creates a new account via Manage Users.

    Raises MailerNotConfigured if Resend env not set — caller decides
    whether to fall back to showing the password on-screen."""
    body = f"""
        <p>An administrator has created an RFPIS account for you.</p>
        <p>Your temporary password is:</p>
        <p style="font-family:monospace; font-size:18px; background:#f8f9fa; padding:12px 16px; border-radius:6px; border:1px solid #e3e7e3; letter-spacing:1px; color:#1e3a8a;">
          {temp_password}
        </p>
        <p><strong>Important — you'll be required to change this password the first time you log in.</strong> Pick something only you know.</p>
    """
    return send_email(
        to=[to_email],
        subject="Your RFPIS account — temporary password inside",
        html=_branded_html(recipient_name=to_name or "", body_html=body),
    )


def send_password_reset_email(
    *,
    to_email: str,
    to_name: str | None,
    temp_password: str,
) -> dict[str, Any]:
    """Sent when admin/super resets a user's password from Manage Users."""
    body = f"""
        <p>An administrator has reset your RFPIS password at your request.</p>
        <p>Your new temporary password is:</p>
        <p style="font-family:monospace; font-size:18px; background:#f8f9fa; padding:12px 16px; border-radius:6px; border:1px solid #e3e7e3; letter-spacing:1px; color:#1e3a8a;">
          {temp_password}
        </p>
        <p><strong>You'll be required to change this password on your next login.</strong></p>
        <p style="font-size:13px; color:#475569;">
          If you did NOT request this reset, contact your administrator
          immediately — your previous password no longer works.
        </p>
    """
    return send_email(
        to=[to_email],
        subject="RFPIS password reset — temporary password inside",
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
        subject="RFPIS — your registration is pending approval",
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
        subject="RFPIS — your account is now active",
        html=_branded_html(recipient_name=to_name or "", body_html=body),
    )


__all__ = [
    "send_welcome_email",
    "send_password_reset_email",
    "send_signup_received_email",
    "send_account_approved_email",
    "MailerNotConfigured",
]
