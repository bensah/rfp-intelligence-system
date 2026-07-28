"""Super_user cross-tenant analytics dashboard — Settings → Analytics.

A first-cut platform overview (super_user only): tenant rollup, user rollup, and the
shared discovery totals. Read-only; all numbers are cross-tenant via core.analytics
(service client). A richer design (time-series, per-tenant drill-down) can come later.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import analytics, permissions


def render_super_analytics(user: dict) -> None:
    if not permissions.is_super_user(user):
        st.error("App analytics are visible to the Super User only.")
        return

    st.subheader("📈 App analytics")
    st.caption("Cross-tenant platform overview — tenants, users and shared discovery. "
               "Super User only; numbers span every tenant.")

    tenants = analytics.tenant_activity()
    users = analytics.user_stats()
    disc = analytics.system_discovery_stats()

    # ── KPI tiles ────────────────────────────────────────────────────────
    k = st.columns(4)
    k[0].metric("Active tenants",
                len([t for t in tenants if t.get("status") == "active"]),
                help=f"{len(tenants)} total (incl. suspended)")
    k[1].metric("Users", users["total"], f"{users['active']} active")
    k[2].metric("Shared catalog", f"{disc['catalog']:,}",
                help="RFPs in the shared extracted_solicitations store that every "
                     "tenant screens.")
    k[3].metric("System found", f"{disc['found']:,}",
                help=f"{disc['rejected']:,} rejected at the gate · "
                     f"{disc['runs']} discovery run(s)")

    # ── Tenants ──────────────────────────────────────────────────────────
    st.markdown("**Tenants**")
    if tenants:
        _df = pd.DataFrame([{
            "Tenant": (t.get("name") or "—") + (" ⭐" if t.get("is_platform") else ""),
            "Status": "🟢 Active" if t.get("status") == "active" else "⏸ Suspended",
            "Members": t.get("members", 0),
            "Pending": t.get("pending", 0),
            "RFPs": t.get("rfps", 0),
            "Last screening": (t.get("last_screen") or "—")[:10],
            "Created": t.get("created") or "—",
        } for t in tenants])
        st.dataframe(_df, hide_index=True, width="stretch")
    else:
        st.info("No tenants yet.")

    # ── Users by role ────────────────────────────────────────────────────
    st.markdown("**Users by role**")
    _roles = users.get("by_role") or {}
    if _roles:
        _ur = pd.DataFrame([{"Role": r, "Count": c}
                            for r, c in sorted(_roles.items(),
                                               key=lambda x: (-x[1], x[0]))])
        st.dataframe(_ur, hide_index=True, width="stretch")
    st.caption(f"{users['active']} active · {users['inactive']} inactive · "
               f"{users['total']} total")

    if disc.get("last_run"):
        st.caption(f"Last system discovery run: {str(disc['last_run'])[:16]}")
