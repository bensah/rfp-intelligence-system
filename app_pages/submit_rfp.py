"""Submit a discovered RFP — standalone page.

Open to ANY logged-in user (no admin gate). Opened from the "Submit Discovered
RFP" button (Home) via st.switch_page, or directly at /submit-new-rfp. Reuses the
shared form in views/submit_form.py (same code the modal used).
"""
from __future__ import annotations

import streamlit as st

from views.submit_form import render_submit_form

user = st.session_state["app_user"]

_t, _b = st.columns([5, 1.2])
_t.title("📝 Submit a Discovered RFP")
with _b:
    st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
    if st.button("← Home", width="stretch", key="submit_back_home"):
        st.switch_page("app_pages/home.py")

_main, _rail = st.columns([3.4, 1], gap="medium")
with _rail:
    from views.opportunity_rail import render_opportunity_rail
    render_opportunity_rail()
with _main:
    st.caption(
        "Capture an opportunity you found outside the Friday scan. Submitted "
        "immediately — duplicate-detection runs at display time, so re-entries are "
        "merged in the dashboards automatically."
    )

    # on_success omitted → the form shows its own inline success (uid + score + rec).
    render_submit_form(user, key_prefix="submit_page")
