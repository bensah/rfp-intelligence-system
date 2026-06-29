"""Page 6 — Meetings (team call log).

Monday team call notes. Per-week view: rota at top,
"Add a note" button opens a modal. Unresolved actions carry forward as a
proper table with headers on top and a Status toggle (Not Resolved ↔
Resolved). Notes for the selected week display below.
"""
from __future__ import annotations

from datetime import date, timedelta
from io import StringIO

import pandas as pd
import streamlit as st

from core import dropdowns, settings
from core.review_week import all_weeks_for_year, week_bounds
from core.records import clean_df
from db.supabase_client import get_client

# auth handled by wrapper page
user = st.session_state["app_user"]
role = user.get("role", "collaborator")
can_edit = role in ("super_user", "admin", "reviewer")
sb = get_client()

_TITLE_COL, _BTN_COL = st.columns([5, 1])
with _TITLE_COL:
    st.subheader("Weekly Meeting Logs")
# Add-note button (rendered in the right column AFTER add_note_dialog is
# defined further down — we reserve the slot here, fill it after definition).
_ADD_NOTE_SLOT = _BTN_COL.empty()

# Inject CSS for the Status toggle buttons. Streamlit ≥1.36 adds a
# `.st-key-<key>` class to each widget's wrapper. We cover both `st-key-`
# (current) and `stKey-` (older) just in case the version differs.
st.markdown(
    """
    <style>
      [class*="st-key-status_no_"] button,
      [class*="stKey-status_no_"] button {
        background: #f8d7da !important;
        color: #842029 !important;
        border: 1px solid #f5c2c7 !important;
      }
      [class*="st-key-status_no_"] button:hover,
      [class*="stKey-status_no_"] button:hover {
        background: #f5c2c7 !important;
        color: #58151c !important;
        border-color: #ea868f !important;
      }
      [class*="st-key-status_no_"] button p,
      [class*="stKey-status_no_"] button p {
        color: #842029 !important;
      }
      [class*="st-key-status_yes_"] button,
      [class*="stKey-status_yes_"] button {
        background: #d1e7dd !important;
        color: #0f5132 !important;
        border: 1px solid #badbcc !important;
      }
      [class*="st-key-status_yes_"] button:hover,
      [class*="stKey-status_yes_"] button:hover {
        background: #badbcc !important;
        color: #0a3622 !important;
        border-color: #75b798 !important;
      }
      [class*="st-key-status_yes_"] button p,
      [class*="stKey-status_yes_"] button p {
        color: #0f5132 !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Week selector
# -----------------------------------------------------------------------------
year = settings.get_year()
all_weeks = all_weeks_for_year(year)


def _monday(label: str) -> date:
    week_num = int(label.split(" ")[1])
    jan4 = date(year, 1, 4)
    mon, _ = week_bounds(jan4)
    return mon + timedelta(days=(week_num - mon.isocalendar().week) * 7)


this_week = next(
    (w for w in all_weeks if _monday(w) <= date.today() <= _monday(w) + timedelta(days=6)),
    all_weeks[0],
)
sel_week = st.selectbox("Review week", all_weeks, index=all_weeks.index(this_week), key="meeting_logs_week")
mon_date = _monday(sel_week)


# -----------------------------------------------------------------------------
# Rota: who's note-taker / presenter / chair this week
# -----------------------------------------------------------------------------
def _short_name(name: str | None) -> str:
    """Shorten a 3-token name like 'First Middle Last' → 'First Middle'.
    Single- and two-token names stay as-is. Helps long names not get
    truncated in metric tile labels."""
    if not name:
        return "—"
    parts = str(name).strip().split()
    return " ".join(parts[:2]) if len(parts) > 2 else name


rota = (
    sb.table("meeting_schedule").select("*").eq("call_date", mon_date.isoformat()).execute().data
)
rota_row = rota[0] if rota else None

r1, r2, r3, r4 = st.columns(4)
r1.metric("Meeting date", mon_date.strftime("%a %d %b %Y"))
r2.metric("Note-taker", _short_name((rota_row or {}).get("note_taker")))
r3.metric("RFP presenter", _short_name((rota_row or {}).get("rfp_presenter")))
r4.metric("Chair", _short_name((rota_row or {}).get("meeting_orgr")))


# -----------------------------------------------------------------------------
# Pre-fetch options used by both the modal and the table below
# -----------------------------------------------------------------------------
proceed_rfps = (
    sb.table("rfp_submissions")
    .select("uid,opportunity_title,funding_agency")
    .in_("decision", ["Proceed", "Proceed as sub"])
    .eq("is_duplicate", False)
    .order("call_submission_deadline")
    .execute()
    .data
    or []
)
rfp_options = {"(none)": None}
for r in proceed_rfps:
    label = f"{r['uid']} — {(r.get('opportunity_title') or '')[:70]}"
    rfp_options[label] = r["uid"]

team = dropdowns.get("team_members")


# -----------------------------------------------------------------------------
# Add-a-note modal (opened by a button at the top)
# -----------------------------------------------------------------------------
@st.dialog("Add a meeting note", width="large")
def add_note_dialog() -> None:
    st.caption("All fields are required.")
    # Drop the "(none)" option — Linked RFP is required.
    rfp_choices = [k for k in rfp_options.keys() if k != "(none)"]
    rfp_pick = st.selectbox(
        "Linked RFP (Proceed) *",
        rfp_choices,
        help="Only RFPs the team has agreed to pursue are shown.",
    )
    remarks = st.text_area("Issues *", height=90)
    actions = st.text_area("Actions / recommendations *", height=110)

    c_owner, c_due = st.columns([2, 1])
    owner = c_owner.selectbox("Owner *", ["—"] + team)
    default_due = mon_date + timedelta(days=7)
    due_date = c_due.date_input(
        "Due Date *",
        value=default_due,
        help="Defaults to the Monday following this meeting. Override if needed.",
    )

    bs, bc = st.columns([1, 1])
    save_clicked = bs.button("💾 Save note", type="primary", width='stretch',
                              disabled=not can_edit)
    cancel_clicked = bc.button("Cancel", width='stretch')

    if cancel_clicked:
        st.rerun()

    if save_clicked:
        errors: list[str] = []
        if not rfp_pick or rfp_options.get(rfp_pick) is None:
            errors.append("Linked RFP is required.")
        if not remarks.strip():
            errors.append("Issues is required.")
        if not actions.strip():
            errors.append("Actions / recommendations is required.")
        if owner == "—":
            errors.append("Owner is required.")
        if not isinstance(due_date, date):
            errors.append("Due Date is required.")
        if errors:
            st.error("Please fix the following:\n\n- " + "\n- ".join(errors))
            return

        sb.table("meeting_logs").insert(
            {
                "meeting_date": mon_date.isoformat(),
                "rfp_uid": rfp_options[rfp_pick],
                "remarks": remarks.strip(),
                "actions": actions.strip(),
                "owner": owner,
                "deadline": due_date.isoformat(),
                "created_by": user.get("email"),
            }
        ).execute()
        st.cache_data.clear()
        st.toast("Note saved", icon="✅")
        st.rerun()


# Render the button directly into the reserved top-right slot.
if _ADD_NOTE_SLOT.button(
    "➕ Add a note",
    type="primary",
    width='stretch',
    disabled=not can_edit,
    key="add_note_top",
):
    add_note_dialog()


# -----------------------------------------------------------------------------
# Notes for the selected week (filtered by meeting_date)
# -----------------------------------------------------------------------------
@st.cache_data(ttl=15)
def _fetch_notes(meeting_date: str) -> pd.DataFrame:
    res = (
        get_client()
        .table("meeting_logs")
        .select("*")
        .eq("meeting_date", meeting_date)
        .order("created_at")
        .execute()
    )
    return clean_df(pd.DataFrame(res.data or []))


notes = _fetch_notes(mon_date.isoformat())


def _linked_label(n: dict) -> str:
    """Prefer the human-readable `Donor - Title` over the cryptic UID.

    donor_title comes from the Excel Donor_Title column (which is already in
    "Acronym - Title" format from the workbook dropdown). The UID is only
    shown as a last resort when donor_title is missing — admins can still
    look up the UID via Admin → Data → Meeting Logs."""
    donor = (n.get("donor_title") or "").strip()
    if donor and donor.lower() != "nan":
        return donor[:80] + ("…" if len(donor) > 80 else "")
    uid = (n.get("rfp_uid") or "").strip()
    if uid and uid.lower() != "nan":
        return f"`{uid}`"
    return "—"


st.subheader(f"Notes for {sel_week}  ·  {len(notes)} record(s)")

if notes.empty:
    st.info("No notes captured for this week. Click **➕ Add a note** above to start.")
else:
    # One unified table — headers on top, then rows. No grouping, no
    # is_resolved filter, so toggling Status keeps the row visible.
    h = st.columns([2, 3, 3, 1.3, 1.4, 1.4])
    h[0].markdown("**Linked RFP**")
    h[1].markdown("**Issues**")
    h[2].markdown("**Action**")
    h[3].markdown("**Owner**")
    h[4].markdown("**Due Date**")
    h[5].markdown("**Status**")
    st.markdown("<hr style='margin:4px 0 8px'/>", unsafe_allow_html=True)

    for _, n in notes.iterrows():
        row = st.columns([2, 3, 3, 1.3, 1.4, 1.4])
        row[0].markdown(_linked_label(n.to_dict()))
        row[1].markdown(n.get("remarks") or "—")
        row[2].markdown(n.get("actions") or "—")
        row[3].markdown(_short_name(n.get("owner")))
        row[4].markdown(str(n.get("deadline") or "—"))

        resolved = bool(n.get("is_resolved"))
        label = "✓ Resolved" if resolved else "Not Resolved"
        # Key prefix `status_yes_` / `status_no_` lets our CSS target the
        # button's wrapper (.stKey-status_yes_<id>) for tint colors.
        key_prefix = "status_yes_" if resolved else "status_no_"
        if row[5].button(label, key=f"{key_prefix}{n['id']}",
                          width='stretch',
                          disabled=not can_edit):
            sb.table("meeting_logs").update(
                {"is_resolved": not resolved}
            ).eq("id", n["id"]).execute()
            st.cache_data.clear()
            st.rerun()

    # CSV export
    buf = StringIO()
    notes.to_csv(buf, index=False)
    st.download_button(
        f"⬇ Download {sel_week} notes as CSV",
        data=buf.getvalue(),
        file_name=f"meeting_log_{mon_date.isoformat()}.csv",
        mime="text/csv",
    )


