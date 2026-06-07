"""Profile page — reached from the top-right user menu (not the sidebar).

Self-service only: every logged-in user manages their own profile + password
here. Admin-only surfaces (Manage Users, User Access) moved to the Settings
page in the 2026-06-07 nav redesign.
"""
from __future__ import annotations

import streamlit as st

from db.supabase_client import get_client
from views.account_sections import render_my_profile, render_change_password

user = st.session_state["app_user"]
sb = get_client()

st.title("Profile")

tab_profile, tab_pw = st.tabs(["My Profile", "Change Password"])
with tab_profile:
    render_my_profile(user, sb)
with tab_pw:
    render_change_password(user, sb)
