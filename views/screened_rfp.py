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
from core.records import clean_df, drop_concluded
from db.supabase_client import get_client

# auth handled by wrapper page
user = st.session_state["app_user"]
sb = get_client()

# -----------------------------------------------------------------------------
# Title + week selector. Year comes from app_settings (Admin → Settings).
# The "Scan now" action lives on the page-title row in app_pages/pipelines.py
# now (beside "Discovered RFP Pipelines"), so it's reachable from every tab.
# -----------------------------------------------------------------------------
year = settings.get_year()
from core.scan_runner import run_screening_now
# Header: "Weekly Screening Pipeline" + the two compact page actions on the same level.
_hl, _hspacer, _h_submit, _h_scan = st.columns([4.4, 1.2, 1.5, 1.6],
                                               vertical_alignment="center")
with _hl:
    st.markdown(
        f"<h2 style='font-size:1.55rem;font-weight:700;color:#334155;"
        f"margin:0.15rem 0 0.5rem;'>Weekly Screening Pipeline ({year})</h2>",
        unsafe_allow_html=True,
    )
with _h_submit:
    if st.button("📝 Submit New Funding", type="secondary", width='stretch',
                 key="screen_submit_new",
                 help="Capture a funding opportunity you found outside the scan."):
        st.switch_page("app_pages/submit_rfp.py")
with _h_scan:
    # "Eligibility Scan" = screen the platform's curated store against THIS org's
    # eligibility. Fast (no web crawl); flips to a disabled "running" state.
    _scan_slot = st.empty()
    _go = _scan_slot.button(
        "🎯 Eligibility Scan", type="primary", width='stretch', key="screen_scan_now",
        help="Find the funding your organisation is potentially eligible for, from the "
             "platform's curated store — runs in seconds (no web crawl).")
    if _go:
        _scan_slot.button("⏳ Scanning…", disabled=True, width='stretch',
                          key="screen_scan_running")
        _who = user.get("name") or user.get("email") or "unknown"
        run_screening_now(triggered_by=f"match:{_who}")
        st.rerun()
all_weeks = all_weeks_for_year(year)
default_week = review_week_label()
if default_week not in all_weeks:
    all_weeks = [default_week] + all_weeks

selected_week = st.selectbox(
    f"Review week", all_weeks, index=all_weeks.index(default_week),
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
    return clean_df(pd.DataFrame(res.data or []))


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
# Drop concluded grants (won/submitted) — they belong in Grants + the Home Summary,
# not the active screening list (e.g. HAPPI: Completed + Approved).
df = drop_concluded(df)

# LIVE scoring — single source of truth (shared with Review / Records / View modal).
# Stored alignment_score / auto_recommendation are a scan-time snapshot that goes stale
# when org profile / donor intel / scoring logic change; recompute fresh so the Screen
# buckets + Score column match the Review gauge. Cached, busted by a profile signature.
if not df.empty:
    import json as _json_live
    import hashlib as _hashlib_live
    from core import org_profile as _op_live, settings as _stg_live
    from core.assessment import assess_row as _assess_row

    _prof_sig = _hashlib_live.sha1(
        (_json_live.dumps(_op_live.get_profile(), sort_keys=True, default=str)
         + _json_live.dumps(_stg_live.get_org(), sort_keys=True, default=str)).encode()
    ).hexdigest()[:12]

    # PER-ROW memo in session_state — only new/edited rows are scored, so navigation and
    # pagination don't re-run scoring (see core.live_scoring). Survives st.cache_data.clear().
    try:
        from core.live_scoring import scores_for as _scores_for
        _memo = st.session_state.setdefault("_screen_score_memo", {})
        with st.spinner("Scoring rows…"):    # only visible when real work happens
            _sc, _ = _scores_for(df.to_dict("records"), _prof_sig, _memo)
        df["alignment_score"] = df["uid"].map(lambda u: (_sc.get(str(u)) or {}).get("alignment_score"))
        df["auto_recommendation"] = df["uid"].map(lambda u: (_sc.get(str(u)) or {}).get("auto_recommendation"))
    except Exception:
        pass
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
        if cta2.button(f"Jump to {latest}", width='stretch'):
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
    geo_opts = sorted({g for arr in df["call_geographic_scope"].dropna() for g in (arr or [])})
    f_geo = fc3.multiselect("Geographic scope", geo_opts)
    prog_opts = sorted({p for arr in df["call_domain_areas"].dropna() for p in (arr or [])})
    f_prog = fc4.multiselect("Program area", prog_opts)

mask = pd.Series(True, index=df.index)
if f_dec:
    mask &= df["decision"].isin(f_dec)
if f_feas:
    mask &= df["feasibility"].isin(f_feas)
if f_geo:
    mask &= df["call_geographic_scope"].apply(lambda v: bool(set(v or []) & set(f_geo)))
if f_prog:
    mask &= df["call_domain_areas"].apply(lambda v: bool(set(v or []) & set(f_prog)))
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
        lambda r: (r.get("call_award_value") or 0) * dropdowns.usd_rate(r.get("currency")),
        axis=1,
    )

