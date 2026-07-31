"""Page 1 — RFPs.

Single landing page for the four sub-views that were previously separate
sidebar items. Each tab renders the original view module from `views/`
via `render_view()`.
"""
from __future__ import annotations

import streamlit as st

from core.render_view import render_view
from core.scan_runner import run_screening_now
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


# Title + the two page actions (Submit New Funding · Find Eligible Funding) on the same
# row, reachable from every tab. The live-opportunity rail is lowered BELOW this row.
_title_col, _submit_col, _scan_col = st.columns([4.4, 1.6, 1.8])
with _title_col:
    st.title("Discovered Funding Opportunities")
with _submit_col:
    st.write("")  # nudge the button down to the title baseline
    if st.button("📝 Submit New Funding", type="secondary", width='stretch',
                 key="pipelines_submit_new",
                 help="Capture a funding opportunity you found outside the scan."):
        st.switch_page("app_pages/submit_rfp.py")
with _scan_col:
    st.write("")
    # "Find Eligible Funding" (tenant-facing) = screen the platform's curated/extracted
    # store against THIS org's eligibility. Fast (no web crawl) — the heavy extraction
    # crawl is a separate admin job (Settings → Manual Scan → Run Extraction). Flips to a
    # disabled "running" state.
    _scan_slot = st.empty()
    _go = _scan_slot.button(
        "🎯 Find Eligible Funding", type="primary", width='stretch',
        key="pipelines_scan_now",
        help="Find the funding your organisation is potentially eligible for, from "
             "the platform's curated store — runs in seconds (no web crawl). New "
             "eligible opportunities appear on the Screen tab.")
    if _go:
        _scan_slot.button("⏳ Selecting eligible funding…", disabled=True,
                          width='stretch', key="pipelines_scan_running")
        _who = user.get("name") or user.get("email") or "unknown"
        run_screening_now(triggered_by=f"match:{_who}")
        st.rerun()

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
