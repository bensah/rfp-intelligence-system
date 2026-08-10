"""Page 6 — Report.

KPI dashboard tracing the full RFPIS pipeline from search activity to
grants secured. Org-aware (header + footer pull from app_settings) and
period-filterable (year / YTD / last 90d / last 12m / all time).

The whole story lives in `views/report.py` — this wrapper just sets the
page config and dispatches.
"""
from __future__ import annotations

import streamlit as st

from core.render_view import render_view

st.title("Your Fund-raising Activity Report")

render_view("report")
