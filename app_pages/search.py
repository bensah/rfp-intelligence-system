"""Site-wide search results page.

Reached from the header 🔍 box (which stows the query in
st.session_state['site_search_query'] and switches here). Works like a
mini search engine scoped to the app: a query box at the top, then top
matching links grouped by kind — Pages & tabs, Opportunities, Donors —
each a clickable link that navigates to the relevant page.

Hidden from the sidebar nav (registered in App.py but CSS-hidden in
core/app_header.py, same as Profile / Help / Settings).
"""
from __future__ import annotations

import streamlit as st

from core import permissions, site_search

user = st.session_state.get("app_user") or {}
is_admin = permissions.is_admin(user)

st.title("🔍 Search")

# ── Query box (pre-filled, editable to refine) ──────────────────────────────
current_q = (st.session_state.get("site_search_query") or "").strip()
with st.form("site_search_form", clear_on_submit=False):
    c1, c2 = st.columns([6, 1])
    q_in = c1.text_input(
        "Search the site", value=current_q,
        placeholder="Search pages, tabs, opportunities, donors…",
        label_visibility="collapsed")
    submitted = c2.form_submit_button("Search", type="primary",
                                      use_container_width=True)
if submitted:
    st.session_state["site_search_query"] = (q_in or "").strip()
    st.rerun()

q = (st.session_state.get("site_search_query") or "").strip()
if len(q) < 2:
    st.info("Type a keyword above (2+ characters) to search across pages, "
            "tabs, opportunities and donors.")
    st.stop()

# ── Run the search ──────────────────────────────────────────────────────────
nav = site_search.search_nav(q, is_admin)
opps = site_search.search_opportunities(q)
donors = site_search.search_donors(q)
total = len(nav) + len(opps) + len(donors)

st.caption(f"{total} result{'s' if total != 1 else ''} for “{q}”")
if total == 0:
    st.warning("No matches. Try fewer or different keywords — e.g. a donor "
               "name, a page like *blacklist* or *manage users*, or part of "
               "an opportunity title.")
    st.stop()

# ── Pages & tabs ────────────────────────────────────────────────────────────
if nav:
    st.subheader(f"Pages & tabs · {len(nav)}")
    for label, path in nav:
        st.page_link(path, label=label, icon="➡️")

# ── Opportunities ───────────────────────────────────────────────────────────
if opps:
    st.subheader(f"Opportunities · {len(opps)}")
    for o in opps:
        st.page_link(o["page"], label=o["title"], icon="📄")
        meta = " · ".join(p for p in (
            o.get("funder"),
            (f"Deadline {o['deadline']}" if o.get("deadline") else ""),
            (o.get("decision") or "").title(),
            (o.get("source") or "").title(),
        ) if p)
        if meta:
            st.caption(meta)

# ── Donors ──────────────────────────────────────────────────────────────────
if donors:
    st.subheader(f"Donors · {len(donors)}")
    for d in donors:
        st.page_link(d["page"], label=d["name"], icon="🗺️")
