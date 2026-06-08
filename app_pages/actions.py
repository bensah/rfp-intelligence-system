"""Page 3 — Actions.

Combines Meeting Logs (weekly team-call notes) and Engagement Logs
(donor touchpoints) into a single tabbed page. Renamed from "Meetings"
on 2026-06-05, then from "Activity" to "Actions" on 2026-06-06, to avoid
confusion with "team leads" / "proposal leads" terminology used elsewhere
in the app.
"""
from __future__ import annotations

import streamlit as st

from core.render_view import render_view

st.title("Meetings and Engagement Activities")

# Tab labels use generic team terms. "Pending" is the third tab so
# unresolved items from both data sources are visible on a single
# screen (no jumping per-week to find what's still open).
tab_meetings, tab_engagements, tab_pending, tab_schedule = st.tabs(
    ["Team Calls", "Engagements", "Pending", "Schedule"]
)

with tab_meetings:
    render_view("meetings")

with tab_engagements:
    render_view("engagements")

with tab_pending:
    render_view("pending_actions")

with tab_schedule:
    render_view("schedule")