# End-of-page review queue = Proceed AND Parked. Parked calls were surfaced
# this week and still need a visible review slot (they're not auto-Declined);
# the KPI cards above stay Proceed-only ("what we'll pursue").
_actionable_mask = (dec_lower.str.startswith("proceed") | dec_lower.eq("park")).to_numpy()
actionable_df = unique[_actionable_mask].copy()

c1, c2, c3, c4 = st.columns(4)

with c1:
    st.caption("Largest Opportunity")
    if not proceed_df.empty and proceed_df["_usd"].max() > 0:
        largest = proceed_df.loc[proceed_df["_usd"].idxmax()]
        st.markdown(
            f"**{format_money(largest.get('call_award_value'), largest.get('currency'))}**  \n"
            f"{(largest['opportunity_title'] or '')[:60]}"
        )
    else:
        st.markdown("**—**  \n_No Proceed RFP with a value_")

with c2:
    st.caption("Nearest Deadline")
    soonest = (
        proceed_df.dropna(subset=["call_submission_deadline"])
        .sort_values("call_submission_deadline")
        .head(1)
        if not proceed_df.empty else pd.DataFrame()
    )
    if not soonest.empty:
        r = soonest.iloc[0]
        st.markdown(
            f"**{r['call_submission_deadline']}**  \n"
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
# Proceed & Parked RFPs — rationale & risks (weekly review queue)
# -----------------------------------------------------------------------------
st.subheader(f"Proceed & Parked RFPs ({len(actionable_df)}) — Rationale & Risks")
if actionable_df.empty:
    st.info("No Proceed or Parked RFPs in this period.")
else:
    # Read-only tabular view — edits happen on Review or Data pages.
    show = actionable_df.copy()
    # Effective decision (human override, else auto-recommendation) drives both
    # the displayed Decision and the ordering: Proceed first, then Park; within
    # each, highest score first.
    _eff_dec = (
        show["decision"]
        .fillna(show.get("auto_recommendation", pd.Series([], dtype=str)))
        .fillna("").astype(str).str.strip()
    )
    show["_ord"] = _eff_dec.str.lower().apply(lambda d: 0 if d.startswith("proceed") else 1)
    show = show.sort_values(["_ord", "alignment_score"], ascending=[True, False])
    _eff_dec = _eff_dec.reindex(show.index)
    show_df = pd.DataFrame({
        "UID": show["uid"],
        "Title": show["opportunity_title"].fillna("—"),
        "Funder": show["funding_agency"].fillna("—"),
        "Role": show["applicant_role"].fillna("—"),
        "Deadline": pd.to_datetime(show["call_submission_deadline"], errors="coerce", format="ISO8601").dt.date,
        "Score": show["alignment_score"].fillna(0).round(0),
        "Decision": _eff_dec.replace("", "—").str.title(),
        "Auto-rec": show["auto_recommendation"].fillna("—"),
        "Key risks": show["key_risks"].fillna(""),
    })
    # Confidence (E3c): how much DATA backs each row's prediction — donor mapping
    # completeness + call extraction completeness → High/Medium/Low. match_donor is
    # index-cached, so the per-row lookup is cheap.
    from core import data_quality as _dq
    from core.donor_intel import match_donor as _md
    _CONF_ICON = {"High": "🟢 High", "Medium": "🟡 Medium", "Low": "🔴 Low"}

    def _row_conf(r) -> str:
        dp, _, _ = _dq.donor_completeness(_md(r.get("funding_agency")))
        cp, _, _ = _dq.call_completeness(r.to_dict())
        return _CONF_ICON[_dq.confidence_band(dp, cp)[0]]

    show_df["Confidence"] = [_row_conf(r) for _, r in show.iterrows()]
    st.dataframe(
        show_df, width='stretch', hide_index=True,
        column_config={
            "Score": st.column_config.NumberColumn("Score", format="%.0f"),
            "Confidence": st.column_config.TextColumn(
                "Confidence", help="Data behind the prediction (donor mapping + call "
                "extraction completeness). Low = verify before acting."),
            "Key risks": st.column_config.TextColumn("Key risks", width="large"),
        },
    )
    st.caption("Proceed rows first, then Parked. Edits are made on the "
               "**Review** page (eligibility + decision) or **Data** page (any field).")
