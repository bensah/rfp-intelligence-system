"""Page 5 — Active Grants.

Tracks **active** grants only: those still under donor review or already
approved. Once a row's `donor_decision` is set to "Not Approved", it drops
out automatically.

Layout:
  1. KPI strip
  2. Per-grant detail (dropdown of Approved + Under Review only)
  3. By funder (paginated at 10 rows/page)

Source of truth: `rfp_submissions.donor_decision` (which mirrors the Excel
"Donor Decision Status" column). The `active_grants` table provides the
reporting status / type / due date / owner for the per-grant drilldown.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

# Must be the FIRST Streamlit call so a direct refresh of this page lands
# in wide layout instead of falling back to centered/portrait view.
st.set_page_config(
    page_title="Grants — RFPIS",
    page_icon="🛈",
    layout="wide",
    initial_sidebar_state="expanded",
)

from auth.authenticator import ensure_logged_in
from core.pipeline import usd_value
from db.supabase_client import get_client

if not ensure_logged_in():
    st.stop()

from core.app_header import render_app_header  # noqa: E402
render_app_header()

sb = get_client()
st.title("Active Grants")
st.caption(
    "Grants currently under donor review or already approved. "
    "**Not Approved** rows drop out automatically once their `donor_decision` "
    "is updated. RFPs still in the screening or proposal-development phase "
    "don't appear here."
)


# -----------------------------------------------------------------------------
# Data fetch — deduplicated RFPs only, ACTIVE statuses only
# -----------------------------------------------------------------------------
@st.cache_data(ttl=60)
def _fetch() -> tuple[pd.DataFrame, pd.DataFrame]:
    sbc = get_client()
    rfps = pd.DataFrame(
        sbc.table("rfp_submissions").select("*").eq("is_duplicate", False).execute().data or []
    )
    grants = pd.DataFrame(sbc.table("active_grants").select("*").execute().data or [])

    if not rfps.empty:
        dd = rfps["donor_decision"].fillna("").astype(str).str.strip().str.lower()
        # ACTIVE = Approved OR Under Review. Not Approved drops out.
        rfps["_active"] = dd.isin({"approved", "under review"})
        rfps["_approved"] = dd.eq("approved")
        rfps["_pending"] = dd.eq("under review")
        rfps["_usd_requested"] = rfps.apply(
            lambda r: usd_value(r.get("amount_requested"), r.get("currency")), axis=1
        )
        rfps["_usd_secured"] = rfps.apply(
            lambda r: usd_value(r.get("amount_secured"), r.get("currency_secured")), axis=1
        )
    return rfps, grants


rfps, grants = _fetch()
if rfps.empty:
    st.info("No RFPs in the database yet.")
    st.stop()

active = rfps[rfps["_active"]].copy()
if active.empty:
    st.info(
        "No active grants. A grant shows up here once its `donor_decision` is "
        "set to **Approved** or **Under Review**."
    )
    st.stop()


# -----------------------------------------------------------------------------
# KPIs
# -----------------------------------------------------------------------------
total_active = int(len(active))
approved = int(active["_approved"].sum())
pending = int(active["_pending"].sum())
total_requested = float(active["_usd_requested"].sum())
total_secured = float(active.loc[active["_approved"], "_usd_secured"].sum())
win_rate = (approved / total_active * 100) if total_active else 0

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Total Active", total_active)
k2.metric("Approved", approved)
k3.metric("Under Review", pending)
k4.metric("Total Requested (USD)", f"${total_requested:,.0f}")
k5.metric("Secured (USD)", f"${total_secured:,.0f}")
st.caption(f"Win rate: **{win_rate:.0f}%** (Approved ÷ Total Active)")
st.divider()


# -----------------------------------------------------------------------------
# Per-grant detail (Approved + Under Review only) — appears BEFORE By funder
# -----------------------------------------------------------------------------
st.subheader("Per-grant detail")

# Approved first, then Under Review; within each by submission deadline desc
priority = active.assign(
    _ord=active["_approved"].astype(int) * 2 + active["_pending"].astype(int)
).sort_values(["_ord", "submission_deadline"], ascending=[False, False])

# Title first (fully visible) — UID and decision as suffix so the title gets
# all the horizontal space. Iterate over priority.itertuples() and build the
# list explicitly (a dict comprehension would silently collapse rows whose
# generated label collides, which can happen if titles are duplicates).
option_pairs: list[tuple[str, str]] = []
for _, gr in priority.iterrows():
    label = (
        f"{gr.get('opportunity_title') or '(no title)'}  ·  "
        f"{gr['uid']}  ·  {gr.get('donor_decision') or '—'}"
    )
    option_pairs.append((label, gr["uid"]))
labels = [lbl for lbl, _ in option_pairs]
uid_by_label = {lbl: uid for lbl, uid in option_pairs}

# Explicit `key=` so Streamlit retains the user's pick across reruns
# (without it, a rerun triggered elsewhere on the page snaps the
# selectbox back to index 0).
pick = st.selectbox(
    "Pick an active grant", labels,
    key="grants_active_picker",
)
uid = uid_by_label[pick]
r = active[active["uid"] == uid].iloc[0].to_dict()

# Status badge
DD_COLOR = {"approved": "#dcf5e3", "under review": "#fff4cc"}
dd_key = (r.get("donor_decision") or "").lower()
bg = DD_COLOR.get(dd_key, "#eee")

h1, h2 = st.columns([4, 1])
h1.markdown(f"### {r.get('opportunity_title') or '(no title)'}")
h1.caption(f"UID `{r['uid']}` · Funder: **{r.get('funding_agency') or '—'}**")
h2.markdown(
    f"<div style='background:{bg};padding:14px 18px;border-radius:8px;"
    f"text-align:center;font-weight:600;font-size:1.05rem;margin-top:6px'>"
    f"{r.get('donor_decision') or '—'}</div>",
    unsafe_allow_html=True,
)

_geo = r.get("geographic_scope")
if isinstance(_geo, (list, tuple)):
    _geo_str = ", ".join(str(g) for g in _geo if g) or "—"
else:
    _geo_str = str(_geo) if _geo else "—"

# Lead / Sub Applicant display logic.
# Role drives the defaults (using the deploying-org short name from
# settings — defaults to "Org" if not configured):
#   * Prime     → the deploying org is the lead; no sub by default.
#   * Sub       → another institution leads; the deploying org is the sub.
#   * Technical → the deploying org provides TA, neither lead nor sub.
# Excel cells containing "N/A" / "NA" / blank are treated as empty.
def _placeholder(v) -> str:
    s = (v or "").strip()
    return "" if s.lower() in ("", "n/a", "na", "none", "—") else s

_role = (r.get("applicant_role") or "").strip()
_role_lc = _role.lower()
_raw_lead = _placeholder(r.get("lead_applicant"))
_raw_sub = _placeholder(r.get("sub_applicant"))

# Pull the deploying-org short name so this page works for any
# deployment without code changes.
from core.settings import get_org_short  # noqa: E402
_org = get_org_short()

if _role_lc == "prime":
    _lead_display = _raw_lead or _org
    _sub_display = _raw_sub or "—"
elif _role_lc == "sub":
    # The deploying org is sub; lead is someone else. TBD if Excel is
    # missing the lead.
    _lead_display = _raw_lead or "TBD"
    _sub_display = _raw_sub or _org
elif _role_lc == "technical":
    # The deploying org provides technical assistance only; doesn't
    # benefit from the grant.
    _lead_display = _raw_lead or "TBD"
    _sub_display = _raw_sub or "—"
else:
    _lead_display = _raw_lead or "—"
    _sub_display = _raw_sub or "—"

# Helper: filter pandas NaN, NaT, "nan" strings, None, etc. to a dash.
def _fmt(v) -> str:
    if v is None:
        return "—"
    try:
        if pd.isna(v):
            return "—"
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none", "<na>"):
        return "—"
    return s


# Donor Decision Date — actual award date if the donor has decided,
# otherwise the expected award date. NOT decision_date (that's the BDT
# review date which is something different).
_award = None
if not grants.empty:
    _glink = grants[grants["form_id_link"] == uid]
    if not _glink.empty:
        _award = _glink.iloc[0].get("award_date")
_donor_decision_date = _fmt(_award) if _fmt(_award) != "—" else _fmt(r.get("expected_award_date"))

# Date Submitted = "Date Completed" on the Excel Form1 sheet
# (= when the deploying org actually submitted the proposal).
_date_submitted = _fmt(r.get("date_completed"))

dc1, dc2, dc3, dc4 = st.columns(4)
dc1.markdown(f"**Role**  \n{_role or '—'}")
dc1.markdown(f"**Date Submitted**  \n{_date_submitted}")
dc2.markdown(f"**Lead Applicant**  \n{_lead_display}")
dc2.markdown(f"**Donor Decision Date**  \n{_donor_decision_date}")
dc3.markdown(f"**Sub Applicant**  \n{_sub_display}")
dc3.markdown(f"**Requested**  \n${(r.get('_usd_requested') or 0):,.0f} USD")
dc4.markdown(f"**Geographic Scope**  \n{_geo_str}")
dc4.markdown(f"**Secured**  \n${(r.get('_usd_secured') or 0):,.0f} USD")

# Linked active_grants row — for reporting status. If MULTIPLE rows share
# the same form_id_link (data-quality glitch from prior syncs), pick the
# most-recently-updated one and warn — that explains "the displayed status
# doesn't match what I edited" reports.
linked = grants[grants["form_id_link"] == uid] if not grants.empty else pd.DataFrame()
if len(linked) > 1:
    if "updated_at" in linked.columns:
        linked = linked.sort_values("updated_at", ascending=False)
    st.warning(
        f"⚠ {len(linked)} active_grants rows match this RFP (`form_id_link = {uid}`). "
        f"Displaying the most-recently-updated one (grant_id "
        f"`{linked.iloc[0].get('grant_id') or '?'}`). Clean up duplicates via "
        f"**Admin → Data → Active Grants**."
    )

if not linked.empty:
    g = linked.iloc[0].to_dict()
    st.markdown("**Reporting**")
    rep1, rep2, rep3, rep4, rep5 = st.columns(5)
    # Markdown rather than st.metric so long text values (e.g. "Not Started")
    # are not truncated to "Comp…" by the metric component's narrow width.
    rep1.markdown(f"**Grant ID**  \n{_fmt(g.get('grant_id'))}")
    rep2.markdown(f"**Report type**  \n{_fmt(g.get('report_type'))}")
    rep3.markdown(f"**Report status**  \n{_fmt(g.get('status'))}")
    rep4.markdown(f"**Due**  \n{_fmt(g.get('report_due_date'))}")
    due = pd.to_datetime(g.get("report_due_date"), errors="coerce")
    if pd.notna(due):
        delta = (due.date() - date.today()).days
        rep5.markdown(f"**Days to due**  \n{delta:+d}")
    else:
        rep5.markdown("**Days to due**  \n—")
    # Fall back to the rfp's date_completed when active_grants.submitted_date
    # is empty — they refer to the same event (when the deploying org submitted).
    submitted_date = _fmt(g.get("submitted_date"))
    if submitted_date == "—":
        submitted_date = _fmt(r.get("date_completed"))
    owner = _fmt(g.get("owner"))
    remarks = _fmt(g.get("remarks"))
    cap_extra = f" · Remarks: {remarks}" if remarks != "—" else ""
    st.caption(f"Submitted: **{submitted_date}** · Owner: **{owner}**{cap_extra}")
else:
    st.caption(
        "_No matching row in active_grants table yet — fill in Grant ID, Report Type, "
        "Report Due Date, Status, and Owner via the Excel `Active_Grants_Log` sheet "
        "or via the Admin tools._"
    )

# Recent meeting notes
notes = (
    sb.table("meeting_logs")
    .select("meeting_date,actions,owner,deadline")
    .eq("rfp_uid", uid)
    .order("meeting_date", desc=True)
    .limit(5)
    .execute()
    .data
)
if notes:
    st.markdown("**Recent meeting notes**")
    for n in notes:
        st.markdown(
            f"- **{n.get('meeting_date')}** — _{n.get('actions') or '(no action)'}_ "
            f"(owner: {n.get('owner') or '—'}, due: {n.get('deadline') or '—'})"
        )

st.divider()


# -----------------------------------------------------------------------------
# By funder (paginated at 10 rows / page)
# -----------------------------------------------------------------------------
st.subheader("By funder")
st.caption(
    "**Submissions** sums the per-RFP `submissions` column (an RFP can have "
    "multiple donor-side submissions). **RFPs** is the distinct count of "
    "active RFPs. Requested is the sum of `amount_requested`; Secured is the "
    "sum of `amount_secured` from approved rows only."
)

# Submissions sums the per-row Submissions column (one RFP can have multiple
# donor-side submissions — captured in rfp_submissions.submissions). RFPs is
# the distinct count of unique RFP rows.
active["_submissions_int"] = active["submissions"].fillna(1).astype(int)

by_funder = (
    active.groupby("funding_agency")
    .agg(
        RFPs=("uid", "count"),
        Submissions=("_submissions_int", "sum"),
        Approved=("_approved", "sum"),
        Pending=("_pending", "sum"),
        Requested=("_usd_requested", "sum"),
        Secured=("_usd_secured", lambda s: float(s[active.loc[s.index, "_approved"]].sum())),
    )
    .reset_index()
    .rename(columns={"funding_agency": "Funder"})
    .sort_values("Submissions", ascending=False)
)

# Pagination — 10 rows/page
PAGE_SIZE = 10
n_rows = len(by_funder)
n_pages = max(1, (n_rows + PAGE_SIZE - 1) // PAGE_SIZE)
page_key = "by_funder_page"
cur_page = max(1, min(st.session_state.get(page_key, 1), n_pages))

pc1, pc2, pc3 = st.columns([1, 3, 1])
if pc1.button("◀ Prev", disabled=cur_page == 1, key="funder_prev"):
    st.session_state[page_key] = cur_page - 1
    st.rerun()
pc2.markdown(
    f"<div style='text-align:center;padding-top:6px;color:#555'>"
    f"Page <b>{cur_page}</b> of <b>{n_pages}</b> · {n_rows} funder{'s' if n_rows != 1 else ''}</div>",
    unsafe_allow_html=True,
)
if pc3.button("Next ▶", disabled=cur_page >= n_pages, key="funder_next"):
    st.session_state[page_key] = cur_page + 1
    st.rerun()

start = (cur_page - 1) * PAGE_SIZE
page_df = by_funder.iloc[start:start + PAGE_SIZE]
st.dataframe(
    page_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "RFPs":        st.column_config.NumberColumn(
            "RFPs", format="%d", help="Distinct RFPs submitted to this funder"
        ),
        "Submissions": st.column_config.NumberColumn(
            "Submissions", format="%d",
            help="Sum of submission events (RFPs × Submissions per RFP)"
        ),
        "Approved":    st.column_config.NumberColumn("Approved", format="%d"),
        "Pending":     st.column_config.NumberColumn("Pending",  format="%d"),
        "Requested":   st.column_config.NumberColumn("Requested (USD)", format="$%.0f"),
        "Secured":     st.column_config.NumberColumn("Secured (USD)",   format="$%.0f"),
    },
)

# Diagnostic: show per-row submissions values so we can verify what's in the DB
with st.expander("🔍 Diagnostic — per-row submissions values", expanded=False):
    diag = active[["uid", "opportunity_title", "funding_agency",
                    "donor_decision", "submissions", "_submissions_int"]].copy()
    diag.columns = ["UID", "Title", "Funder", "Donor Decision",
                    "submissions (raw)", "submissions (used for sum)"]
    st.dataframe(diag, use_container_width=True, hide_index=True)
    st.caption(
        f"Sum check: total submissions across all active rows = "
        f"**{int(active['_submissions_int'].sum())}**"
    )
