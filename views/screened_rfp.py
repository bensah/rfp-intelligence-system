"""Page 1 — Screenings (Weekly RFP dashboard).

Mirrors the Excel `RFP_Screening` sheet:
  - 4-metric KPI strip (Total Screened / Proceed / Parked / Declined)
  - Duplicate banner
  - PROCEED RFPs list with rationale & risks (inline decision override)
  - 4 secondary KPIs (Largest Opportunity / Nearest Deadline / Prime / Sub)

Designed for quick Monday-morning triage. The 4 secondary KPIs always render
— even when a bucket is empty — so the layout stays consistent week to week.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import streamlit as st

from core import dropdowns, settings
from core.currency import format_money
from core.review_week import all_weeks_for_year, review_week_label, upcoming_review_week_label
from db.supabase_client import get_client

# auth handled by wrapper page
user = st.session_state["app_user"]
sb = get_client()

from core.scan_runner import run_scan_now

_title_col, _scan_col = st.columns([5, 1])
with _title_col:
    st.title("Weekly Screened RFPs")
with _scan_col:
    st.write("")  # vertical spacer to align button with title
    if st.button(
        "🔄 Scan now", type="primary", use_container_width=True,
        key="screened_scan_now",
        help="Run the donor-source scanner now. Any new RFPs found are inserted and will appear on this page after the run completes.",
    ):
        # Lock navigation during scan so the user can't switch tabs and
        # see the duplicated/grayed-out render Streamlit produces while a
        # long subprocess blocks the script.
        st.markdown(
            """
            <style>
              [data-testid="stTabs"] [role="tablist"],
              [data-testid="stSidebarNav"] {
                pointer-events: none !important;
                opacity: 0.45 !important;
              }
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.info(
            "⏳ Scan in progress — please stay on this tab. A full run "
            "(~40 donor sources with detail-page + PDF enrichment) "
            "typically takes **3-8 minutes**."
        )
        run_scan_now(
            triggered_by=f"manual:{user.get('name') or user.get('email') or 'unknown'}"
        )
        st.rerun()

# -----------------------------------------------------------------------------
# Week selector (year sourced from app_settings — change in Admin > Settings)
# -----------------------------------------------------------------------------
year = settings.get_year()
all_weeks = all_weeks_for_year(year)
default_week = review_week_label()
if default_week not in all_weeks:
    all_weeks = [default_week] + all_weeks

selected_week = st.selectbox(
    f"Review week ({year})", all_weeks, index=all_weeks.index(default_week),
    key="screened_rfp_week",
)


# -----------------------------------------------------------------------------
# Data fetch
# -----------------------------------------------------------------------------
@st.cache_data(ttl=30)
def _fetch(week: str) -> pd.DataFrame:
    res = (
        get_client()
        .table("rfp_submissions")
        .select("*")
        .eq("review_week", week)
        .execute()
    )
    return pd.DataFrame(res.data or [])


@st.cache_data(ttl=60)
def _weeks_with_data() -> list[str]:
    res = (
        get_client()
        .table("rfp_submissions")
        .select("review_week")
        .execute()
    )
    weeks = pd.Series([r.get("review_week") for r in res.data or []]).dropna()
    return weeks.value_counts().index.tolist()


df = _fetch(selected_week)
# Canonical (non-duplicate) rows — what we actually "screened" this week.
# A row flagged as duplicate-of an RFP in another week shouldn't keep the
# page open by itself, so we treat unique-empty as "nothing screened".
canonical_df = df[~df["is_duplicate"].fillna(False)].copy() if not df.empty else df


# -----------------------------------------------------------------------------
# Empty-state fallback — suggest most recent week WITH data
# -----------------------------------------------------------------------------
if canonical_df.empty:
    if df.empty:
        st.info(f"No RFPs recorded for **{selected_week}**.")
    else:
        # Rows exist for this week but they're all duplicates of canonicals
        # in OTHER weeks — semantically nothing was screened here.
        st.info(
            f"No RFPs screened for **{selected_week}** "
            f"({len(df)} record(s) tagged to this week are duplicates of "
            "canonical RFPs in other weeks)."
        )
    recent = _weeks_with_data()
    if recent:
        # Pick the most recent (by week number)
        def _num(label: str) -> int:
            try:
                return int(label.split(" ")[1])
            except Exception:
                return -1
        latest = max(recent, key=_num)
        cta1, cta2 = st.columns([3, 1])
        cta1.markdown(f"Most recent week with activity: **{latest}**")
        if cta2.button(f"Jump to {latest}", use_container_width=True):
            st.session_state["screenings_jump"] = latest
            st.rerun()
    st.stop()


# Honour a pending jump-to from the empty-state CTA
if "screenings_jump" in st.session_state:
    jump = st.session_state.pop("screenings_jump")
    if jump in all_weeks:
        st.session_state["_widget_jump_pending"] = jump


# -----------------------------------------------------------------------------
# Filters
# -----------------------------------------------------------------------------
with st.expander("Filters", expanded=False):
    fc1, fc2, fc3, fc4 = st.columns(4)
    f_dec = fc1.multiselect("Decision", sorted(df["decision"].dropna().unique().tolist()))
    f_feas = fc2.multiselect("Feasibility", sorted(df["feasibility"].dropna().unique().tolist()))
    geo_opts = sorted({g for arr in df["geographic_scope"].dropna() for g in (arr or [])})
    f_geo = fc3.multiselect("Geographic scope", geo_opts)
    prog_opts = sorted({p for arr in df["program_area"].dropna() for p in (arr or [])})
    f_prog = fc4.multiselect("Program area", prog_opts)

