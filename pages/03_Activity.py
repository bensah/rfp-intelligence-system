"""Page 3 — Activities.

Combines Meeting Logs (weekly team-call notes) and Engagement Logs
(donor touchpoints) into a single 2-tab page. Renamed from "Meetings"
on 2026-06-05 to avoid confusion with "team leads" / "proposal leads"
terminology used elsewhere in the app.
"""
from __future__ import annotations

import streamlit as st

# Must be the FIRST Streamlit call so a direct refresh lands in wide layout.
st.set_page_config(
    page_title="Activity - RFPIS",
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

st.title("Meetings and Engagement Activities")
st.caption(
    "**Weekly Touchpoints** — Monday Business Development Team call notes "
    "(per-week, with the rota and follow-up tracker). **Engagements** — "
    "every donor-facing touchpoint (call, pitch, conference, scoping) "
    "towards the KR2.2 quarterly target. **Pending Actions** — every "
    "open follow-up from both, owner-summarised + filterable."
)

# Tabs renamed 2026-06-05: BDT Check-Ins → BDT Touchpoints. Added
# Pending Actions as the third tab so unresolved items from both data
# sources are visible on a single screen (no jumping per-week to find
# what's still open).
tab_meetings, tab_engagements, tab_pending = st.tabs(
    ["Weekly Touchpoints", "Engagements", "Pending Actions"]
)

with tab_meetings:
    render_view("meetings")

with tab_engagements:
    render_view("engagements")

with tab_pending:
    render_view("pending_actions")
