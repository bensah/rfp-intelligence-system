"""Account activation and password reset — a PUBLIC page, with no login on it.

WHY ITS OWN PAGE (owner, 2026-08-17). The code box used to be an expander on the sign-in
screen, next to a login form and a forgot-password form. A person holding an invitation has
no password yet, so two of the three things in front of them were useless, and the one they
needed was collapsed. Worse, it sat behind the login gate conceptually: the screen's whole
frame is "sign in", which is the one thing they cannot do.

This page carries the activation flow alone. It is reachable without signing in - App.py
runs it BEFORE the login gate - and once a password is set it offers the way on to sign-in
rather than leaving the person to find it.

What it deliberately does NOT do: nothing here redirects anyone. Visiting the app root still
lands on sign-in as before, so an ordinary user is never dumped onto an activation screen
they did not ask for.
"""
from __future__ import annotations

import streamlit as st

from auth.authenticator import (
    _hide_sidebar_on_login, activation_code_entry, handle_setup_token,
)

st.set_page_config(page_title="Activate your account", page_icon="🔑",
                   layout="centered")

# No nav rail: there is nowhere to go from here until the account works, and a sidebar full
# of pages that will bounce you to a login screen is noise.
_hide_sidebar_on_login()

# `handle_setup_token` owns the whole verdict — a token from the URL, a token kept in session
# state across the reruns of the password form, or a code pasted below. It renders the
# set-password screen and st.stop()s when it has one, so everything after this line only runs
# when there is no code to act on yet.
handle_setup_token()

# DONE MEANS DONE. Once a password has been set in this session, this page must not fall
# back to its "paste your code" form - that is what made the flow feel circular: password
# saved, and then apparently back to the beginning. The code that got you here is spent, so
# there is nothing left to paste.
_done_for = st.session_state.get("_password_set_for")
if _done_for:
    st.title("✅ All set")
    st.write(f"**{_done_for}** is ready to use.")
    st.caption("Sign in with your email and the password you just chose.")
    if st.button("Go to sign in", type="primary"):
        st.session_state.pop("_password_set_for", None)
        st.switch_page("app_pages/login.py")
    st.stop()

st.title("🔑 Activate your account")
st.caption(
    "Paste the activation code from your invitation email. If you are resetting a "
    "forgotten password, the code from that email works here too."
)

# expanded=True: this page exists FOR this form. The same control on the sign-in screen stays
# collapsed, because there it is the exception rather than the purpose.
activation_code_entry(expanded=True)

st.divider()
st.caption("Already have a working password?")
if st.button("Go to sign in"):
    # The app root renders the login screen for anyone not signed in, so switching there is
    # the whole redirect.
    st.switch_page("app_pages/home.py")