mask = pd.Series(True, index=df.index)
if f_dec:
    mask &= df["decision"].isin(f_dec)
if f_feas:
    mask &= df["feasibility"].isin(f_feas)
if f_geo:
    mask &= df["geographic_scope"].apply(lambda v: bool(set(v or []) & set(f_geo)))
if f_prog:
    mask &= df["program_area"].apply(lambda v: bool(set(v or []) & set(f_prog)))
fdf = df[mask].copy()


# -----------------------------------------------------------------------------
# KPI row
# -----------------------------------------------------------------------------
unique = fdf[~fdf["is_duplicate"].fillna(False)].copy()
# Only count an "in-week duplicate" — a row flagged is_duplicate=True whose
# canonical is ALSO in this week. Duplicates that point to canonicals in
# other weeks shouldn't inflate this period's banner.
canonical_uids_this_week = set(unique["uid"].tolist())
in_week_dups = fdf[
    fdf["is_duplicate"].fillna(False)
    & fdf["duplicate_of_uid"].isin(canonical_uids_this_week)
]
duplicate_count = int(len(in_week_dups))

# Effective decision = human-set `decision` if present, else the
# auto_recommendation from policy scoring. This lets newly-scanned RFPs
# (where decision is NULL) immediately show up in the Proceed / Parked /
# Declined buckets based on their auto-recommendation, without requiring
# a human to click through Review for every row.
_eff = (
    unique["decision"]
    .fillna(unique.get("auto_recommendation", pd.Series([], dtype=str)))
    .fillna("")
    .astype(str)
    .str.strip()
    .str.lower()
)
proceed = int(_eff.str.startswith("proceed").sum())
parked = int(_eff.eq("park").sum())
declined = int(_eff.eq("decline").sum())
dec_lower = _eff  # downstream code expects this name
total = len(unique)

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Screened", total)
k2.metric("Proceed", proceed)
k3.metric("Parked", parked)
k4.metric("Declined", declined)

if duplicate_count:
    st.warning(
        f"⚠ {duplicate_count} duplicate record(s) flagged this period and excluded from KPIs."
    )
else:
    st.success("✓ No duplicates detected in this period.")


# -----------------------------------------------------------------------------
# Compute "largest", "nearest", prime/sub once; always show the 4 KPIs
# -----------------------------------------------------------------------------
proceed_df = unique[dec_lower.str.startswith("proceed").to_numpy()].copy()
if not proceed_df.empty:
    proceed_df["_usd"] = proceed_df.apply(
        lambda r: (r.get("estimated_value") or 0) * dropdowns.usd_rate(r.get("currency")),
        axis=1,
    )

c1, c2, c3, c4 = st.columns(4)

with c1:
    st.caption("Largest Opportunity")
    if not proceed_df.empty and proceed_df["_usd"].max() > 0:
        largest = proceed_df.loc[proceed_df["_usd"].idxmax()]
        st.markdown(
            f"**{format_money(largest.get('estimated_value'), largest.get('currency'))}**  \n"
            f"{(largest['opportunity_title'] or '')[:60]}"
        )
    else:
        st.markdown("**—**  \n_No Proceed RFP with a value_")

with c2:
    st.caption("Nearest Deadline")
    soonest = (
        proceed_df.dropna(subset=["submission_deadline"])
        .sort_values("submission_deadline")
        .head(1)
        if not proceed_df.empty else pd.DataFrame()
    )
    if not soonest.empty:
        r = soonest.iloc[0]
        st.markdown(
            f"**{r['submission_deadline']}**  \n"
            f"{(r['opportunity_title'] or '')[:60]}"
        )
    else:
        st.markdown("**—**  \n_No deadlines on Proceed RFPs_")

with c3:
    prime = int(proceed_df["applicant_role"].fillna("").str.lower().eq("prime").sum()) if not proceed_df.empty else 0
    st.caption("Prime Opportunities")
    st.markdown(f"### {prime} of {len(proceed_df)}")
    st.caption("Proceed as prime applicant")

with c4:
    sub = int(proceed_df["applicant_role"].fillna("").str.lower().eq("sub").sum()) if not proceed_df.empty else 0
    st.caption("Sub Opportunities")
    st.markdown(f"### {sub} of {len(proceed_df)}")
    st.caption("Proceed, applying as Sub")

st.divider()


# -----------------------------------------------------------------------------
# Proceed RFPs — rationale & risks (matches Excel layout)
# -----------------------------------------------------------------------------
st.subheader(f"Proceed RFPs ({len(proceed_df)}) — Rationale & Risks")
if proceed_df.empty:
    st.info("No Proceed RFPs in this period.")
else:
    # Read-only tabular view — edits happen on Review or Data pages
    show = proceed_df.sort_values("alignment_score", ascending=False).copy()
    show_df = pd.DataFrame({
        "UID": show["uid"],
        "Title": show["opportunity_title"].fillna("—"),
        "Funder": show["funding_agency"].fillna("—"),
        "Role": show["applicant_role"].fillna("—"),
        "Deadline": pd.to_datetime(show["submission_deadline"], errors="coerce").dt.date,
        "Score": show["alignment_score"].fillna(0).round(0),
        "Decision": show["decision"].fillna("—"),
        "Auto-rec": show["auto_recommendation"].fillna("—"),
        "Key risks": show["key_risks"].fillna(""),
    })
    st.dataframe(
        show_df, use_container_width=True, hide_index=True,
        column_config={
            "Score": st.column_config.NumberColumn("Score", format="%.0f"),
            "Key risks": st.column_config.TextColumn("Key risks", width="large"),
        },
    )
    st.caption("Edits are made on the **Review** page (eligibility + decision) or **Data** page (any field).")
