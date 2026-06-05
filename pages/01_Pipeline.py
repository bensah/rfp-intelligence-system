"""Page 1 — RFPs.

Single landing page for the four sub-views that were previously separate
sidebar items. Each tab renders the original view module from `views/`
via `render_view()`.
"""
from __future__ import annotations

import streamlit as st

# Must be the FIRST Streamlit call on this page (and every page). Without
# it a direct-refresh on a non-Home page falls back to "centered" layout
# until the user navigates to Home and back.
st.set_page_config(
    page_title="RFP Pipeline - RFPIS",
    page_icon="🛈",
    layout="wide",
    initial_sidebar_state="expanded",
)

from auth.authenticator import ensure_logged_in
from core.render_view import render_view
from views.submit_form import render_submit_form

user = ensure_logged_in()
if not user:
    st.stop()

from core.app_header import render_app_header  # noqa: E402
render_app_header()


# ---- Submit-RFP modal (Streamlit ≥1.32 @st.dialog) ----
@st.dialog("Submit a new RFP", width="large")
def _submit_rfp_modal():
    render_submit_form(
        user,
        key_prefix="leads_modal",
        on_success=lambda row: st.rerun(),
    )


# Title + Submit-RFP button on the same row (button top-right).
_title_col, _btn_col = st.columns([5, 1])
with _title_col:
    st.title("RFP Pipelines & Eligibility Screening")
    st.caption(
        "Friday-scan + manual submissions through the full lifecycle: "
        "**Screen** (this week's intake), **Review** (deep-dive on one RFP), "
        "**Tracking** (Proceed pipeline), **Summary** (KPIs & reflections)."
    )
with _btn_col:
    st.markdown("<div style='height: 0.8rem'></div>", unsafe_allow_html=True)
    if st.button("📝 Submit RFP", type="primary",
                 use_container_width=True, key="leads_submit_rfp_btn",
                 help="Capture an opportunity outside the Friday scan."):
        _submit_rfp_modal()

tab_screen, tab_review, tab_tracking, tab_summary = st.tabs(
    ["Screen", "Review", "Tracking", "Summary"]
)

with tab_screen:
    render_view("screened_rfp")

with tab_review:
    render_view("review_rfp")

with tab_tracking:
    render_view("y2d_pipeline")

with tab_summary:
    render_view("summary_rfp")
