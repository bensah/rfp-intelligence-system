"""Page 3 — Tracking (Year-to-Date Proceed pipeline).

UX:
  1. A single RFP-detail card at top, swappable via either a search dropdown
     OR by clicking a row in the table below.
  2. Inside the card: Next Action / Assigned to / Action due are pulled
     from the meeting_logs for the selected review week. If no meeting note
     exists, Action due defaults to the Monday after that week.
  3. All edits happen from the Data page — Tracking is **read-only**.

Filters (active by default):
  - Only Proceed / Proceed as sub (post-screening)
  - Not yet submitted to a donor (donor_decision NULL or 'Not submitted')
  - Progress status ≠ Completed
  - submission_deadline is null OR ≥ today (not overdue)
  - submitted_at year == settings.get_year() (YTD)
  - is_duplicate = false (canonical row only; duplicate leads concatenated)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import dropdowns, settings
from core.currency import format_money
from core.pipeline import days_to_deadline, deadline_status, usd_value
from core.review_week import all_weeks_for_year, week_bounds
from db.supabase_client import get_client

# auth handled by wrapper page
user = st.session_state["app_user"]
sb = get_client()

year = settings.get_year()
today = date.today()

st.markdown(
    f"<h2 style='font-size:1.55rem;font-weight:700;color:#334155;"
    f"margin:0.15rem 0 0.5rem;'>YTD Proceed Pipeline ({year})</h2>",
    unsafe_allow_html=True,
)
st.caption(
    "Read-only pipeline of active **Proceed** RFPs for "
    f"**{year}** that haven't been submitted to a donor, aren't past deadline, "
    "and aren't marked Completed. Edits are made via the Data page."
)


# -----------------------------------------------------------------------------
# Data fetch + dedup-aware view
# -----------------------------------------------------------------------------
@st.cache_data(ttl=30)
def _fetch(year: int) -> pd.DataFrame:
    sbc = get_client()
    res = (
        sbc.table("rfp_submissions")
        .select("*")
        .eq("is_duplicate", False)
        .in_("decision", ["Proceed", "Proceed as sub"])
        .execute()
    )
    df = pd.DataFrame(res.data or [])
    if df.empty:
        return df

    # YTD by submitted_at
    df["_submitted_at"] = pd.to_datetime(df["submitted_at"], errors="coerce", format="ISO8601")
    df = df[df["_submitted_at"].dt.year == year].copy()

    # Exclude submitted/approved/completed
    dd_lower = df["donor_decision"].fillna("").str.lower()
    df = df[dd_lower.isin({"", "not submitted"})].copy()
    if "progress_status" in df:
        # Exclude only rows that have actually been submitted to a donor
        # (Completed = submitted). Discontinued / Missed stay visible so
        # the team can see what was dropped or missed.
        df = df[~df["progress_status"].fillna("").astype(str).str.strip()
                .str.lower().isin({"completed"})].copy()

    # Not overdue (allow null deadlines)
    df["_deadline_date"] = pd.to_datetime(df["submission_deadline"], errors="coerce", format="ISO8601").dt.date
    df = df[df["_deadline_date"].isna() | (df["_deadline_date"] >= today)].copy()

    if df.empty:
        return df

    # Derived columns
    df["_dtd"] = df["submission_deadline"].apply(days_to_deadline)
    df["_dstat"] = df["submission_deadline"].apply(deadline_status)
    df["_usd"] = df.apply(lambda r: usd_value(r.get("estimated_value"), r.get("currency")), axis=1)

    # Concatenate Proposal Leads from duplicate group
    dup_res = (
        sbc.table("rfp_submissions")
        .select("duplicate_of_uid,proposal_lead")
        .eq("is_duplicate", True)
        .in_("duplicate_of_uid", df["uid"].tolist())
        .execute()
        .data
        or []
    )
    extra_leads: dict[str, list[str]] = defaultdict(list)
    for d in dup_res:
        if d.get("proposal_lead") and d.get("duplicate_of_uid"):
            extra_leads[d["duplicate_of_uid"]].append(d["proposal_lead"])

    def _leads(r):
        leads = [r.get("proposal_lead")] + extra_leads.get(r["uid"], [])
        return ", ".join(sorted({l for l in leads if l}))

    df["all_leads"] = df.apply(_leads, axis=1)
    df = df.sort_values(by="_dtd", na_position="last").reset_index(drop=True)
    return df


df = _fetch(year)
if df.empty:
    st.info(
        f"No active Proceed RFPs in {year}. An RFP shows up here once its "
        "decision is set to Proceed and the deadline hasn't passed."
    )
    st.stop()


# -----------------------------------------------------------------------------
# Compact KPI strip
# -----------------------------------------------------------------------------
overdue_excluded = "(overdue rows excluded by design)"
k1, k2, k3, k4 = st.columns(4)
k1.metric("Active Proceed RFPs", len(df))
k2.metric("Due in 14 days", int((df["_dstat"] == "Due Soon").sum()))
k3.metric("On Track", int((df["_dstat"] == "On Track").sum()))
k4.metric("Pipeline value (USD)", f"${float(df['_usd'].sum()):,.0f}")


# -----------------------------------------------------------------------------
# RFP selector — combo dropdown
# -----------------------------------------------------------------------------
SELECTED_UID_KEY = "tracking_selected_uid"


def _label(r: dict) -> str:
    return f"{r['uid']} · {(r.get('funding_agency') or '—')[:30]} · {(r.get('opportunity_title') or '')[:80]}"


labels = [_label(r) for _, r in df.iterrows()]
uid_by_label = {_label(r): r["uid"] for _, r in df.iterrows()}

stored_uid = st.session_state.get(SELECTED_UID_KEY)
default_idx = next(
    (i for i, r in enumerate(df.itertuples()) if r.uid == stored_uid),
    0,
)

picked_label = st.selectbox(
    "Select RFP to review",
    labels,
    index=default_idx,
    help="Or click a row in the table at the bottom.",
)
selected_uid = uid_by_label[picked_label]
st.session_state[SELECTED_UID_KEY] = selected_uid

row = df[df["uid"] == selected_uid].iloc[0].to_dict()

st.divider()


# -----------------------------------------------------------------------------
# Detail card for selected RFP
# -----------------------------------------------------------------------------
BADGE = {
    "Overdue":  ("🔴", "#fde2e2"),
    "Due Soon": ("🟡", "#fff4cc"),
    "On Track": ("🟢", "#dcf5e3"),
}
status = row.get("_dstat") or "On Track"
icon, bg = BADGE.get(status, ("⚪", "#eee"))

with st.container(border=True):
    top1, top2, top3, top4 = st.columns([4, 1, 1, 1])
    top1.markdown(
        f"### {row.get('opportunity_title')}\n"
        f"_{row.get('funding_agency') or '—'}_ · "
        f"role: **{row.get('applicant_role') or '—'}** · "
        f"window: **{row.get('funding_window') or '—'}** · "
        f"decision: **{row.get('decision') or '—'}**"
    )
    top2.markdown(
        f"<div style='background:{bg};padding:8px 12px;border-radius:6px;"
        f"text-align:center;font-weight:600'>{icon} {status}</div>",
        unsafe_allow_html=True,
    )
    dtd = row.get("_dtd")
    # _dtd rides in a pandas column that may hold NaN, so it arrives as a
    # float (e.g. 5.0) or NaN — `:d` only accepts ints, so coerce + guard.
    top3.metric("Days to deadline",
                f"{int(dtd):+d}" if pd.notna(dtd) else "—")
    top4.metric("Value", format_money(row.get("estimated_value"), row.get("currency")))
    top4.caption(f"≈ ${row.get('_usd') or 0:,.0f} USD")

    d1, d2, d3 = st.columns(3)
    d1.markdown(f"**Submission deadline**  \n{row.get('submission_deadline') or '—'}")
    d1.markdown(f"**Expected award**  \n{row.get('expected_award_date') or '—'}")
    d2.markdown(f"**Proposal lead(s)**  \n{row.get('all_leads') or '—'}")
    d2.markdown(f"**Stage**  \n{row.get('stage') or '—'}")
    d3.markdown(f"**Progress status**  \n{row.get('progress_status') or '—'}")
    d3.markdown(f"**Geography**  \n{', '.join(row.get('geographic_scope') or []) or '—'}")

    # ----- Meeting-log review week selector for this RFP -----
    st.markdown("---")
    st.markdown("**Next action — from Meeting Log**")

    # Pre-fetch meeting weeks where this RFP has notes
    notes_all = (
        sb.table("meeting_logs")
        .select("meeting_date,actions,owner,deadline")
        .eq("rfp_uid", selected_uid)
        .order("meeting_date", desc=True)
        .execute()
        .data
        or []
    )
    weeks_with_notes = sorted({n["meeting_date"] for n in notes_all if n.get("meeting_date")}, reverse=True)

    all_weeks = all_weeks_for_year(year)

    def _monday_label_to_date(label: str) -> date:
        week_num = int(label.split(" ")[1])
        jan4 = date(year, 1, 4)
        mon, _ = week_bounds(jan4)
        return mon + timedelta(days=(week_num - mon.isocalendar().week) * 7)

    # Default: the latest week with a note for this RFP, else current week
    if weeks_with_notes:
        latest_note_mon = pd.to_datetime(weeks_with_notes[0]).date()
        default_label = next(
            (w for w in all_weeks if _monday_label_to_date(w) == latest_note_mon),
            all_weeks[0],
        )
    else:
        # current ISO week
        today_mon = today - timedelta(days=today.weekday())
        default_label = next(
            (w for w in all_weeks if _monday_label_to_date(w) == today_mon),
            all_weeks[0],
        )

    rw_col, _spacer = st.columns([2, 4])
    rw_label = rw_col.selectbox(
        "Review week",
        all_weeks,
        index=all_weeks.index(default_label),
        key=f"tracking_week_{selected_uid}",
    )
    rw_monday = _monday_label_to_date(rw_label)
    default_due = rw_monday + timedelta(days=7)

    note_for_week = next(
        (n for n in notes_all if str(n.get("meeting_date")) == rw_monday.isoformat()),
        None,
    )

    nc1, nc2, nc3 = st.columns([3, 2, 2])
    if note_for_week:
        nc1.markdown(f"**Next action**  \n{note_for_week.get('actions') or '_(none captured)_'}")
        nc2.markdown(f"**Assigned to**  \n{note_for_week.get('owner') or '—'}")
        nc3.markdown(
            f"**Action due**  \n{note_for_week.get('deadline') or default_due.isoformat()}"
        )
        if not note_for_week.get("deadline"):
            nc3.caption("_(defaulted to Monday after review week)_")
    else:
        nc1.markdown("**Next action**  \n_(no meeting note for this week)_")
        nc2.markdown("**Assigned to**  \n—")
        nc3.markdown(f"**Action due**  \n{default_due.isoformat()}")
        nc3.caption("_(defaulted to Monday after review week)_")


# -----------------------------------------------------------------------------
# Browse table — all active Proceed RFPs YTD (click to swap selection)
# -----------------------------------------------------------------------------
st.markdown("")
st.subheader(f"All active Proceed RFPs ({len(df)})")
st.caption(
    "Click any row to load it into the card above. Columns: UID · Funder · Title · "
    "Role · Deadline · Days · Stage · Progress · Lead(s) · USD value."
)

browse = pd.DataFrame({
    "UID": df["uid"],
    "Funder": df["funding_agency"].fillna("—"),
    "Title": df["opportunity_title"].fillna("—"),
    "Role": df["applicant_role"].fillna("—"),
    "Deadline": df["_deadline_date"],
    "Days": df["_dtd"],
    "Stage": df["stage"].fillna("—"),
    "Progress": df["progress_status"].fillna("—"),
    "Lead(s)": df["all_leads"].fillna("—"),
    "USD value": df["_usd"].astype(float),
})

event = st.dataframe(
    browse,
    use_container_width=True,
    hide_index=True,
    selection_mode="single-row",
    on_select="rerun",
    column_config={
        "Days":      st.column_config.NumberColumn("Days to deadline"),
        "USD value": st.column_config.NumberColumn("USD value", format="$%.0f"),
    },
)
sel = event.selection.rows if event and getattr(event, "selection", None) else []
if sel:
    new_uid = df.iloc[sel[0]]["uid"]
    if new_uid != selected_uid:
        st.session_state[SELECTED_UID_KEY] = new_uid
        st.rerun()
