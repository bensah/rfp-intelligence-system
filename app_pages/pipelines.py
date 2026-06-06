"""Page 1 — RFPs.

Single landing page for the four sub-views that were previously separate
sidebar items. Each tab renders the original view module from `views/`
via `render_view()`.
"""
from __future__ import annotations

import streamlit as st

from core.render_view import render_view
from views.submit_form import render_submit_form

user = st.session_state["app_user"]


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
    st.title("Found RFPs Pipeline")
# with _btn_col:
#     st.markdown("<div style='height: 0.8rem'></div>", unsafe_allow_html=True)
#     if st.button("📝 Submit RFP", type="primary",
#                  use_container_width=True, key="leads_submit_rfp_btn",
#                  help="Capture an opportunity outside the Friday scan."):
#         _submit_rfp_modal()

tab_screen, tab_review, tab_tracking, tab_summary = st.tabs(
    ["Screen", "Review", "Tracking", "Summary"]
)

with tab_screen:
    render_view("screened_rfp")

with tab_review:
    render_view("review_rfp")

with tab_tracking:
    render_view("tracking_rfp")

with tab_summary:
    render_view("summary_rfp")
