"""Page 1 — RFPs.

Single landing page for the four sub-views that were previously separate
sidebar items. Each tab renders the original view module from `views/`
via `render_view()`.
"""
from __future__ import annotations

import streamlit as st

from core.render_view import render_view
from core.scan_runner import run_scan_now, scan_banner
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


# Title + "Scan now" button on the same row (button top-right). The scan
# action used to live inside the Screen tab; it sits here now so it's
# reachable from every tab and aligns with the page title.
_title_col, _btn_col = st.columns([5, 1.5])
with _title_col:
    st.title("Discovered Opportunities Pipeline")
with _btn_col:
    st.write("")  # nudge the button down to the title baseline
    if st.button("🔄 Scan now", type="primary", width='stretch',
                 key="pipelines_scan_now",
                 help="Run the donor-source scanner now. New RFPs are inserted "
                      "and appear on the Screen tab after the run completes."):
        # Lock nav while the long scan subprocess blocks the script, so the
        # user can't switch tabs into a half-rendered/grayed-out view.
        st.markdown(
            """
            <style>
              [data-testid="stTabs"] [role="tablist"],
              [data-testid="stSidebarNav"] {
                pointer-events: none !important; opacity: 0.45 !important;
              }
            </style>
            """,
            unsafe_allow_html=True,
        )
        _who = user.get("name") or user.get("email") or "unknown"
        st.info(scan_banner(_who))
        run_scan_now(triggered_by=f"manual:{_who}")
        st.rerun()

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
