"""Sign in — /login.

The login screen has always rendered wherever you happened to be, which meant the app root
was both "home" and "the login page" depending on who was asking. Giving it its own path
means it can be linked to, bookmarked and redirected to, and it makes the address bar tell
the truth about which of the two you are looking at.

`ensure_logged_in` still owns the whole flow — cookie restore, the form, the
must-change-password gate, tenant context. This page is a location for it, not a second
implementation of it.
"""
from __future__ import annotations

import streamlit as st

from auth.authenticator import ensure_logged_in

user = ensure_logged_in()
if user:
    # Already signed in - /login has nothing to say. Home is where they were going.
    st.switch_page("app_pages/home.py")
