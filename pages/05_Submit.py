"""Page 5 — Manual RFP submission form (standalone page).

NOTE: The "Open in Review" navigation target lives at pages/01_Pipeline.py
(was renamed from pages/01_Leads.py). See Home.py CARDS for the parallel
update.

The actual form lives in `views/submit_form.py` so the same code renders
inside the @st.dialog modal opened from Home and Leads pages. This page
just wraps it with page config + the login gate.

Per the 2026-06-05 policy change: submit no longer blocks on duplicate
detection. Duplicates are reconciled at DISPLAY time in Tracking / Report.
"""
from __future__ import annotations

import streamlit as st

# Must be the FIRST Streamlit call so a direct refresh lands in wide layout.
st.set_page_config(
    page_title="Submit — RFPIS",
    page_icon="🛈",
    layout="wide",
    initial_sidebar_state="expanded",
)

from auth.authenticator import ensure_logged_in
from views.submit_form import render_submit_form

user = ensure_logged_in()
if not user:
    st.stop()

from core.app_header import render_app_header  # noqa: E402
render_app_header()

# Constrain form width on desktop (~2/3 of viewport); full width on mobile.
st.markdown(
    """
    <style>
      @media (min-width: 900px) {
        section.main div.block-container { max-width: 66%; }
      }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Submit RFP")
st.caption(
    "Capture an opportunity found outside the automated Friday scan. "
    "Submission is recorded immediately — duplicate detection runs at "
    "display time so your entry is never lost or blocked."
)

render_submit_form(user, key_prefix="page")
