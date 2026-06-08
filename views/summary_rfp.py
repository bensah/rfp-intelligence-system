"""Page 4 — Highlights (Year-to-Date Summary Dashboard).

Mirrors the Excel `Year-to-date RFP Summary Dashboard` sheet with sections:
  A. RFPs Snapshot              — counts + Pipeline highlights
  B. Pipeline Health            — Stage / Probability / $ by Tier + Donor concentration
  C. Proposal Development Status — 3 tables + validation alerts
  D. Periodic Reflection         — Weekly / Monthly / Quarterly + Annual reflection
  E. Partner Engagement KRs      — KR2.2 / KR2.3 / KR2.4 cards

All deduplicated. Year sourced from app_settings (Admin > Settings).
Week / Month / Quarter selectable via dropdowns at the top of Section D.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from core import settings
from core.currency import format_money
from core.pipeline import (
    PROB_LABEL_HIGH, PROB_LABEL_MED, PROB_LABEL_LOW,
    days_to_deadline, deadline_status, prob_tier, usd_value,
)
from core.review_week import all_weeks_for_year, monday_from_week_label
from db.supabase_client import get_client

# auth handled by wrapper page
sb = get_client()

year = settings.get_year()
today = date.today()
st.markdown(
    f"<h2 style='font-size:1.55rem;font-weight:700;color:#334155;"
    f"margin:0.15rem 0 0.5rem;'>YTD Summary ({year})</h2>",
    unsafe_allow_html=True,
)
st.caption(
    f"Review period: **1 Jan – {today.strftime('%d %b').lstrip('0')} {year}**. "
    f"Year sourced from app_settings (change in Admin > Settings)."
)

# Centre non-first column headers + values in every st.dataframe on this page.
st.markdown(
    """
    <style>
      div[data-testid="stDataFrame"] thead tr th:not(:first-child) > div,
      div[data-testid="stDataFrame"] tbody tr td:not(:first-child) {
        text-align: center !important;
        justify-content: center !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Defensive helpers — used everywhere date / string conversions happen
# -----------------------------------------------------------------------------
def _safe_date(v):
    """Always returns a python date or None. Never NaT, Timestamp, or np.datetime64."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, date) and not isinstance(v, pd.Timestamp):
        return v
    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date()
    except Exception:
        return None


def _safe_str(v) -> str:
    """NaN-safe string. `x or ""` is unsafe because NaN is truthy."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v)


def _is_past(d):
    d = _safe_date(d)
    return d is not None and d < today


def _in_range(d, start: date, end: date) -> bool:
    d = _safe_date(d)
    return d is not None and start <= d <= end


def _kpi(label: str, value, helper: str | None = None) -> None:
    """Stacked KPI rendered as a bordered card.

    Card framing matches the global metric-tile styling in
    core/app_header._GLOBAL_CSS (green left-border, soft bg, padding)
    so the snapshot row reads as a uniform card grid rather than
    free-floating numbers.
    """
    parts = [
        f"<div style='font-size:1.65rem;font-weight:700;color:#005a30;line-height:1.1;'>"
        f"{value}</div>",
        f"<div style='font-size:0.85rem;color:#475569;font-weight:500;margin-top:2px;'>"
        f"{label}</div>",
    ]
    if helper:
        parts.append(
            f"<div style='font-size:0.75rem;color:#94a3b8;margin-top:2px;'>{helper}</div>"
        )
    st.markdown(
        "<div style='background:#fafcfa;border:1px solid #e3e7e3;"
        "border-left:4px solid #00703C;border-radius:6px;"
        "padding:12px 14px;height:100%;'>"
        + "".join(parts) +
        "</div>",
        unsafe_allow_html=True,
    )


# -----------------------------------------------------------------------------
# Data fetch
# -----------------------------------------------------------------------------
@st.cache_data(ttl=30)
def _fetch(year: int):
    sbc = get_client()
    rfps = pd.DataFrame(sbc.table("rfp_submissions").select("*").execute().data or [])
    grants = pd.DataFrame(sbc.table("active_grants").select("*").execute().data or [])
    engagements = pd.DataFrame(sbc.table("engagement_logs").select("*").execute().data or [])
    narratives = pd.DataFrame(sbc.table("narrative_logs").select("*").execute().data or [])
    meetings = pd.DataFrame(sbc.table("meeting_logs").select("*").execute().data or [])

    if not rfps.empty:
        rfps["_submitted_at"] = pd.to_datetime(rfps["submitted_at"], errors="coerce", format="ISO8601")
        rfps = rfps[rfps["_submitted_at"].dt.year == year].copy()
        rfps["_dtd"] = rfps["submission_deadline"].apply(days_to_deadline)
        rfps["_dstat"] = rfps["submission_deadline"].apply(deadline_status)
        rfps["_usd"] = rfps.apply(
            lambda r: usd_value(r.get("estimated_value"), r.get("currency")), axis=1
        )
        rfps["_secured_usd"] = rfps.apply(
            lambda r: usd_value(r.get("amount_secured"), r.get("currency_secured")), axis=1
        )

        # Amount Requested falls back to Estimated Value when blank,
        # because the team always has at least the donor-published amount.
        def _req_amt(r):
            amt = r.get("amount_requested")
            try:
                if amt is None or pd.isna(amt):
                    amt = r.get("estimated_value")
            except (TypeError, ValueError):
                amt = r.get("estimated_value")
            return amt
        rfps["_requested_usd"] = rfps.apply(
            lambda r: usd_value(_req_amt(r), r.get("currency")), axis=1
        )

        rfps["_date_completed"] = rfps["date_completed"].apply(_safe_date)
        rfps["_date_approval"] = rfps["date_of_approval"].apply(_safe_date)
        rfps["_search_date"] = rfps["search_date"].apply(_safe_date)
        rfps["_deadline_date"] = rfps["submission_deadline"].apply(_safe_date)
        # Submissions count (defaults to 1 if column missing or null)
        if "submissions" in rfps.columns:
            rfps["_submissions"] = rfps["submissions"].fillna(1).astype(int)
        else:
            rfps["_submissions"] = 1
    return rfps, grants, engagements, narratives, meetings


rfps, grants, engagements, narratives, meetings = _fetch(year)
if rfps.empty:
    st.info(f"No RFPs in {year} yet.")
    st.stop()

unique = rfps[~rfps["is_duplicate"].fillna(False)].copy()
dup_count = int(rfps["is_duplicate"].fillna(False).sum())
dec_lower = unique["decision"].fillna("").str.lower()
proceed_df = unique[dec_lower.str.startswith("proceed").to_numpy()].copy()


# =============================================================================
# A. RFPs Snapshot  (was A. Screening snapshot + B. Pipeline highlights)
# =============================================================================
st.subheader("A · RFPs snapshot")
proceed = int(dec_lower.str.startswith("proceed").sum())
park = int(dec_lower.eq("park").sum())
decline = int(dec_lower.eq("decline").sum())

# Row 1 — counts
r1c = st.columns(6)
for col, (label, val) in zip(r1c, [
    ("Total RFPs Found", len(rfps)),
    ("Total Unique RFPs", len(unique)),
    ("Proceed", proceed),
    ("Parked", park),
    ("Declined", decline),
    ("Duplicate Flagged", dup_count),
]):
    with col:
        _kpi(label, val)

# Row 2 — role mix (centered, slightly lower; 3 cards across the middle)
role_lower = unique["applicant_role"].fillna("").str.lower()
prime = int(role_lower.eq("prime").sum())
sub = int(role_lower.eq("sub").sum())
ta = int(role_lower.eq("technical").sum())
proc_role_lower = proceed_df["applicant_role"].fillna("").str.lower()
proc_prime = int(proc_role_lower.eq("prime").sum())
proc_sub = int(proc_role_lower.eq("sub").sum())
proc_ta = int(proc_role_lower.eq("technical").sum())

st.write("")  # vertical spacer
# Three even cards (was [1,2,2,2,1] with empty side cells, which wrapped into
# a lopsided layout with blank gaps on mobile).
rc1, rc2, rc3 = st.columns(3)
with rc1:
    _kpi("Prime Opportunities", f"{proc_prime} of {prime}", "Proceed as prime applicant")
with rc2:
    _kpi("Sub Opportunities", f"{proc_sub} of {sub}", "Proceed, applying as Sub")
with rc3:
    _kpi("TA Provider", f"{proc_ta} of {ta}", "Technical assistance only")

st.markdown("---")

# --- Pipeline highlights (merged in) ---
pipeline_value = float(proceed_df["_usd"].sum()) if not proceed_df.empty else 0.0
bc1, bc2, bc3 = st.columns([2, 2, 1])

with bc1:
    st.markdown("**Largest Opportunities (Top 3 — Proceed)**")
    if proceed_df.empty:
        st.caption("_No Proceed RFPs._")
    else:
        top3 = proceed_df.nlargest(3, "_usd")
        for i, (_, r) in enumerate(top3.iterrows(), 1):
            st.markdown(
                f"{i}. **{format_money(r.get('estimated_value'), r.get('currency'))}** — "
                f"{(r.get('opportunity_title') or '')[:80]}"
            )

with bc2:
    st.markdown("**Closest Deadlines (< 14 days — Proceed)**")
    if proceed_df.empty:
        st.caption("_No Proceed RFPs._")
    else:
        soon = proceed_df[(proceed_df["_dtd"].notna()) & (proceed_df["_dtd"] >= 0)
                          & (proceed_df["_dtd"] <= 14)].sort_values("_dtd")
        if soon.empty:
            st.caption("_No deadlines within 14 days._")
        else:
            for _, r in soon.iterrows():
                st.markdown(
                    f"- **{r['submission_deadline']}** ({int(r['_dtd'])}d) — "
                    f"{(r.get('opportunity_title') or '')[:70]}"
                )

with bc3:
    # Label on top (like adjacent column headers), value below
    st.markdown("**Total Pipeline Value (Proceed Only)**")
    st.markdown(
        f"<div style='font-size:1.65rem;font-weight:600;color:#00703C;"
        f"line-height:1.4;margin-top:6px'>${pipeline_value:,.0f} USD</div>",
        unsafe_allow_html=True,
    )

st.divider()


# =============================================================================
# B. Pipeline Health  (was C. Pipeline health + D. Donor concentration)
# =============================================================================
st.subheader("B · Pipeline health")


def _tier_of(score):
    """Probability Tier from core.pipeline.prob_tier — single source of truth.
    Thresholds: High >75 · Medium 60-75 · Low <60."""
    return prob_tier(score)


def _hbar(values: list[float], labels: list[str], height: int = 220, fmt: str | None = None,
          inside: bool = False) -> None:
    fig = px.bar(
        x=values, y=labels, orientation="h",
        color_discrete_sequence=["#00703C"], height=height,
    )
    fig.update_layout(
        margin=dict(t=10, b=10, l=10, r=10),
        showlegend=False, xaxis_title=None, yaxis_title=None,
    )
    if fmt:
        if inside:
            # Place values INSIDE the bar so long values stay visible at the
            # right edge. White text on the green bar gives strong contrast.
            fig.update_traces(
                texttemplate=fmt,
                textposition="inside",
                insidetextanchor="end",
                textfont=dict(color="white", size=13),
            )
        else:
            fig.update_traces(texttemplate=fmt, textposition="outside")
    st.plotly_chart(fig, use_container_width=True)


cc1, cc2, cc3 = st.columns(3)

with cc1:
    st.markdown("**Stage**")
    if not proceed_df.empty:
        stage_counts = proceed_df["stage"].fillna("Unspecified").value_counts()
        _hbar(stage_counts.values.tolist(), stage_counts.index.tolist())
    else:
        st.caption("_No Proceed RFPs._")

_tier_order = [PROB_LABEL_HIGH, PROB_LABEL_MED, PROB_LABEL_LOW]

with cc2:
    st.markdown("**Probability Tier**")
    if not proceed_df.empty:
        proceed_df["_tier"] = proceed_df["alignment_score"].apply(_tier_of)
        tier_counts = (
            proceed_df["_tier"].dropna()
            .value_counts()
            .reindex(_tier_order, fill_value=0)
        )
        _hbar(tier_counts.values.tolist(), tier_counts.index.tolist())
        # Sanity caption — same logic as Data page Prob column
        st.caption(
            f"_{len(proceed_df)} Proceed RFPs in scope (after dedup): "
            f"{int(tier_counts.get(PROB_LABEL_HIGH, 0))} High · "
            f"{int(tier_counts.get(PROB_LABEL_MED, 0))} Medium · "
            f"{int(tier_counts.get(PROB_LABEL_LOW, 0))} Low_"
        )
        # Show the per-RFP scores so it's obvious where each row lands
        with st.expander("See per-RFP score → tier mapping", expanded=False):
            tier_df = proceed_df[["uid", "opportunity_title", "alignment_score", "_tier"]].copy()
            tier_df["alignment_score"] = tier_df["alignment_score"].fillna(0).round(1)
            tier_df = tier_df.sort_values("alignment_score", ascending=False).rename(columns={
                "uid": "UID",
                "opportunity_title": "Title",
                "alignment_score": "Score",
                "_tier": "Tier",
            })
            st.dataframe(tier_df, hide_index=True, use_container_width=True)
    else:
        st.caption("_No Proceed RFPs._")

with cc3:
    st.markdown("**Pipeline Value by Tier (USD)**")
    if not proceed_df.empty:
        if "_tier" not in proceed_df.columns:
            proceed_df["_tier"] = proceed_df["alignment_score"].apply(_tier_of)
        tier_value = (
            proceed_df.dropna(subset=["_tier"]).groupby("_tier")["_usd"].sum()
            .reindex(_tier_order, fill_value=0)
        )
        _hbar(tier_value.values.tolist(), tier_value.index.tolist(),
              fmt="$%{x:,.0f}", inside=True)
    else:
        st.caption("_No Proceed RFPs._")

st.divider()


# --- Donor concentration (merged into Pipeline Health) ---
st.markdown("**Donor concentration**")
if not proceed_df.empty:
    by_funder = (
        proceed_df.groupby("funding_agency")
        .agg(RFPs=("uid", "count"), Pipeline=("_usd", "sum"))
        .reset_index()
        .rename(columns={"funding_agency": "Funder", "Pipeline": "Pipeline Value (USD)"})
        .sort_values("Pipeline Value (USD)", ascending=False)
    )
    total_pl = max(float(by_funder["Pipeline Value (USD)"].sum()), 1.0)
    by_funder["% Share"] = (by_funder["Pipeline Value (USD)"] / total_pl * 100).round(1)
    top3_share = by_funder.head(3)["% Share"].sum()
    st.caption(f"Top 3 funders hold **{top3_share:.0f}%** of Proceed pipeline value.")

    # Pagination — max 10 rows per page
    page_size = 10
    n_rows = len(by_funder)
    n_pages = max(1, (n_rows + page_size - 1) // page_size)
    pg_key = "donor_conc_page"
    cur_page = st.session_state.get(pg_key, 1)
    cur_page = max(1, min(cur_page, n_pages))

    pcol1, pcol2, pcol3 = st.columns([1, 3, 1])
    if pcol1.button("◀ Prev", disabled=cur_page == 1, key="donor_prev"):
        st.session_state[pg_key] = cur_page - 1
        st.rerun()
    pcol2.markdown(
        f"<div style='text-align:center;padding-top:6px;color:#555'>"
        f"Page <b>{cur_page}</b> of <b>{n_pages}</b> · {n_rows} funders</div>",
        unsafe_allow_html=True,
    )
    if pcol3.button("Next ▶", disabled=cur_page >= n_pages, key="donor_next"):
        st.session_state[pg_key] = cur_page + 1
        st.rerun()

    start = (cur_page - 1) * page_size
    page_df = by_funder.iloc[start:start + page_size]
    st.dataframe(
        page_df, use_container_width=True, hide_index=True,
        column_config={
            "Pipeline Value (USD)": st.column_config.NumberColumn(
                "Pipeline Value (USD)", format="$%,.0f"
            ),
            "% Share": st.column_config.NumberColumn("% Share", format="%.1f%%"),
        },
    )
else:
    st.info("No Proceed RFPs to break down.")

st.divider()


# =============================================================================
# C. Proposal Development Status  (was E. Status & urgency)
# =============================================================================
st.subheader("C · Proposal development status")

# Validation alerts — donor_decision vs progress_status consistency
SUBMITTED_DECISIONS = {"approved", "under review", "not approved"}
# Pre-submit progress states (never submitted to donor):
#   Not Started, In Progress       — still being worked on
#   Discontinued                   — team chose to drop
#   Missed                         — deadline lapsed without submission
PRE_SUBMIT_PROGRESS = {"", "not started", "in progress", "discontinued",
                       "missed", "missing"}
POST_SUBMIT_PROGRESS = {"completed"}  # "Completed" = submitted to donor

alerts: list[tuple[str, str]] = []
for _, r in unique.iterrows():
    dd_raw = _safe_str(r.get("donor_decision")).strip()
    dd = dd_raw.lower() or "not submitted"
    ps_raw = _safe_str(r.get("progress_status")).strip()
    ps = ps_raw.lower()
    if dd == "not submitted" and ps not in PRE_SUBMIT_PROGRESS:
        alerts.append((r["uid"], f"donor_decision = 'Not submitted' but progress = '{ps_raw or '(blank)'}'"))
    elif dd in SUBMITTED_DECISIONS and ps not in POST_SUBMIT_PROGRESS:
        alerts.append((r["uid"], f"donor_decision = '{dd_raw}' but progress = '{ps_raw or '(blank)'}' (expected Completed)"))

if alerts:
    with st.expander(f"⚠ {len(alerts)} data-validation alert(s) — donor_decision ↔ progress_status mismatch"):
        for uid, msg in alerts:
            st.markdown(f"- `{uid}` — {msg}")

ec1, ec2, ec3 = st.columns(3)

_PS_CANONICAL = {
    "not started":  "Not Started",
    "in progress":  "In Progress",
    "completed":    "Completed",
    "discontinued": "Discontinued",
    "missed":       "Missed",
    "missing":      "Missed",   # tolerate legacy DB values
}


def _canon_ps(v) -> str:
    s = _safe_str(v).strip().lower()
    return _PS_CANONICAL.get(s, "Not Started")


def _is_missed(v) -> bool:
    return _safe_str(v).strip().lower() in ("missing", "missed")


with ec1:
    st.markdown("**Progress Status**")
    if not proceed_df.empty:
        ps_order = ["Not Started", "In Progress", "Completed", "Discontinued", "Missed"]
        ps_norm = proceed_df["progress_status"].apply(_canon_ps)
        ps = (
            ps_norm.value_counts()
            .reindex(ps_order, fill_value=0)
            .reset_index()
        )
        ps.columns = ["Status", "Count"]
        st.dataframe(ps, hide_index=True, use_container_width=True)
    else:
        st.caption("—")

with ec2:
    st.markdown("**Action Urgency**")
    if not proceed_df.empty:
        def _urgency_bucket(row):
            ps = _safe_str(row.get("progress_status")).strip().lower()
            # Only Discontinued is excluded — intentional drop, no urgency tracking
            if ps == "discontinued":
                return None
            # Missed = past-deadline unintentional miss → Overdue
            if ps in ("missed", "missing"):
                return "Overdue"
            dtd = row.get("_dtd")
            if dtd is None:
                return "On Track (>14d)"
            try:
                if pd.isna(dtd):
                    return "On Track (>14d)"
                d = int(dtd)
            except (TypeError, ValueError):
                return "On Track (>14d)"
            if d < 0:
                return "Overdue"
            if d < 7:
                return "At Risk (<7d)"
            if d <= 14:
                return "Due Soon (<14d)"
            return "On Track (>14d)"
        urgency_series = proceed_df.apply(_urgency_bucket, axis=1).dropna()
        urg = urgency_series.value_counts().reindex(
            ["Overdue", "At Risk (<7d)", "Due Soon (<14d)", "On Track (>14d)"],
            fill_value=0,
        ).reset_index()
        urg.columns = ["Urgency", "Count"]
        st.dataframe(urg, hide_index=True, use_container_width=True)
    else:
        st.caption("—")

with ec3:
    st.markdown("**Donor Decision Status**")
    # Case-normalise stored values to the canonical dropdown labels
    _DD_CANONICAL = {
        "approved":       "Approved",
        "under review":   "Under Review",
        "not approved":   "Not Approved",
        "not submitted":  "Not submitted",
        "":               "Not submitted",
    }
    def _canon_dd(v):
        s = _safe_str(v).strip().lower()
        return _DD_CANONICAL.get(s, "Not submitted")

    dd_counts = (
        unique["donor_decision"].apply(_canon_dd)
        .value_counts()
        .reindex(["Approved", "Under Review", "Not Approved", "Not submitted"], fill_value=0)
        .reset_index()
    )
    dd_counts.columns = ["Decision", "Count"]
    st.dataframe(dd_counts, hide_index=True, use_container_width=True)

st.divider()


# =============================================================================
# D. Periodic Reflection  (was F. Review cadence + G. Annual reflection)
# =============================================================================
st.subheader(f"D · Periodic Reflection — {year}")

# --- Period selectors: Week / Month / Quarter aligned column-by-column with pulses ---
MONTHS = ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"]
QUARTERS = ["Q1", "Q2", "Q3", "Q4"]

all_weeks = all_weeks_for_year(year)
today_mon = today - timedelta(days=today.weekday())
default_week_label = next(
    (w for w in all_weeks if monday_from_week_label(w, year) == today_mon),
    all_weeks[0],
)

default_month_idx = today.month - 1 if today.year == year else 0
default_q_idx = ((today.month - 1) // 3) if today.year == year else 0

# Three columns: subheader (placeholder, filled after selection) on top,
# dropdown directly under (with collapsed label), then bullets below.
pcols = st.columns(3)
hdr_week = pcols[0].empty()
hdr_month = pcols[1].empty()
hdr_quarter = pcols[2].empty()

with pcols[0]:
    sel_week_label = st.selectbox(
        "Week", all_weeks, index=all_weeks.index(default_week_label),
        key="hl_week", label_visibility="collapsed",
    )
with pcols[1]:
    sel_month_name = st.selectbox(
        "Month", MONTHS, index=default_month_idx,
        key="hl_month", label_visibility="collapsed",
    )
with pcols[2]:
    sel_q_label = st.selectbox(
        "Quarter", QUARTERS, index=default_q_idx,
        key="hl_quarter", label_visibility="collapsed",
    )

# Compute period bounds from picks
sel_week_start = monday_from_week_label(sel_week_label, year)
sel_week_end = sel_week_start + timedelta(days=6)
sel_month_num = MONTHS.index(sel_month_name) + 1
sel_month_start = date(year, sel_month_num, 1)
sel_month_end = (date(year, 12, 31) if sel_month_num == 12
                 else date(year, sel_month_num + 1, 1) - timedelta(days=1))
sel_q_num = int(sel_q_label[1])
sel_q_start = date(year, (sel_q_num - 1) * 3 + 1, 1)
sel_q_end_month = sel_q_num * 3
sel_q_end = (date(year, 12, 31) if sel_q_end_month == 12
             else date(year, sel_q_end_month + 1, 1) - timedelta(days=1))

# Aliases for the pulse functions below (they read these names)
week_start = sel_week_start
month_start = sel_month_start
quarter_start = sel_q_start
q_num = sel_q_num

# Subheaders (the dropdown below acts as the date label)
hdr_week.markdown("**Weekly Pulse**")
hdr_month.markdown("**Monthly Review**")
hdr_quarter.markdown("**Quarterly Review**")

# Reusable subset: "live" Proceed pipeline — excludes anything no longer active.
# Completed = already submitted; Discontinued = intentional drop;
# Missed = deadline passed unintentionally (still counted in Overdue separately).
active_proceed = proceed_df[
    ~proceed_df["progress_status"].fillna("").str.lower().isin(
        {"completed", "discontinued", "missed", "missing"}
    )
].copy()


# ----- Helpers shared across pulses -----
def _in(d, start, end) -> bool:
    return d is not None and start <= d <= end


def _has_recent_meeting(uid: str, days: int) -> bool:
    if meetings.empty:
        return False
    cutoff = today - timedelta(days=days)
    m_dates = pd.to_datetime(meetings["meeting_date"], errors="coerce").dt.date
    return bool(((m_dates >= cutoff) & (meetings["rfp_uid"] == uid)).any())


# ----- Weekly Pulse (filters by search_date for "Total opportunities") -----
def _weekly_pulse() -> dict:
    out = dict(total=0, moving=0, cold=0, pending=0, overdue=0)
    if proceed_df.empty:
        return out
    week_end = sel_week_end
    out["total"] = int(proceed_df["_search_date"].apply(lambda d: _in(d, week_start, week_end)).sum())
    out["moving"] = int(((active_proceed["_dtd"].notna())
                         & (active_proceed["_dtd"] >= 0)
                         & (active_proceed["_dtd"] <= 14)).sum())
    # Going cold: no meeting note in last 14 days, per active Proceed
    out["cold"] = int(active_proceed["uid"].apply(lambda u: not _has_recent_meeting(u, 14)).sum())
    # Pending follow-up actions = ALL unresolved meeting actions, not period-scoped
    if not meetings.empty:
        out["pending"] = int((~meetings["is_resolved"].fillna(False)).sum())
    # Overdue includes:
    #   (a) live Proceed rows whose deadline has passed (still trying), AND
    #   (b) any Proceed row explicitly marked Missed (deadline lapsed)
    overdue_live = int(((active_proceed["_deadline_date"].notna())
                        & (active_proceed["_deadline_date"] < today)).sum())
    missed_count = int(proceed_df["progress_status"].fillna("").str.lower()
                       .isin({"missed", "missing"}).sum())
    out["overdue"] = overdue_live + missed_count
    return out


# ----- Monthly Review -----
def _monthly_review() -> dict:
    out = dict(total=0, high=0, medium=0, submitted_month=0, approved_month=0)
    if proceed_df.empty:
        return out
    month_end_eff = min(sel_month_end, today) if today.year == year else sel_month_end
    out["total"] = int(proceed_df["_search_date"].apply(
        lambda d: _in(d, month_start, month_end_eff)).sum())
    # Probability tier — uses core.pipeline.prob_tier (single source of truth)
    tiers = proceed_df["alignment_score"].apply(_tier_of)
    out["high"] = int((tiers == PROB_LABEL_HIGH).sum())
    out["medium"] = int((tiers == PROB_LABEL_MED).sum())
    # Submitted = progress=Completed AND date_completed in month
    completed_mask = proceed_df["progress_status"].fillna("").str.lower().eq("completed")
    in_month_dc = proceed_df["_date_completed"].apply(
        lambda d: _in(d, month_start, month_end_eff))
    out["submitted_month"] = int((completed_mask & in_month_dc).sum())
    # Approved = donor_decision=Approved AND date_of_approval in month
    approved_mask = proceed_df["donor_decision"].fillna("").str.lower().eq("approved")
    in_month_da = proceed_df["_date_approval"].apply(
        lambda d: _in(d, month_start, month_end_eff))
    out["approved_month"] = int((approved_mask & in_month_da).sum())
    return out


# ----- Quarterly Review -----
def _quarterly_review() -> dict:
    out = dict(total=0, submitted=0, approved=0, secured_usd=0.0, missed=0)
    if proceed_df.empty and grants.empty:
        return out
    q_end_eff = min(sel_q_end, today) if today.year == year else sel_q_end

    if not proceed_df.empty:
        out["total"] = int(proceed_df["_search_date"].apply(
            lambda d: _in(d, quarter_start, q_end_eff)).sum())
        # Submitted in quarter (progress=Completed, date_completed in quarter)
        completed_mask = proceed_df["progress_status"].fillna("").str.lower().eq("completed")
        in_q_dc = proceed_df["_date_completed"].apply(
            lambda d: _in(d, quarter_start, q_end_eff))
        out["submitted"] = int((completed_mask & in_q_dc).sum())
        # Approved in quarter (donor_decision=Approved, date_of_approval in quarter)
        approved_mask = proceed_df["donor_decision"].fillna("").str.lower().eq("approved")
        in_q_da = proceed_df["_date_approval"].apply(
            lambda d: _in(d, quarter_start, q_end_eff))
        out["approved"] = int((approved_mask & in_q_da).sum())
        out["secured_usd"] = float(
            proceed_df.loc[approved_mask & in_q_da, "_secured_usd"].sum()
        )
        # Missed grant deadlines: submission_deadline in quarter AND progress != Completed
        missed_mask = proceed_df["_deadline_date"].apply(
            lambda d: _in(d, quarter_start, q_end_eff)) \
            & (~proceed_df["progress_status"].fillna("").str.lower().eq("completed"))
        out["missed"] = int(missed_mask.sum())
    return out


wp = _weekly_pulse()
mr = _monthly_review()
qr = _quarterly_review()

# Render the 3 bullet lists in the SAME 3 columns as the subheaders/dropdowns
with pcols[0]:
    st.markdown(
        f"- Total opportunities this week: **{wp['total']}**\n"
        f"- Moving closer to deadline (<14d): **{wp['moving']}**\n"
        f"- Going cold (no notes in 14d): **{wp['cold']}**\n"
        f"- Pending follow-up actions: **{wp['pending']}**\n"
        f"- Overdue deadlines (passed, not submitted): **{wp['overdue']}**"
    )
with pcols[1]:
    st.markdown(
        f"- Total opportunities this month: **{mr['total']}**\n"
        f"- High probability (>90%): **{mr['high']}**\n"
        f"- Medium probability (70-90%): **{mr['medium']}**\n"
        f"- Submitted proposals this month: **{mr['submitted_month']}**\n"
        f"- Approved this month: **{mr['approved_month']}**"
    )
with pcols[2]:
    st.markdown(
        f"- Total opportunities this quarter: **{qr['total']}**\n"
        f"- Submitted this quarter: **{qr['submitted']}**\n"
        f"- Approved this quarter: **{qr['approved']}**\n"
        f"- Total funding secured (USD): **${qr['secured_usd']:,.0f}**\n"
        f"- Missed grant deadlines (KR2.3): **{qr['missed']}**"
    )

st.markdown("---")


# --- Annual reflection (merged into Periodic Reflection) ---
st.markdown("**Annual reflection**")

# `unique` is already YTD-filtered by submitted_at. We don't double-filter on
# date_completed / date_of_approval because some rows have those dates blank;
# the YTD scope is sufficient for "this year".
completed_mask = unique["progress_status"].fillna("").str.strip().str.lower().eq("completed")
submitted_df = unique[completed_mask].copy()

approved_mask = unique["donor_decision"].fillna("").str.strip().str.lower().eq("approved")
approved_year = unique[approved_mask].copy()

# Total Submitted = sum of Submissions across completed rows (counts donor-side
# submission EVENTS — multi-submit rows count multiple times).
total_submitted = int(submitted_df["_submissions"].sum()) if not submitted_df.empty else 0
total_requested_usd = float(submitted_df["_requested_usd"].sum()) if not submitted_df.empty else 0.0
total_secured_usd = float(approved_year["_secured_usd"].sum()) if not approved_year.empty else 0.0
pct_secured = (total_secured_usd / total_requested_usd * 100) if total_requested_usd > 0 else 0.0

active_donors = int(approved_year["funding_agency"].dropna().nunique()) if not approved_year.empty else 0

largest_source_text = "—"
top_share_pct = 0.0
if not approved_year.empty and total_secured_usd > 0:
    by_donor_secured = approved_year.groupby("funding_agency")["_secured_usd"].sum().sort_values(ascending=False)
    top_donor = by_donor_secured.index[0]
    top_share_pct = by_donor_secured.iloc[0] / total_secured_usd * 100
    largest_source_text = f"{top_donor} ({top_share_pct:.0f}%)"

if top_share_pct >= 70:
    diversification = "Concentrated — improve"
elif top_share_pct >= 40:
    diversification = "Moderately concentrated"
elif top_share_pct == 0:
    diversification = "—"
else:
    diversification = "Diversified"

if top_share_pct >= 70:
    risk = f"HIGH — top donor holds {top_share_pct:.0f}% of secured"
elif top_share_pct >= 40:
    risk = f"MEDIUM — top donor holds {top_share_pct:.0f}% of secured"
elif top_share_pct == 0:
    risk = "—"
else:
    risk = f"LOW — top donor holds {top_share_pct:.0f}% of secured"

# Row 1 — quantitative: count → requested → secured → % secured
ar1, ar2, ar3, ar4 = st.columns(4)
with ar1:
    _kpi("Total Submitted RFPs", total_submitted,
         "Sum of Submissions where Progress = Completed")
with ar2:
    _kpi("Total Requested (USD)", f"${total_requested_usd:,.0f}",
         "Sum of Amount Requested (falls back to Estimated Value)")
with ar3:
    _kpi("Total Secured (USD)", f"${total_secured_usd:,.0f}",
         "Sum of Amount Secured (Approved)")
with ar4:
    _kpi("% Secured", f"{pct_secured:.1f}%", "Secured ÷ Requested")

# Row 2 — qualitative: donors → source → trend → risk
ar5, ar6, ar7, ar8 = st.columns(4)
with ar5:
    _kpi("Active donors / partners", active_donors, "With ≥ 1 approval this year")
with ar6:
    _kpi("Largest funding source", largest_source_text)
with ar7:
    _kpi("Diversification trend", diversification)
with ar8:
    _kpi("Dependency risk", risk)

st.divider()


# =============================================================================
# E. Partner Engagement KRs  (was H. KR2.2 / KR2.3 / KR2.4)
# =============================================================================
st.subheader("E · Partner engagement KRs")
kr1, kr2 = st.columns(2)
kr3, kr4 = st.columns(2)

# KR2.2 — Donor Engagements in selected quarter
q_end_eff = min(sel_q_end, today) if today.year == year else sel_q_end
q_engagements = 0
if not engagements.empty:
    edates = pd.to_datetime(engagements["engagement_date"], errors="coerce").dt.date
    q_engagements = int(((edates.notna()) & (edates >= quarter_start) & (edates <= q_end_eff)).sum())
target_low, target_high = 2, 4
if q_engagements < target_low:
    kr22_status = "below target"
elif q_engagements <= target_high:
    kr22_status = "on track"
else:
    kr22_status = "exceeding"
with kr1:
    with st.container(border=True):
        st.caption("KR2.2 — Donor Engagements (this quarter)")
        st.markdown(f"**Target:** 2–4 per quarter")
        _kpi("", f"{q_engagements} — {kr22_status}")
        st.caption("_Source: engagement_logs.engagement_date_")

# KR2.3 — Missed Reporting Deadlines in selected quarter
missed_reports = 0
if not grants.empty:
    for _, gg in grants.iterrows():
        due = _safe_date(gg.get("report_due_date"))
        if due is None or due > q_end_eff or due < quarter_start:
            continue
        if _safe_str(gg.get("status")).strip().lower() != "submitted":
            missed_reports += 1
miss_status = "target met" if missed_reports == 0 else "above target"
with kr2:
    with st.container(border=True):
        st.caption("KR2.3 — Missed Reporting Deadlines (this quarter)")
        st.markdown(f"**Target:** 0 missed")
        _kpi("", f"{missed_reports} missed — {miss_status}")
        st.caption("_Source: active_grants.report_due_date, submitted_date_")

# KR2.3 — Reports Due Next 30 Days (forward-look)
due_30 = 0
horizon = today + timedelta(days=30)
if not grants.empty:
    for _, gg in grants.iterrows():
        due = _safe_date(gg.get("report_due_date"))
        if due is None or due < today or due > horizon:
            continue
        if _safe_str(gg.get("status")).strip().lower() != "submitted":
            due_30 += 1
with kr3:
    with st.container(border=True):
        st.caption("KR2.3 — Reports Due Next 30 Days (forward-look)")
        st.markdown("**Forward-looking workload**")
        _kpi("", "None due in next 30 days" if due_30 == 0 else f"{due_30} due in next 30 days")
        st.caption("_Source: active_grants.report_due_date, submitted_date_")

# KR2.4 — Country Narrative Status in selected quarter
narr_text = "— select quarter —"
if not narratives.empty:
    in_q_count = 0
    in_q_current = 0
    for _, nn in narratives.iterrows():
        d = _safe_date(nn.get("date_used"))
        if d is None or d < quarter_start or d > q_end_eff:
            continue
        in_q_count += 1
        if _safe_str(nn.get("status")).strip().lower() == "current":
            in_q_current += 1
    if in_q_current:
        narr_text = f"{in_q_current} current narrative(s) in use {sel_q_label}"
    else:
        any_current = sum(
            1 for _, nn in narratives.iterrows()
            if _safe_str(nn.get("status")).strip().lower() == "current"
        )
        narr_text = f"{any_current} narrative(s) tagged 'Current'"
with kr4:
    with st.container(border=True):
        st.caption("KR2.4 — Country Narrative Status (this quarter)")
        st.markdown("**Target:** narrative in use each quarter")
        _kpi("", narr_text)
        st.caption("_Source: narrative_logs.version_date, status, date_used_")
