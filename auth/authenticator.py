"""Login gate for Streamlit.

Backs streamlit-authenticator with the Supabase `users` table. Passwords are
bcrypt-hashed and stored in `users.password_hash`. The admin seeds users on
first deploy; new invitees get a one-time setup email and choose their password
on first login (Phase 1 path: admin sets initial hash via the Admin panel).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import bcrypt
import streamlit as st
import streamlit_authenticator as stauth

from db.supabase_client import get_client


# ---------------------------------------------------------------------------
# Streamlit Cloud cookie-restore fix
# ---------------------------------------------------------------------------
# streamlit-authenticator >= 0.4.1 reads the re-auth cookie from
# `st.context.cookies` (the request-header snapshot). That works locally but is
# unreliable behind Streamlit Cloud's proxy, so a refresh on *.streamlit.app
# finds no cookie and bounces to the login form (works fine locally — exactly
# the symptom we hit). Versions <= 0.3.x read it via the JS CookieManager
# component, which survives the proxy. We restore that read here with a tiny
# monkeypatch (cookie READ only — token decode + expiry stay the library's).
# Wrapped in try/except: if the library's internals change, it silently no-ops
# back to stock behaviour rather than breaking auth.
def _patch_authenticator_cookie_read() -> None:
    try:
        from datetime import datetime as _dt
        from streamlit_authenticator.models import cookie_model as _cm

        def _get_cookie(self):  # noqa: ANN001
            if st.session_state.get("logout"):
                return False
            try:
                self.token = self.cookie_manager.get(self.cookie_name)
            except Exception:
                return None  # JS read failed → fall through to the login form
            if self.token is not None:
                self.token = self._token_decode()
                if (self.token is not False and "username" in self.token
                        and self.token["exp_date"] > _dt.now().timestamp()):
                    return self.token
            return None

        _cm.CookieModel.get_cookie = _get_cookie
    except Exception:
        pass


_patch_authenticator_cookie_read()


COOKIE_NAME = "rfpis_session"
COOKIE_EXPIRY_DAYS = 1 / 3  # ~8 hours

# Self-service sign-up is OFF: a small, invite-only team where admins create
# accounts directly (Admin -> Manage users). Keeps spammers out. The Sign Up
# form code below is preserved — flip this to True to re-enable self-service.
_SIGNUP_ENABLED = False

# Small in-process cache so a Home / page-switch render doesn't hit Supabase
# on every rerun. Auto-invalidates after 60s; admins can also force-refresh
# via `clear_credentials_cache()` after editing users.
_CRED_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_CRED_TTL_SEC = 60.0


def clear_credentials_cache() -> None:
    _CRED_CACHE["at"] = 0.0
    _CRED_CACHE["data"] = None


def _load_credentials() -> dict[str, Any]:
    """Build the credentials dict streamlit-authenticator expects, from Supabase.

    Wrapped in a tiny TTL cache + 3-attempt retry so the occasional transient
    httpx.ReadError when Supabase blips doesn't crash the entire app on
    every page navigation.
    """
    now = time.time()
    if _CRED_CACHE["data"] is not None and (now - _CRED_CACHE["at"]) < _CRED_TTL_SEC:
        return _CRED_CACHE["data"]

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            sb = get_client()
            rows = (
                sb.table("users")
                .select("email,name,role,password_hash,is_active")
                .eq("is_active", True)
                .execute()
                .data
                or []
            )
            creds = {"usernames": {}}
            for r in rows:
                email = r.get("email")
                if not email or not r.get("password_hash"):
                    continue
                creds["usernames"][email] = {
                    "name": r.get("name") or email,
                    "password": r["password_hash"],
                    "email": email,
                    "roles": [r.get("role") or "collaborator"],
                }
            _CRED_CACHE["at"] = now
            _CRED_CACHE["data"] = creds
            return creds
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))

    # All retries exhausted. If we have a stale cached copy use it — better
    # to log a stale user in than to crash the whole app.
    if _CRED_CACHE["data"] is not None:
        return _CRED_CACHE["data"]
    raise last_exc if last_exc else RuntimeError("could not load credentials")


def _secret_key() -> str:
    key = os.environ.get("APP_SECRET_KEY")
    if not key:
        try:
            key = str(st.secrets["APP_SECRET_KEY"])
        except Exception:
            key = "dev-insecure-change-me"
    return key


def get_authenticator() -> stauth.Authenticate:
    creds = _load_credentials()
    # auto_hash=False (0.4.x): our password_hash values are already bcrypt,
    # so we must stop the library from re-hashing them on init.
    kwargs = dict(
        credentials=creds,
        cookie_name=COOKIE_NAME,
        cookie_key=_secret_key(),
        cookie_expiry_days=COOKIE_EXPIRY_DAYS,
    )
    try:
        return stauth.Authenticate(**kwargs, auto_hash=False)
    except TypeError:
        return stauth.Authenticate(**kwargs)  # 0.3.x: no auto_hash kwarg


def login_gate() -> Optional[dict[str, Any]]:
    """Render the login form. Returns the authenticated user dict, or None.

    When not authenticated, also renders self-service Sign Up + Forgot
    Password expanders directly under the login form so a first-time
    visitor can register without admin help.

    Cookie-restore quirk: streamlit-authenticator fetches the session
    cookie via a JS component round-trip. On the FIRST script run after
    a hard refresh, the JS hasn't returned yet → `authentication_status`
    is None → user incorrectly sees the login form despite holding a
    valid cookie. We force one rerun (capped per browser session) to
    give the cookie manager time to settle before falling through to
    the login form.
    """
    auth = get_authenticator()

    # Landing-page branding so a first-time visitor immediately knows what
    # this app is (the stock streamlit-authenticator form has no identity).
    # Rendered into a slot we CLEAR once authenticated, so it only ever shows
    # on the login screen — never pushing content down on the logged-in pages.
    _brand_slot = st.empty()
    _brand_slot.markdown(
        "<div style='text-align:center; margin:1.25rem 0 0.75rem;'>"
        "<div style='font-size:2.2rem; font-weight:800; color:#1e3a8a; "
        "letter-spacing:0.05em;'>RFPIS</div>"
        "<div style='font-size:1.05rem; font-weight:600; color:#00703C; "
        "margin-top:-0.1rem;'>RFP Intelligence System</div>"
        "<div style='color:#475569; font-size:0.9rem; margin-top:0.35rem;'>"
        "Weekly opportunities discovery, eligibility screening and decision "
        "support.</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    try:
        auth.login(location="main")
    except TypeError:
        auth.login("Login", "main")  # back-compat with older API

    status = st.session_state.get("authentication_status")

    if status is None and not st.session_state.get("_auth_cookie_settled"):
        # First post-refresh render — let the cookie manager finish its
        # JS round-trip, then rerun once. The flag prevents an infinite
        # loop when there genuinely is no cookie (anonymous visitor).
        st.session_state["_auth_cookie_settled"] = True
        import time as _t
        _t.sleep(0.4)
        st.rerun()

    if status is False:
        _hide_sidebar_on_login()
        st.error("Username or password is incorrect.")
        _render_signup_and_reset_forms()
        return None
    if status is None:
        _hide_sidebar_on_login()
        st.info("Please log in to continue.")
        _render_signup_and_reset_forms()
        return None
    # Successful auth — clear the cookie-settled flag so the next
    # logout-then-login cycle gets a fresh wait window.
    st.session_state.pop("_auth_cookie_settled", None)
    _brand_slot.empty()  # login-screen branding only — remove once signed in

    email = st.session_state.get("username")
    name = st.session_state.get("name")
    user = _fetch_user(email) if email else None

    if not user or not user.get("is_active"):
        st.error("Your account is inactive. Contact an administrator.")
        return None

    _record_login(email)
    st.session_state["app_user"] = user
    st.session_state.setdefault("display_name", name or email)

    _render_sidebar_user(user, auth=auth)
    return user


def _render_sidebar_user(user: dict[str, Any], auth=None) -> None:
    """Render 'Signed in as ... · Role: ...' at the bottom of the sidebar.

    The Sign Out control moved to the top-right user menu
    (`core/app_header._render_user_menu`) in the 2026-06-07 redesign, so no
    logout button is drawn here — just the identity + a friendly role badge
    (e.g. collaborator → "Contributor"). The `auth` param is retained for
    call-site compatibility but is now unused.

    Called from BOTH login_gate() (first login) AND ensure_logged_in()
    (subsequent page loads where the session is already cached) so the
    block persists on every page, not just Home.
    """
    from core import permissions  # local import — avoid import cycle
    name = user.get("name") or user.get("email")
    with st.sidebar:
        st.caption(
            f"Signed in as **{name}**  \n"
            f"Role: `{permissions.role_label(user)}`")


def _fetch_user(email: str) -> Optional[dict[str, Any]]:
    sb = get_client()
    res = sb.table("users").select("*").eq("email", email).limit(1).execute()
    return res.data[0] if res.data else None


def _record_login(email: str) -> None:
    try:
        sb = get_client()
        sb.table("users").update(
            {"last_login_at": datetime.now(timezone.utc).isoformat()}
        ).eq("email", email).execute()
    except Exception:
        pass  # non-fatal


def ensure_logged_in() -> Optional[dict[str, Any]]:
    """Use at the top of EVERY page (not just Home).

    Path:
      1. If `st.session_state["app_user"]` is already set (within this
         server session), return it immediately — zero cost.
      2. Otherwise call `login_gate()`, which reads the
         `rfpis_session` cookie. If the cookie is valid (within 8h),
         streamlit-authenticator restores the session and we proceed.
      3. If no valid cookie, the login form is rendered IN PLACE on the
         current page. After login, the user lands back on this page —
         no need to bounce to Home and back.

    Pages should do:
        from auth.authenticator import ensure_logged_in
        user = ensure_logged_in()
        if not user:
            st.stop()
    """
    if "app_user" in st.session_state:
        # Cached — short-circuit the login flow, but DO re-render the
        # sidebar user block (Signed in / Logout). Without this the
        # sidebar appears blank below the page nav on every page after
        # the user navigates away from Home.
        user = st.session_state["app_user"]
        _render_sidebar_user(user)
        _gate_must_change_password(user)
        return user
    user = login_gate()
    if user:
        _gate_must_change_password(user)
    return user


# ---------------------------------------------------------------------------
# First-login password gate
# ---------------------------------------------------------------------------
# When admin creates a user OR resets their password, the users row is
# stamped with `must_change_password = true`. Until the user picks a
# new password, every page render runs through this gate. The gate
# renders an inline "Change your password" form on top of whatever page
# they're trying to visit, then `st.stop()`s — they cannot access any
# app content until the flag is cleared.

def _gate_must_change_password(user: dict[str, Any]) -> None:
    """Block all page rendering while user.must_change_password is True.

    Renders a self-contained Change Password form. Once the user
    successfully changes their password, the flag is cleared, the
    session_state copy is updated, and st.rerun() lets the original
    page render normally."""
    if not user or not user.get("must_change_password"):
        return

    # Hide the sidebar nav while gated — same UX as the login screen,
    # so the user can't escape the gate by clicking another page.
    _hide_sidebar_on_login()

    st.title("🔐 Set a new password")
    st.warning(
        "You're using a temporary password issued by an administrator. "
        "Choose a new password before continuing."
    )

    # Plain widgets + a regular button (NOT st.form). A form-submit button
    # rendered in the entry script — before st.navigation().run() in the MPA-v2
    # flow — was not reliably registering its click, so the page sat static.
    current_pw = st.text_input(
        "Current (temporary) password", type="password", key="fcp_current")
    new_pw = st.text_input(
        "New password", type="password", key="fcp_new",
        help="At least 8 characters, mix of letters and digits.")
    confirm_pw = st.text_input(
        "Confirm new password", type="password", key="fcp_confirm")
    submit = st.button("🔐 Save new password", type="primary",
                       key="fcp_submit", use_container_width=False)

    if submit:
        # Re-fetch the user's stored hash — session copy could be stale.
        fresh = _fetch_user(user.get("email") or "")
        stored_hash = (fresh or {}).get("password_hash") or ""
        errs: list[str] = []
        try:
            ok = bool(current_pw) and bcrypt.checkpw(
                current_pw.encode("utf-8"), stored_hash.encode("utf-8"))
        except Exception:
            ok = False
        if not ok:
            errs.append("Current password is incorrect.")
        if not new_pw or len(new_pw) < 8:
            errs.append("New password must be at least 8 characters.")
        if new_pw and (not any(c.isalpha() for c in new_pw)
                       or not any(c.isdigit() for c in new_pw)):
            errs.append("New password must include letters AND digits.")
        if new_pw != confirm_pw:
            errs.append("Confirm password does not match.")
        if new_pw and new_pw == current_pw:
            errs.append("New password must be different from the "
                         "temporary one.")

        if errs:
            st.error("Please fix:\n\n- " + "\n- ".join(errs))
        else:
            saved, err = False, None
            try:
                res = (get_client().table("users").update({
                    "password_hash": hash_password(new_pw),
                    "must_change_password": False,
                    "password_changed_at":
                        datetime.now(timezone.utc).isoformat(),
                }).eq("email", user.get("email")).execute())
                saved = bool(getattr(res, "data", None))
            except Exception as exc:
                err = str(exc)

            if err:
                st.error(f"Save failed: {err}")
            elif not saved:
                st.error(
                    "Couldn't save the new password — no matching account row "
                    "was updated (the database may have rejected the write). "
                    "Contact an administrator.")
            else:
                clear_credentials_cache()
                # Flip the flag in the session copy so the next run skips this
                # gate, then rerun OUTSIDE the try/except (never risk a
                # control-flow signal being caught) to load the app.
                user["must_change_password"] = False
                st.session_state["app_user"] = user
                st.rerun()

    st.stop()


def require_role(user: dict[str, Any], roles: list[str]) -> bool:
    if not user:
        return False
    return user.get("role") in roles


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


# ---------------------------------------------------------------------------
# Hide the sidebar on the login screen
# ---------------------------------------------------------------------------
# Visitors who aren't logged in shouldn't see page names they can't
# access. Inject CSS only in the not-authenticated branches of
# `login_gate()` so the hide rule disappears the moment the user
# authenticates and the regular app renders.
def _hide_sidebar_on_login() -> None:
    st.markdown(
        """
        <style>
          [data-testid="stSidebar"],
          section[data-testid="stSidebar"],
          [data-testid="collapsedControl"] {
            display: none !important;
          }
          /* Reclaim the full viewport width for the login form. */
          [data-testid="stMainBlockContainer"],
          .block-container {
            max-width: 720px !important;
            margin: 0 auto !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Self-service forms shown under the login screen
# ---------------------------------------------------------------------------
# Sign Up: any visitor can register but the account lands inactive
# (is_active=false) so they can't log in until admin / super_user
# approves them from the Manage Users tab.
#
# Forgot Password: writes a row to `password_reset_requests`. Admin
# picks it up from the Manage Users banner and issues a temp password
# via the existing reset action. No email infrastructure required —
# the request is the notification.

def _render_signup_and_reset_forms() -> None:
    """Render Sign Up + Forgot Password expanders below the login form.

    Both are collapsed by default so the login UX stays uncluttered for
    returning users. Expand-on-click reveals the form."""
    st.markdown(" ")  # small spacer below the login form

    # Open the Sign Up expander automatically when a sign-up has just
    # completed, so the user sees the confirmation block in place of
    # the form (the form is hidden once `_signup_done_for` is set).
    signup_done_for = st.session_state.get("_signup_done_for")
    if _SIGNUP_ENABLED:  # deactivated — admins create accounts directly
      with st.expander("📝 Sign Up — new account",
                       expanded=bool(signup_done_for)):
        if signup_done_for:
            # ─── Post-success state: form HIDDEN, confirmation shown ──
            st.success(
                f"✅ **Account created for `{signup_done_for}`.** "
                f"Your access is **pending admin approval** — you'll "
                f"be able to log in once an admin activates your "
                f"account."
            )
            st.caption(
                "A confirmation email was sent (if our email service is "
                "configured). If you don't see it, contact your admin "
                "directly. You can close this panel and log in once "
                "you've been approved."
            )
            if st.button("Sign up another account",
                          key="signup_reset_btn"):
                st.session_state.pop("_signup_done_for", None)
                for k in ("su_email", "su_name", "su_pw", "su_confirm",
                          "su_dept"):
                    st.session_state.pop(k, None)
                st.rerun()
        else:
            # ─── Pre-success state: render the form ───────────────────
            st.caption(
                "Create an account. Your access will be **pending "
                "admin approval** — you'll be able to log in once an "
                "admin activates your account and assigns a role."
            )
            with st.form("signup_form", clear_on_submit=False):
                sc1, sc2 = st.columns(2)
                su_email = sc1.text_input("Email *", key="su_email")
                su_name = sc2.text_input("Full name *", key="su_name")
                sc3, sc4 = st.columns(2)
                su_pw = sc3.text_input(
                    "Password *", type="password", key="su_pw",
                    help="At least 8 characters, mix of letters and "
                         "digits.")
                su_confirm = sc4.text_input(
                    "Confirm password *", type="password",
                    key="su_confirm")
                su_dept = st.text_input("Department (optional)",
                                          key="su_dept")
                su_submit = st.form_submit_button(
                    "➕ Create account", type="primary")

            if su_submit:
                errs: list[str] = []
                if not su_email or "@" not in (su_email or ""):
                    errs.append("Valid email is required.")
                if not su_name:
                    errs.append("Full name is required.")
                if not su_pw or len(su_pw) < 8:
                    errs.append("Password must be at least 8 "
                                 "characters.")
                if su_pw and (not any(c.isalpha() for c in su_pw)
                               or not any(c.isdigit() for c in su_pw)):
                    errs.append("Password must include letters AND "
                                 "digits.")
                if su_pw != su_confirm:
                    errs.append("Passwords do not match.")
                # Email uniqueness — fail-soft if Supabase blips.
                if not errs:
                    try:
                        existing = get_client().table("users") \
                            .select("email") \
                            .eq("email", su_email.strip()) \
                            .limit(1).execute().data or []
                        if existing:
                            errs.append("An account with this email "
                                         "already exists. Use Forgot "
                                         "password if you can't log "
                                         "in.")
                    except Exception:
                        pass

                if errs:
                    st.error("Please fix:\n\n- " + "\n- ".join(errs))
                else:
                    try:
                        get_client().table("users").insert({
                            "email": su_email.strip(),
                            "name":  su_name.strip(),
                            "role":  "collaborator",
                            "department":
                                (su_dept or "").strip() or None,
                            "password_hash": hash_password(su_pw),
                            "is_active": False,  # pending approval
                            "must_change_password": False,
                            "password_changed_at":
                                datetime.now(timezone.utc).isoformat(),
                        }).execute()
                        clear_credentials_cache()
                    except Exception as exc:
                        st.error(f"Sign-up failed: {exc}")
                        return
                    # Best-effort confirmation email — silent on error
                    # so a transient SMTP issue doesn't block the
                    # signup itself.
                    try:
                        from core.user_emails import (
                            send_signup_received_email,
                        )
                        send_signup_received_email(
                            to_email=su_email.strip(),
                            to_name=su_name.strip(),
                        )
                    except Exception:
                        pass
                    # Flag completion + rerun to swap the form for the
                    # confirmation block.
                    st.session_state["_signup_done_for"] = \
                        su_email.strip()
                    st.rerun()

    with st.expander("🔐 Forgot password?", expanded=False):
        st.caption(
            "Enter your email and request a reset. Once an admin approves "
            "it, the app emails you a temporary password — sign in with it "
            "and you'll be prompted to set a new password immediately."
        )
        with st.form("forgot_pw_form", clear_on_submit=True):
            fp_email = st.text_input("Email", key="fp_email")
            fp_submit = st.form_submit_button("📨 Request password reset")

        if fp_submit:
            if not fp_email or "@" not in fp_email:
                st.error("Enter a valid email.")
            else:
                try:
                    # Don't reveal whether the email exists (anti-
                    # enumeration). Insert the request either way; admin
                    # will see it and decide whether to action.
                    get_client().table("password_reset_requests").insert({
                        "email": fp_email.strip(),
                    }).execute()
                    st.success(
                        "✅ Request received. After an admin approves it, "
                        "the app will email you a temporary password. Sign "
                        "in with it and set a new password right away."
                    )
                except Exception as exc:
                    st.error(f"Could not record request: {exc}")
