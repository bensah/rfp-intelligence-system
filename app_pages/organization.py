"""Entity Details — a full, READ-ONLY view of an entity (organization OR individual).

Everyone can view; editing is admin / super-user only. The full edit form lives in
Settings → Setup (Profile · Bid Fitness · Team · Scan Preferences); the "✏️ Edit entity"
button opens that same form in an overlay. A live opportunity right-rail (the same one on
the Pipeline page) sits beside the details.

"Entity" is deliberately universal: a tenant may be an organization or a single individual
(migration 078), so the page avoids org-only language.

Auth + the global header already ran in App.py; this file is content-only.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core import org_profile as _orgp
from core import permissions, settings
from core.program_area_classifier import category_full as _cat, subarea_label as _sub
from core.program_area_select import rating_bars_html
from db.supabase_client import get_client

user = st.session_state["app_user"]
can_edit = permissions.is_admin(user)          # admin OR super_user only
_is_super = permissions.is_super_user(user)

# Which entity are we showing? A super_user in 'view-as' mode (su_view_tenant, set by a
# Settings → Tenants link and shown by the global header banner) sees THAT tenant;
# everyone else — and a super_user in their own account — sees their own (view_tid None →
# own/session tenant). The view-as is resolved centrally in core.app_header.
view_tid = st.session_state.get("su_view_tenant") if _is_super else None

org = settings.get_org(view_tid)
prof = _orgp.get_profile(view_tid)


@st.dialog("Edit entity", width="large")
def _edit_entity_dialog() -> None:
    """In-place overlay reusing the EXACT Settings → Setup form (one source). Edits the
    entity currently being viewed (view_tid), so a super_user editing a view-as tenant
    writes to THAT tenant."""
    from views.org_setup import render_org_setup
    render_org_setup(user, get_client(), tenant_id=view_tid)


# ── Value-formatting helpers (module-level so they're defined before the layout) ──
def _yn(v) -> str:
    # NB: handle real booleans before the `or ""` trap — `False or ""` is "",
    # which would wrongly read as "—" instead of "No".
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if v is None or v == "":
        return "—"
    s = str(v).strip().lower()
    if s in ("true", "yes"):
        return "Yes"
    if s in ("false", "no"):
        return "No"
    return "—"


def _present(v) -> str:
    if isinstance(v, bool):
        return "Present" if v else "Absent"
    if v is None or v == "":
        return "—"
    s = str(v).strip().lower()
    if s in ("true", "yes"):
        return "Present"
    if s in ("false", "no"):
        return "Absent"
    return "—"


def _money(v) -> str:
    try:
        return f"${float(v):,.0f}" if v not in (None, "", 0) else "—"
    except (TypeError, ValueError):
        return str(v)


def _kv(container, label, value):
    v = value if value not in (None, "", []) else "—"
    container.markdown(
        f"<div class='org-field'><div class='lbl'>{label}</div>"
        f"<div class='val'>{v}</div></div>",
        unsafe_allow_html=True,
    )


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


# Card styling — muted uppercase labels over prominent values, green card heads.
st.markdown(
    """
    <style>
    .org-field { margin: 0 0 0.95rem 0; }
    .org-field .lbl { font-size:.70rem; letter-spacing:.045em; text-transform:uppercase;
                      color:#64748b; font-weight:700; margin:0 0 1px 0; }
    .org-field .val { font-size:1.02rem; color:#0f172a; font-weight:500; line-height:1.3; }
    div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:12px; }
    div[data-testid="stVerticalBlockBorderWrapper"] h4 { margin:.1rem 0 .6rem 0; color:#00703C; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Page body (left/main) + the live opportunity right-rail.
_main, _rail = st.columns([3.4, 1], gap="medium")

with _rail:
    from views.opportunity_rail import render_opportunity_rail
    render_opportunity_rail()

with _main:
    # ── Header ────────────────────────────────────────────────────────────
    _hl, _hr = st.columns([5, 1.4])
    _hl.title("Entity Details")
    if can_edit:
        if _hr.button("✏️ Edit entity", width="stretch", type="primary"):
            _edit_entity_dialog()
    else:
        _hr.caption("View only — editing is restricted to app owners.")

    _logo_bytes, _ = settings.get_org_logo(view_tid)
    _top = st.columns([1, 5])
    if _logo_bytes:
        try:
            _top[0].image(_logo_bytes, width=120)
        except Exception:
            pass
    with _top[1]:
        st.subheader(org.get("org_name") or "This entity")
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

    # ── Identity & eligibility gates ──────────────────────────────────────
    with st.container(border=True):
        st.markdown("#### 🏷 Identity & eligibility")
        g = st.columns(4)
        _kv(g[0], "Legally registered as", _orgp.legal_type_label(prof.get("org_legal_type")))
        _kv(g[1], "Founded in the year", prof.get("org_founding_year"))
        _kv(g[2], "Located in", org.get("org_country"))
        _kv(g[3], "Local board", _present(org.get("org_has_local_board")))
        g2 = st.columns(4)
        _kv(g2[0], "BD / fundraising team", _yn(org.get("org_has_bd_team")))
        _kv(g2[1], "Grassroots / local NGO", _yn(org.get("org_is_grassroot")))
        _kv(g2[2], "Multi-country", _yn(org.get("org_is_multi_country")))
        _kv(g2[3], "HQ country", org.get("org_hq_country"))

    # ── Capacity & funding targets ────────────────────────────────────────
    with st.container(border=True):
        st.markdown("#### 💰 Capacity & funding targets",
                    help="Funding-quality bands use geometric midpoints: ≤√(low·mid) Low · "
                         "≤√(mid·max) Moderate · above High.")
        c = st.columns(4)
        _kv(c[0], "Annual budget", _money(prof.get("org_annual_budget")))
        _kv(c[1], "Largest grant managed", _money(prof.get("org_largest_grant")))
        _kv(c[2], "Co-financing capacity", (prof.get("org_cofinancing_capacity") or "—").title())
        _kv(c[3], "Stage", (prof.get("org_stage") or "—").title())
        c2 = st.columns(4)
        _kv(c2[0], "Funding target — low", _money(prof.get("org_min_target")))
        _kv(c2[1], "Funding target — mid", _money(prof.get("org_mid_target")))
        _kv(c2[2], "Funding target — max", _money(prof.get("org_max_target")))
        e = st.columns(4)
        _kv(e[0], "Independent entity (not INGO affiliate)", _yn(prof.get("org_is_independent_entity")))
        _kv(e[1], "Holds SAM.gov / UEI", _yn(prof.get("org_has_sam_uei")))
        _kv(e[2], "Tax-exempt", _yn(prof.get("org_tax_exempt")))

    # ── Domains (track record) & strategic priorities (graded) ────────────
    with st.container(border=True):
        st.markdown("#### 🎯 Program areas (graded 0–5)")
        _areas_block("Domains / areas of expertise (track record → competitiveness)",
                     prof.get("org_domain_ratings"), prof.get("org_domain_expertise"),
                     "Where you have demonstrated experience.")
        st.markdown("")
        _areas_block("Strategic priority areas (strategy → strategic fit)",
                     prof.get("org_priority_ratings"), prof.get("org_priority_areas"),
                     "Where your strategy says you want to work.")

    # ── Geography & partners ──────────────────────────────────────────────
    with st.container(border=True):
        st.markdown("#### 🌍 Geography & partners")
        _gc = st.columns(2)
        _kv(_gc[0], "Countries registered", ", ".join(prof.get("org_registered_countries") or []) or "—")
        _kv(_gc[1], "Countries of operation", ", ".join(prof.get("org_operating_countries") or []) or "—")
        _partners = prof.get("partners") or []
        if _partners:
            st.markdown("**Affiliated partners & collaborators**")
            st.dataframe(
                pd.DataFrame(_partners).reindex(columns=["name", "type", "country"]).rename(
                    columns={"name": "Partner", "type": "Type", "country": "Country"}),
                hide_index=True, width="stretch")

    # ── Funder relationships & languages ──────────────────────────────────
    with st.container(border=True):
        st.markdown("#### 🤝 Funders & languages")
        _kv(st, "Donors we've won grants/awards from", ", ".join(prof.get("org_funder_history") or []) or "—")
        _kv(st, "Donor portal registration active", ", ".join(prof.get("org_donor_registrations") or []) or "—")
        _kv(st, "Proposal languages", ", ".join(prof.get("proposal_languages") or []) or "—")

    # ── Team ──────────────────────────────────────────────────────────────
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
