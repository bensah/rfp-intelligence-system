"""Help page — reached from the top-right user menu (❓).

A navigation-first orientation: where things live, what each page/tab does,
and how the weekly opportunity workflow flows. Role-aware — admins also see a
Settings section.
"""
from __future__ import annotations

import streamlit as st

from core import permissions, settings

user = st.session_state.get("app_user") or {}
is_admin = permissions.is_admin(user)

st.title("❓ Help & navigation guide")
st.caption(
    "How to find your way around the RFP Intelligence System (RFPIS). "
    "Anything this doesn't answer? Ask an administrator.")

st.markdown(
    "**RFPIS** discovers funding opportunities (RFPs, RFIs, EOIs, calls for "
    "proposals, grand challenges) from donor sites and the web, screens each "
    "against your eligibility rules, and helps the team decide **Proceed / "
    "Park / Decline** — all in one place.")

st.divider()

# ── Getting around ──────────────────────────────────────────────────────────
st.subheader("🧭 Getting around")
st.markdown(
    "There are two navigation zones:\n"
    "- **Left sidebar** — your six work pages. Use the **«/»** control to "
    "expand it to labels or collapse it to an icon rail; your **role** shows "
    "at the bottom.\n"
    "- **Top-right icons** — 🔍 **Search**, 🔔 **Notifications**, and the "
    "👤 **person menu**.\n\n"
    "Most pages carry **tabs** along the top — that's where the detail lives.")

# ── Pages ───────────────────────────────────────────────────────────────────
st.subheader("📋 The pages (left sidebar)")
st.markdown(
    "- **🏠 Home** — dashboard: pending actions, recent activity, and quick "
    "links to submit or review an opportunity.\n"
    "- **📚 Pipelines** — the core workflow in four tabs: **Screen** (triage "
    "new finds) → **Review** (check against the MUST / PREFER criteria) → "
    "**Tracking** (manage what you're pursuing) → **Summary** (totals).\n"
    "- **💼 Grants** — opportunities won or under active management, with "
    "reporting deadlines.\n"
    "- **🗒️ Actions** — three tabs: **Team Calls** (meeting notes), "
    "**Engagements** (donor touchpoints), **Pending** (open follow-ups).\n"
    "- **📊 Report** — analytics across the whole pipeline (volume by donor, "
    "decision mix, team activity); exportable to PDF.\n"
    "- **🗺️ Donors** — the donor catalogue + intelligence: focus areas, award "
    "ranges, and contacts.")

# ── Top-right tools ─────────────────────────────────────────────────────────
st.subheader("🔝 Top-right tools")
_menu = ("**Profile** (your details + password), **Help** (this page)")
if is_admin:
    _menu += ", **Settings** (admin tools)"
_menu += ", and **Sign Out**"
st.markdown(
    "- **🔍 Search** — type a keyword and hit **Search** to open a results "
    "page listing matching **pages & tabs**, **opportunities**, and "
    "**donors** — click any result to jump there. It can also search the "
    "**web** for live, validated, recent funding calls.\n"
    "- **🔔 Notifications** — an org-wide activity feed: when auto-scans run, "
    "when the next scan is scheduled, and newly added opportunities. The badge "
    "counts items since you last hit **Mark all as read**.\n"
    f"- **👤 Person menu** — {_menu}.")

# ── Workflow ────────────────────────────────────────────────────────────────
st.subheader("🔄 How an opportunity flows")
st.markdown(
    "1. **Discovered** — the Friday auto-scan (or a manual submission from "
    "Home / Pipelines) lands it in **Screen**.\n"
    "2. **Screened** — auto-scoring tags eligibility (MUST 1–5 / PREFER 6–9) "
    "and proposes a decision.\n"
    "3. **Reviewed** — on **Review**, the team confirms or overrides it "
    "(Proceed / Park / Decline).\n"
    "4. **Tracked** — anything you **Proceed** on moves to **Tracking** with "
    "an owner, stage, and deadlines; wins graduate to **Grants**.")

# ── Account ─────────────────────────────────────────────────────────────────
st.subheader("👤 Your account & role")
st.markdown(
    "- **👤 → Profile** to edit your name and contact details or **change "
    "your password**.\n"
    "- Your **role** is shown at the bottom of the sidebar — most teammates "
    "are **Contributors**.\n"
    "- Need more access? Ask an administrator to adjust your role under "
    "**Settings → Manage Users**.")

# ── Admins only ─────────────────────────────────────────────────────────────
if is_admin:
    st.subheader("⚙️ Settings (administrators)")
    st.markdown(
        "Open from **👤 → Settings**:\n"
        "- **Setup** — org profile, working year, currencies, Excel sync, and "
        "scan eligibility (health themes, geographic scope, criteria).\n"
        "- **Manage Users** — add/edit teammates, set roles, reset passwords.\n"
        "- **User Access** — the role × surface permission matrix.\n"
        "- **Records** — the full opportunity backend (view / edit / delete, "
        "export, share).\n"
        "- **Sources** — the donor-site catalogue the scanner crawls.\n"
        "- **Manual Scan** — trigger a scan now and see scan history.\n"
        "- **Blacklist** — domains to exclude from scans + web search.")

st.divider()
st.caption(f"{settings.get_org_name()} · powered by RFPIS")
