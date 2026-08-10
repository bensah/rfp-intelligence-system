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


# The page title only. The Submit New Funding + Eligibility Scan actions live on the
# "Weekly Screening Pipeline" header inside the Screen tab (see views/screened_rfp.py).
st.title("Your Funding Eligibility Pipeline")

# Outcome banner from the last run (survives the post-run rerun).
_pipe_banner = st.session_state.pop("admin_scan_banner", None)
if _pipe_banner:
    (st.success if _pipe_banner.get("ok") else st.error)(_pipe_banner["msg"])

# Page body (left/main) + the live opportunity right-rail, lowered below the title row.
_main, _rail = st.columns([3.4, 1], gap="medium")

with _rail:
    from views.opportunity_rail import render_opportunity_rail
    render_opportunity_rail()

with _main:

    # FOCUSED SINGLE-RFP MODE — /pipelines?uid=<uid>. st.tabs cannot be selected
    # programmatically, so a deep link that only preselected the row would still land the
    # user on the Screen tab hunting for it. When a uid is present we render the Review
    # view on its own instead: the link opens exactly that RFP, in the normal read-only
    # view, with its existing Update Decision / Edit RFP buttons.
    _focus_uid = (st.query_params.get("uid") or "").strip()
    if _focus_uid:
        _b1, _b2 = st.columns([1, 4])
        if _b1.button("← All pipelines", key="pipelines_exit_focus"):
            st.query_params.pop("uid", None)
            st.session_state.pop("_deep_uid_applied", None)
            st.rerun()
        _b2.caption(f"Showing one RFP (`{_focus_uid}`) — use **← All pipelines** for the "
                    "full Screen / Review / Track / Summary view.")
        render_view("review_rfp")
        st.stop()

    tab_screen, tab_review, tab_tracking, tab_summary = st.tabs(
        ["Screen", "Review", "Track", "Summary"]
    )

    with tab_screen:
        render_view("screened_rfp")

    with tab_review:
        render_view("review_rfp")

    with tab_tracking:
        render_view("tracking_rfp")

    with tab_summary:
        render_view("summary_rfp")
