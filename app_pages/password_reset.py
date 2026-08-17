"""Password reset — /password-reset. PUBLIC, like /activate-account.

Somebody who has forgotten their password cannot sign in, so the two things they might need
- asking for a reset, and redeeming the code that arrives - must both be reachable without
signing in. They used to live as expanders on the login screen, underneath a login form that
is the one thing the person cannot use.

Both halves are here:
  * redeem a reset code (or arrive with ?token=... from the email link);
  * ask an admin for a reset, if they do not have a code yet.

As with /activate-account, nothing redirects anyone here. The app root still goes to sign-in.
"""
from __future__ import annotations

import streamlit as st

from auth.authenticator import (
    _hide_sidebar_on_login, activation_code_entry, handle_setup_token,
    password_reset_request_form,
)

st.set_page_config(page_title="Reset your password", page_icon="🔐",
                   layout="centered")

_hide_sidebar_on_login()

# Owns the verdict for a token in the URL, a token held across the reruns of the password
# form, or a code pasted below: it renders the set-password screen and stops when it has one.
# Everything after this line runs only when there is nothing to redeem yet.
handle_setup_token()

st.title("🔐 Reset your password")

st.markdown("#### I have a reset code")
activation_code_entry(expanded=True)

st.divider()
st.markdown("#### I do not have one yet")
password_reset_request_form()

st.divider()
if st.button("Back to sign in"):
    st.switch_page("app_pages/login.py")
