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
    "**BDT Check-in Calls** — Monday Business Development Team call notes "
    "(per-week, with the rota and follow-up tracker). **Engagement Logs** — "
    "every donor-facing touchpoint (call, pitch, conference, scoping) "
    "towards the KR2.2 quarterly target."
)

tab_meetings, tab_engagements = st.tabs(["BDT Check-Ins", "Engagements"])

with tab_meetings:
    render_view("meeting_logs")

with tab_engagements:
    render_view("engagement_logs")
