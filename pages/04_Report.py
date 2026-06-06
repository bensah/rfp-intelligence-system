"""Page 6 — Report.

KPI dashboard tracing the full RFPIS pipeline from search activity to
grants secured. Org-aware (header + footer pull from app_settings) and
period-filterable (year / YTD / last 90d / last 12m / all time).

The whole story lives in `views/report.py` — this wrapper just sets the
page config and dispatches.
"""
from __future__ import annotations

import streamlit as st

# Must be the FIRST Streamlit call so a direct refresh of this page lands
# in wide layout instead of falling back to centered view.
st.set_page_config(
    page_title="Report — RFPIS",
    page_icon="🛈",
    layout="wide",
    initial_sidebar_state="expanded",
)

from auth.authenticator import ensure_logged_in
from core.render_view import render_view

if not ensure_logged_in():
    st.stop()

from core.app_header import render_app_header  # noqa: E402
render_app_header()

st.title("BDT Activity Report")
st.caption(
    "End-to-end activity dashboard — from scanner output through team "
    "decisions to secured grants. Use the period selector below to scope "
    "every section consistently."
)

render_view("report")
