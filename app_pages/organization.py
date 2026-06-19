"""Organization Details — a full, READ-ONLY view of the deploying org.

Everyone can view; editing is admin / super-user only. The full edit form lives
in Settings → Setup (Profile · Bid Fitness · Team · Scan Preferences); the
"✏️ Edit organization" button jumps there. (An in-place edit overlay that reuses
that form is the next iteration.)

Auth + the global header already ran in App.py; this file is content-only.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import org_profile as _orgp
from core import permissions, settings
from core.program_area_classifier import category_full as _cat, subarea_label as _sub
from core.program_area_select import rating_bars_html

user = st.session_state["app_user"]
can_edit = permissions.is_admin(user)          # admin OR super_user only
org = settings.get_org()
prof = _orgp.get_profile()

# ── Header ──────────────────────────────────────────────────────────────────
_hl, _hr = st.columns([5, 1.4])
_hl.title("Organization Details")
if can_edit:
    if _hr.button("✏️ Edit organization", width="stretch", type="primary"):
        st.switch_page("app_pages/admin.py")
else:
    _hr.caption("View only — editing is restricted to app owners.")

_logo_bytes, _ = settings.get_org_logo()
_top = st.columns([1, 5])
if _logo_bytes:
    try:
        _top[0].image(_logo_bytes, width=120)
    except Exception:
        pass
with _top[1]:
    st.subheader(org.get("org_name") or "Your organization")
    _sub_bits = [b for b in (org.get("org_team"), org.get("org_country")) if b]
    if _sub_bits:
        st.caption(" · ".join(_sub_bits))
    _links = []
    if org.get("org_website"):
        _w = org["org_website"]
        _links.append(f"[{_w}]({_w if _w.startswith('http') else 'https://' + _w})")
    if org.get("org_contact_email"):
        _links.append(f"✉ {org['org_contact_email']}")
    if _links:
        st.markdown(" · ".join(_links))

st.divider()


def _yn(v) -> str:
    s = str(v or "").strip().lower()
    if s in ("true", "yes"):
        return "Yes"
    if s in ("false", "no"):
        return "No"
    return "—"


def _money(v) -> str:
    try:
        return f"${float(v):,.0f}" if v not in (None, "", 0) else "—"
    except (TypeError, ValueError):
        return str(v)


def _kv(container, label, value):
    container.markdown(f"**{label}**  \n{value if value not in (None, '', []) else '—'}")


# ── Identity & eligibility gates ──────────────────────────────────────────────
with st.container(border=True):
    st.markdown("#### 🏷 Identity & eligibility gates")
    g = st.columns(4)
    _kv(g[0], "Legal type", prof.get("legal_type"))
    _kv(g[1], "Founding year", prof.get("founding_year"))
    _kv(g[2], "US-based entity", _yn(org.get("org_is_us_entity")))
    _kv(g[3], "Local board", (org.get("org_has_local_board") or "—").title())
    g2 = st.columns(4)
    _kv(g2[0], "BD / fundraising team", _yn(org.get("org_has_bd_team")))
    _kv(g2[1], "Grassroots / local NGO", _yn(org.get("org_is_grassroot")))
    _kv(g2[2], "Multi-country org", _yn(org.get("org_is_multi_country")))
    _kv(g2[3], "HQ country", org.get("org_hq_country"))

# ── Capacity & funding targets ────────────────────────────────────────────────
with st.container(border=True):
    st.markdown("#### 💰 Capacity & funding targets")
    c = st.columns(4)
    _kv(c[0], "Annual budget", _money(prof.get("annual_budget_usd")))
    _kv(c[1], "Largest grant managed", _money(prof.get("largest_grant_usd")))
    _kv(c[2], "Co-financing capacity", (prof.get("cofinancing_capacity") or "—").title())
    _kv(c[3], "Org stage", (prof.get("org_stage") or "—").title())
    c2 = st.columns(3)
    _kv(c2[0], "Funding target — low", _money(prof.get("funding_target_low")))
    _kv(c2[1], "Funding target — mid", _money(prof.get("funding_target_mid")))
    _kv(c2[2], "Funding target — max", _money(prof.get("funding_target_max")))
    st.caption("Funding-quality bands use geometric midpoints: ≤√(low·mid) Low · "
               "≤√(mid·max) Moderate · above High.")
    e = st.columns(3)
    _kv(e[0], "Independent entity (not INGO affiliate)", _yn(prof.get("org_is_independent_entity")))
    _kv(e[1], "Holds SAM.gov / UEI", _yn(prof.get("org_has_sam_uei")))
    _kv(e[2], "Tax-exempt", _yn(prof.get("org_tax_exempt")))


def _areas_block(title, ratings, selection, caption):
    bars = rating_bars_html(ratings)
    rmap = ratings if isinstance(ratings, dict) else {}
    ungraded = [a for a in (selection or []) if a not in rmap]
    if not bars and not ungraded:
        return
    st.markdown(f"**{title}**")
    st.caption(caption)
    if bars:
        st.markdown(bars, unsafe_allow_html=True)
    if ungraded:
        st.markdown(" ".join(
            "<span style='display:inline-block;background:#e6f2eb;color:#00703C;"
            "padding:2px 11px;border-radius:12px;margin:0 5px 7px 0;font-size:.85rem;'>"
            f"{_sub(a)}</span>" for a in ungraded), unsafe_allow_html=True)


# ── Domains (track record) & strategic priorities (graded) ────────────────────
with st.container(border=True):
    st.markdown("#### 🎯 Program areas (graded 0–5)")
    _areas_block("Domains / areas of expertise (track record → competitiveness)",
                 prof.get("domain_ratings"), prof.get("domains"),
                 "Where you have demonstrated experience.")
    st.markdown("")
    _areas_block("Strategic priority areas (strategy → strategic fit)",
                 prof.get("program_area_ratings"), prof.get("priority_areas"),
                 "Where your strategy says you want to work.")

# ── Geography & partners ──────────────────────────────────────────────────────
with st.container(border=True):
    st.markdown("#### 🌍 Geography & partners")
    _kv(st, "Countries of operation", ", ".join(prof.get("countries_of_operation") or []) or "—")
    _kv(st, "Countries registered", ", ".join(prof.get("countries_registered") or []) or "—")
    _partners = prof.get("partners") or []
    if _partners:
        st.markdown("**Partners (name · type · country)**")
        st.dataframe(
            pd.DataFrame(_partners).reindex(columns=["name", "type", "country"]).rename(
                columns={"name": "Partner", "type": "Type", "country": "Country"}),
            hide_index=True, width="stretch")
    for _lbl, _key in (("Trusted non-profit partners", "trusted_partners"),
                       ("Trusted for-profit partners", "trusted_for_profit_partners"),
                       ("Trusted academic institutions", "trusted_academic_institutions")):
        _v = prof.get(_key) or []
        if _v:
            _kv(st, _lbl, ", ".join(_v))

# ── Funder relationships & languages ──────────────────────────────────────────
with st.container(border=True):
    st.markdown("#### 🤝 Funders & languages")
    _kv(st, "Donors we've won grants/awards from", ", ".join(prof.get("funder_history") or []) or "—")
    _kv(st, "Donor registrations (portals)", ", ".join(prof.get("donor_registrations") or []) or "—")
    _kv(st, "Proposal languages", ", ".join(prof.get("proposal_languages") or []) or "—")

# ── Team ──────────────────────────────────────────────────────────────────────
_members = (getattr(settings, "get_team_members", lambda: None)() or [])
if _members:
    with st.container(border=True):
        st.markdown("#### 👥 Team members")
        st.markdown(" ".join(
            "<span style='display:inline-block;background:#eef2f6;color:#0f3d6e;"
            "padding:2px 11px;border-radius:12px;margin:0 5px 7px 0;font-size:.85rem;'>"
            f"{m}</span>" for m in _members), unsafe_allow_html=True)

if not can_edit:
    st.caption("To change any of the above, ask an app owner (admin / super-user).")
