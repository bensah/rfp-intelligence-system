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


COOKIE_NAME = "rfpis_session"
COOKIE_EXPIRY_DAYS = 1 / 3  # ~8 hours

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
    """Render the login form. Returns the authenticated user dict, or None."""
    auth = get_authenticator()
    try:
        auth.login(location="main")
    except TypeError:
        auth.login("Login", "main")  # back-compat with older API

    status = st.session_state.get("authentication_status")
    if status is False:
        st.error("Username or password is incorrect.")
        return None
    if status is None:
        st.info("Please log in to continue.")
        return None

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
    """Render 'Signed in as ... · Role: ... · Logout' in the sidebar.

    Called from BOTH login_gate() (first login) AND ensure_logged_in()
    (subsequent page loads where the session is already cached). Before
    this split, ensure_logged_in() short-circuited on the session cache
    and the user block only rendered on Home → it silently disappeared
    on every other page after navigation.
    """
    name = user.get("name") or user.get("email")
    role = user.get("role")
    if auth is None:
        auth = get_authenticator()
    with st.sidebar:
        st.caption(f"Signed in as **{name}**  \nRole: `{role}`")
        try:
            auth.logout(location="sidebar")
        except TypeError:
            auth.logout("Logout", "sidebar")


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
        return user
    return login_gate()


def require_role(user: dict[str, Any], roles: list[str]) -> bool:
    if not user:
        return False
    return user.get("role") in roles


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
