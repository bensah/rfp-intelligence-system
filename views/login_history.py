"""Login-history panel for the Profile page — the user's own recent sign-ins.

Surfaces time · device (browser/OS parsed from the user-agent) · IP for the last few
logins so a user can spot activity they don't recognise. Rows come from `login_logs`
(migration 084), captured best-effort at login by auth.authenticator. A user sees ONLY
their own rows (RLS + explicit user_id filter here). Location-from-IP is intentionally
NOT looked up (that needs an external geo-IP call) — IP + device is shown instead.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from db.supabase_client import service_client


def _parse_ua(ua: str | None) -> str:
    """A short 'Browser · OS' label from a raw user-agent (substring heuristics, no deps)."""
    if not ua:
        return "Unknown device"
    u = ua.lower()
    if "edg/" in u:
        browser = "Edge"
    elif "opr/" in u or " opera" in u or u.startswith("opera"):
        browser = "Opera"
    elif "firefox/" in u:
        browser = "Firefox"
    elif "chrome/" in u and "chromium" not in u:
        browser = "Chrome"
    elif "safari/" in u:
        browser = "Safari"
    else:
        browser = "Browser"
    if "windows" in u:
        osn = "Windows"
    elif "android" in u:
        osn = "Android"
    elif "iphone" in u or "ipad" in u or "ios " in u:
        osn = "iOS"
    elif "mac os" in u or "macintosh" in u:
        osn = "macOS"
    elif "linux" in u:
        osn = "Linux"
    else:
        osn = "—"
    if browser == "Browser" and osn in ("iOS", "macOS"):
        browser = "Safari"          # Apple devices default to Safari when UA is terse
    return f"{browser} · {osn}"


def render_login_history(user: dict) -> None:
    """Right-rail panel: the caller's recent sign-ins."""
    # Marker so the mobile CSS (@media in core/app_header.py) can reflow this rail
    # below the main content on phones — same hook the opportunity rail uses.
    st.markdown("<div class='app-rail-marker'></div>", unsafe_allow_html=True)
    st.markdown("#### 🔐 Recent sign-ins")
    st.caption("Your last logins. See something you don't recognise? Change your password "
               "on the left and tell an admin.")
    uid = user.get("id")
    if not uid:
        st.caption("No login history for this account.")
        return
    try:
        rows = (service_client().table("login_logs")
                .select("at,ip,user_agent")
                .eq("user_id", uid).order("at", desc=True)
                .limit(12).execute().data or [])
    except Exception:
        st.caption("Login history isn't available yet (apply migration 084).")
        return
    if not rows:
        st.caption("No sign-ins recorded yet — this list fills in as you log in.")
        return
    df = pd.DataFrame([{
        "When": (r.get("at") or "")[:16].replace("T", " "),
        "Device": _parse_ua(r.get("user_agent")),
        "IP": r.get("ip") or "—",
    } for r in rows])
    st.dataframe(
        df, hide_index=True, width="stretch",
        column_config={
            "When": st.column_config.TextColumn("When", width="small"),
            "Device": st.column_config.TextColumn("Device"),
            "IP": st.column_config.TextColumn("IP", width="small"),
        })
