"""Live opportunity right-rail — three cards shown beside the Entity view and on the
Pipeline page:

  💰 Top Funding      — biggest / most-urgent calls, fit-agnostic
  ✅ Top Matches      — strong fit (Proceed / Park / high alignment)
  ✨ Also Interesting — fresh non-matches worth a glance

Reads the CURRENT entity's pipeline (rfp_submissions — tenant-scoped by get_client, so a
super_user's 'view-as' shows the viewed entity, and public/individual tenants' rows are
included per the scoping wrapper). Cached ~45s so it refreshes as new calls land without
hammering the DB — a live feed, not a static card. Classification lives in
core.opportunity_feed.
"""
from __future__ import annotations

import streamlit as st

from core import opportunity_feed as _feed
from db.supabase_client import get_client, _tenant_scope_tid

_FIELDS = ("uid,opportunity_title,funding_agency,call_award_value,currency,"
           "call_submission_deadline,call_geographic_scope,date_posted,search_date,"
           "created_at,alignment_score,auto_recommendation,decision,opportunity_link")


@st.cache_data(ttl=45, show_spinner=False)
def _load_pipeline(scope_key: str) -> list[dict]:
    """Recent pipeline rows for the effective tenant. `scope_key` (the tenant id) is part
    of the cache key so tenants / view-as never share a cached feed."""
    try:
        return (get_client().table("rfp_submissions").select(_FIELDS)
                .order("created_at", desc=True).limit(800).execute().data or [])
    except Exception:
        return []


def _fmt_amount(amount: float, currency: str) -> str:
    if not amount or amount <= 0:
        return ""
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get((currency or "").upper(), "")
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if amount >= div:
            return f"{sym}{amount / div:.1f}{unit}".replace(".0", "")
    return f"{sym}{amount:,.0f}"


def _deadline_chip(item: dict) -> str:
    d = item.get("days_until")
    if d is None:
        return ""
    if d < 0:
        return "⏳ closed"
    if d == 0:
        return "⏳ due today"
    if d <= 14:
        return f"🔴 {d}d left"
    if d <= 45:
        return f"🟠 {d}d left"
    return f"🗓 {d}d"


def _render_item(item: dict) -> None:
    # Title links to the Pipeline (Review tab) so it's reviewable in-app; the external
    # call URL is offered as a secondary ↗ when present.
    title = (item["title"][:70] + "…") if len(item["title"]) > 70 else item["title"]
    st.markdown(f"**[{title}](/pipelines)**")
    bits = [b for b in (item.get("funder", "")[:34],
                        _fmt_amount(item["amount"], item["currency"]),
                        _deadline_chip(item)) if b]
    line = "  ·  ".join(bits)
    if item.get("link"):
        line += f"  ·  [↗]({item['link']})"
    if line:
        st.caption(line)


def _card(title: str, help_txt: str, items: list[dict], empty: str) -> None:
    with st.container(border=True):
        st.markdown(f"#### {title}")
        st.caption(help_txt)
        if not items:
            st.caption(f"_{empty}_")
            return
        for i, it in enumerate(items):
            _render_item(it)
            if i < len(items) - 1:
                st.divider()


def render_opportunity_rail() -> None:
    """Render the three live cards. Safe to call in any column/container."""
    scope = _tenant_scope_tid() or "all"
    groups = _feed.classify(_load_pipeline(f"t:{scope}"))

    st.markdown("<div style='font-size:.8rem;color:#00703C;font-weight:700;"
                "letter-spacing:.03em;'>🔴 LIVE OPPORTUNITY FEED</div>",
                unsafe_allow_html=True)
    st.caption("Updates as new funding calls arrive. Open any item in **Pipelines → "
               "Review**.")
    _card("💰 Top Funding", "Biggest / most-urgent calls — regardless of fit.",
          groups["top_funding"], "No live opportunities yet — run a scan.")
    _card("✅ Top Matches", "Strong fit for this entity (Proceed / Park / high alignment).",
          groups["top_matches"], "No strong matches yet.")
    _card("✨ Also Interesting", "Fresh calls that aren't a match but are worth a look.",
          groups["other"], "Nothing else new right now.")
