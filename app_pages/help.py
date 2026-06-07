"""Help page — reached from the top-right user menu.

Plain-language orientation for new team members: what each page does and
how the weekly opportunity pipeline flows. Kept deliberately short.
"""
from __future__ import annotations

import streamlit as st

from core import settings

st.title("Help & guide")
st.caption(
    "A quick orientation to the RFP Intelligence System (RFPIS). "
    "Need something that isn't here? Ask an administrator.")

st.subheader("What RFPIS does")
st.markdown(
    "RFPIS finds funding opportunities (RFPs, RFIs, EOIs, calls for "
    "proposals, grand challenges) from donor websites and feeds, screens "
    "each one against your eligibility rules, and helps the team decide "
    "whether to **Proceed**, **Park**, or **Decline** — all in one place.")

st.subheader("The pages")
st.markdown(
    "- **🏠 Home** — your dashboard: pending actions, recent activity, and "
    "quick links to submit or review an opportunity.\n"
    "- **📚 Pipelines** — the heart of the workflow: Screen new finds, "
    "Review them against eligibility criteria, and Track what's in flight.\n"
    "- **💼 Grants** — opportunities you've won or are actively managing, "
    "with reporting deadlines.\n"
    "- **🗒️ Actions** — your outstanding to-dos across all opportunities.\n"
    "- **📊 Report** — analytics: volume by donor, decision mix, team "
    "activity, exportable to PDF.\n"
    "- **🗺️ Donors** — the donor catalogue + intelligence (focus areas, "
    "award ranges, contacts).")

st.subheader("How an opportunity flows")
st.markdown(
    "1. **Discovered** — the weekly scan (or a manual entry) adds it to "
    "Screening.\n"
    "2. **Screened** — auto-scoring tags eligibility (MUST / PREFER "
    "criteria) and proposes a decision.\n"
    "3. **Reviewed** — the team confirms or overrides the decision.\n"
    "4. **Tracked** — anything you Proceed on moves into the active "
    "pipeline with owners + deadlines.")

st.subheader("Your account")
st.markdown(
    "- Use the **person icon (top-right)** → **Profile** to update your "
    "details or change your password.\n"
    "- Your **role** is shown at the bottom of the sidebar. Most teammates "
    "are **Contributors**; administrators have an extra **Settings** entry "
    "in the top-right menu.\n"
    "- Need elevated access? Ask an administrator to adjust your role "
    "under **Settings → Manage Users**.")

st.divider()
st.caption(f"{settings.get_org_name()} · powered by RFPIS")
