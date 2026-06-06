"""Page 7 — Engagement Log (KR2.2 donor engagements).

Captures every donor-facing interaction (call, pitch, conference, scoping).
Separate from Meeting Log because an engagement may have no linked RFP.
Quarterly target: 2–4 engagements per quarter.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from core import dropdowns
from db.supabase_client import get_client

# auth handled by wrapper page
user = st.session_state["app_user"]
role = user.get("role", "collaborator")
can_edit = role in ("super_user", "admin", "reviewer", "collaborator")  # any logged-in user can log engagements
sb = get_client()

st.title("Review Engagements with Donors")
st.caption(
    "Donor-facing interactions: calls, pitches, conferences, scoping conversations. "
    "Link to an RFP only when the engagement is directly about that opportunity."
)


# -----------------------------------------------------------------------------
# Data fetch
# -----------------------------------------------------------------------------
@st.cache_data(ttl=30)
def _fetch() -> pd.DataFrame:
    res = (
        get_client()
        .table("engagement_logs")
        .select("*")
        .order("engagement_date", desc=True)
        .execute()
    )
    df = pd.DataFrame(res.data or [])
    if not df.empty:
        df["engagement_date"] = pd.to_datetime(df["engagement_date"], errors="coerce")
    return df


df = _fetch()


# -----------------------------------------------------------------------------
# Quarterly KPI strip
# -----------------------------------------------------------------------------
today = date.today()
year = today.year
quarter = (today.month - 1) // 3 + 1
q_start = date(year, (quarter - 1) * 3 + 1, 1)

q_count = 0
if not df.empty:
    q_count = int((df["engagement_date"] >= pd.Timestamp(q_start)).sum())

target_low, target_high = 2, 4
if q_count < target_low:
    status_text, status_color = "Below target", "#fde2e2"
elif q_count <= target_high:
    status_text, status_color = "On track", "#dcf5e3"
else:
    status_text, status_color = "Exceeding", "#dcf5e3"

k1, k2, k3 = st.columns(3)
k1.metric(f"Engagements this quarter (Q{quarter})", q_count)
k2.metric("Quarterly target", f"{target_low}–{target_high}")
k3.markdown(
    f"<div style='background:{status_color};padding:14px 18px;border-radius:6px;"
    f"text-align:center;font-weight:600;margin-top:8px'>{status_text}</div>",
    unsafe_allow_html=True,
)
st.divider()


# -----------------------------------------------------------------------------
# Entry form
# -----------------------------------------------------------------------------
team = dropdowns.get("team_members")
types_ = dropdowns.get("engagement_types")
formats_ = dropdowns.get("engagement_formats")
donors_list = dropdowns.get("donors")

rfps = (
    sb.table("rfp_submissions")
    .select("uid,opportunity_title")
    .eq("is_duplicate", False)
    .order("submitted_at", desc=True)
    .limit(500)
    .execute()
    .data
    or []
)
rfp_options = {"(none)": None}
for r in rfps:
    rfp_options[f"{r['uid']} — {(r.get('opportunity_title') or '')[:70]}"] = r["uid"]

with st.form("new_engagement", clear_on_submit=True):
    st.subheader("Log an engagement")
    c1, c2, c3 = st.columns(3)
    e_date = c1.date_input("Engagement date *", value=today)
    e_type = c2.selectbox("Type *", ["—"] + types_)
    e_format = c3.selectbox("Format", ["—"] + formats_)

    c4, c5 = st.columns(2)
    donor = c4.selectbox("Donor *", ["—"] + donors_list)
    donor_other = ""
    if donor == "Other":
        donor_other = c4.text_input("Specify donor *")
    internal_lead = c5.selectbox("Internal lead *", ["—"] + team)
    internal_lead_other = ""
    if internal_lead == "Other":
        internal_lead_other = c5.text_input("Specify internal lead *")

    donor_contacts = st.text_input("Donor contact(s)")
    purpose = st.text_area("Purpose", height=70)
    outcome = st.text_area("Outcome / follow-up", height=70)
    linked_uid = rfp_options[st.selectbox("Linked RFP (optional)", list(rfp_options.keys()))]

    submit_pressed = st.form_submit_button("💾 Save engagement", type="primary")

if submit_pressed:
    errors = []
    if e_type == "—":
        errors.append("Type is required.")
    final_donor = donor_other.strip() if donor == "Other" else (None if donor == "—" else donor)
    if not final_donor:
        errors.append("Donor is required.")
    final_lead = internal_lead_other.strip() if internal_lead == "Other" else (None if internal_lead == "—" else internal_lead)
    if not final_lead:
        errors.append("Internal lead is required.")
    if errors:
        st.error("Please fix:\n\n- " + "\n- ".join(errors))
    else:
        sb.table("engagement_logs").insert(
            {
                "engagement_date": e_date.isoformat(),
                "donor": final_donor,
                "engagement_type": e_type,
                "format": None if e_format == "—" else e_format,
                "internal_lead": final_lead,
                "donor_contacts": donor_contacts.strip() or None,
                "purpose": purpose.strip() or None,
                "outcome": outcome.strip() or None,
                "linked_rfp_uid": linked_uid,
                "created_by": user.get("email"),
            }
        ).execute()
        st.cache_data.clear()
        st.success("Engagement logged.")
        st.rerun()

st.divider()


# -----------------------------------------------------------------------------
# Read view: table with filters
# -----------------------------------------------------------------------------
st.subheader("Recent engagements")
if df.empty:
    st.info("No engagements logged yet.")
    st.stop()

with st.expander("Filters", expanded=False):
    fc1, fc2, fc3 = st.columns(3)
    f_type = fc1.multiselect("Type", sorted(df["engagement_type"].dropna().unique().tolist()))
    f_donor = fc2.multiselect("Donor", sorted(df["donor"].dropna().unique().tolist()))
    f_lead = fc3.multiselect("Internal lead", sorted(df["internal_lead"].dropna().unique().tolist()))

mask = pd.Series(True, index=df.index)
if f_type:
    mask &= df["engagement_type"].isin(f_type)
if f_donor:
    mask &= df["donor"].isin(f_donor)
if f_lead:
    mask &= df["internal_lead"].isin(f_lead)

show = df[mask].copy()
show["engagement_date"] = show["engagement_date"].dt.date
st.dataframe(
    show[
        ["engagement_date", "donor", "engagement_type", "format", "internal_lead",
         "donor_contacts", "purpose", "outcome", "linked_rfp_uid"]
    ],
    use_container_width=True,
    hide_index=True,
    column_config={
        "engagement_date": st.column_config.DateColumn("Date"),
        "engagement_type": st.column_config.TextColumn("Type"),
    },
)
