"""RFPIS — RFP Intelligence System · entry page.

Login gate + role-aware welcome dashboard with real KPIs and a quick-start
guide. Page-level routing is handled by Streamlit's pages/ directory.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from auth.authenticator import login_gate
from core import excel_sync
from core.settings import get_org_name
from db.supabase_client import get_client
from views.submit_form import render_submit_form

st.set_page_config(
    page_title="RFP Intelligence System - RFPIS",
    page_icon="🛈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Home-specific block-container padding only — global theme (headings,
# .quickcard, metric tiles, buttons) lives in core/app_header._GLOBAL_CSS
# which is injected by render_app_header() right after login.
st.markdown(
    "<style>.block-container { padding-top: 1.5rem; }</style>",
    unsafe_allow_html=True,
)

user = login_gate()
if not user:
    st.stop()

# Persistent top-right brand header (logo + RFPIS v1.0).
from core.app_header import render_app_header  # noqa: E402
render_app_header()

role = user.get("role", "collaborator")
st.session_state["chai_role"] = role
display_name = user.get("name") or user.get("email") or "there"

# ----- Auto-sync from Excel when the workbook is newer than the last sync -----
# Cached so it only fires once per actual file change per session.
@st.cache_data(ttl=30)
def _auto_sync_check(stamp: float) -> dict | None:
    """`stamp` is the workbook mtime; including it in the cache key means a new
    save invalidates the cache and we run the sync again."""
    return excel_sync.sync(updated_by=user.get("email"))


_pending = excel_sync.needs_sync()
if _pending:
    try:
        with st.spinner(f"Syncing from {_pending.name}..."):
            _result = _auto_sync_check(_pending.stat().st_mtime)
        if _result and _result.get("ok"):
            st.toast("✓ Excel auto-synced", icon="🔄")
        elif _result:
            # Surface the actual failure. subprocess returncode + stderr/stdout
            # are the most useful signals when the file is locked or the
            # migration script raised.
            err_msg = _result.get("error") or ""
            stderr_tail = (_result.get("stderr") or "").strip().splitlines()[-3:] if _result.get("stderr") else []
            stdout_tail = (_result.get("stdout") or "").strip().splitlines()[-3:] if _result.get("stdout") else []
            rc = _result.get("returncode")
            with st.expander(
                f"⚠ Excel auto-sync failed (exit {rc}) — click for details · "
                "manual retry: Admin > Settings > Sync now",
                expanded=False,
            ):
                if err_msg:
                    st.error(err_msg)
                if stderr_tail:
                    st.markdown("**stderr (last 3 lines):**")
                    st.code("\n".join(stderr_tail), language="text")
                if stdout_tail:
                    st.markdown("**stdout (last 3 lines):**")
                    st.code("\n".join(stdout_tail), language="text")
                st.caption(
                    "Common causes on startup: OneDrive is still syncing the "
                    "file (Windows file lock), or the migration timed out. "
                    "Manual sync usually succeeds a few seconds later."
                )
    except Exception as exc:
        st.warning(f"Auto-sync error: {exc}")

# ---- Submit-RFP modal (Streamlit ≥1.32 @st.dialog) ----
# The form lives in views/submit_form.py so the same code renders here in
# the modal AND on the standalone Submit page. key_prefix keeps widget
# IDs unique so both can coexist in one session.
@st.dialog("Submit a new RFP", width="large")
def _submit_rfp_modal():
    render_submit_form(
        user,
        key_prefix="home_modal",
        on_success=lambda row: st.rerun(),
    )


# Title + Submit-RFP button on the same row (button top-right).
_title_col, _btn_col = st.columns([5, 1])
with _title_col:
    st.title(f"Welcome, {display_name.split()[0]} 👋")
    st.caption(
        "Weekly RFP discovery, eligibility scoring, and decision pipeline for "
        f"the **{get_org_name()}**."
    )
with _btn_col:
    # Vertical spacer aligns the button roughly with the title baseline.
    st.markdown("<div style='height: 0.8rem'></div>", unsafe_allow_html=True)
    if st.button("📝 Submit RFP", type="primary",
                 use_container_width=True, key="home_submit_rfp_btn",
                 help="Capture an opportunity you found outside the Friday scan. "
                      "Opens a modal; no duplicate-check gate — submitted "
                      "immediately, dedup happens at display time."):
        _submit_rfp_modal()


# -----------------------------------------------------------------------------
# Live KPIs
# -----------------------------------------------------------------------------
@st.cache_data(ttl=60)
def _kpis() -> dict:
    sbc = get_client()
    rfps = sbc.table("rfp_submissions").select(
        "uid,decision,donor_decision,progress_status,"
        "submission_deadline,is_duplicate"
    ).execute().data or []

    today = date.today()
    in_two_weeks = today + timedelta(days=14)

    def _to_date(v):
        """Always returns a python date or None. Never NaT/Timestamp."""
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        try:
            ts = pd.to_datetime(v, errors="coerce")
            if pd.isna(ts):
                return None
            return ts.date()
        except Exception:
            return None

    def _is_overdue(d):
        return d is not None and d < today

    def _is_due_soon(d):
        return d is not None and today <= d <= in_two_weeks

    def _in_this_year(d):
        return d is not None and date(today.year, 1, 1) <= d <= today

    df = pd.DataFrame(rfps)
    total_submissions = int(len(df))
    if not df.empty:
        dup_count = int(df["is_duplicate"].fillna(False).sum())
        unique = df[~df["is_duplicate"].fillna(False)].copy()
        unique["_d"] = unique["submission_deadline"].apply(_to_date)
        dec_lower = unique["decision"].fillna("").astype(str).str.strip().str.lower()
        total_unique = int(len(unique))
        proceed = int(dec_lower.str.startswith("proceed").sum())
        park = int(dec_lower.eq("park").sum())
        decline = int(dec_lower.eq("decline").sum())
        # Urgency counts apply only to Proceed RFPs (Park/Decline don't have actionable deadlines)
        proceed_df = unique[dec_lower.str.startswith("proceed").to_numpy()]
        # Element-wise compares — pd.NaT in vectorized "< today" crashes
        overdue = int(proceed_df["_d"].apply(_is_overdue).sum())
        due_soon = int(proceed_df["_d"].apply(_is_due_soon).sum())

        # Submitted = Progress Status = Completed (deduplicated set).
        # Consistent with Proceed/Park/Decline which all sum to Total Unique.
        ps_lower = unique["progress_status"].fillna("").astype(str).str.strip().str.lower()
        submitted_count = int(ps_lower.eq("completed").sum())

        # Awarded Grants = donor_decision = Approved (deduplicated set).
        dd_lower = unique["donor_decision"].fillna("").astype(str).str.strip().str.lower()
        awarded_grants = int(dd_lower.eq("approved").sum())
    else:
        dup_count = total_unique = proceed = park = decline = overdue = due_soon = 0
        submitted_count = awarded_grants = 0

    return {
        "total_unique": total_unique,
        "total_found": total_submissions,
        "duplicates": dup_count,
        "proceed": proceed,
        "park": park,
        "decline": decline,
        "overdue": overdue,
        "due_soon": due_soon,
        "submitted": submitted_count,
        "awarded_grants": awarded_grants,
    }


try:
    k = _kpis()
except Exception as exc:
    st.warning(f"Couldn't load live KPIs: {exc}")
    k = {"total_unique": 0, "total_found": 0, "duplicates": 0,
         "proceed": 0, "park": 0, "decline": 0,
         "overdue": 0, "due_soon": 0,
         "submitted": 0, "awarded_grants": 0}



# Row 1 — pipeline status (deduplicated)
r1c1, r1c2, r1c3, r1c4 = st.columns(4)
r1c1.metric(
    "Total Unique RFPs",
    k["total_unique"],
    delta=(f"{k['total_found']} found · {k['duplicates']} duplicate"
           if k["duplicates"] else f"{k['total_found']} found"),
    delta_color="off",
)
r1c2.metric("Proceed", k["proceed"])
r1c3.metric("Park", k["park"])
r1c4.metric("Decline", k["decline"])

# Row 2 — urgency + grants. Flow: Due → Submitted → Past deadline → Awarded
r2c1, r2c2, r2c3, r2c4 = st.columns(4)
r2c1.metric("Due in 14 days", k["due_soon"])
r2c2.metric("Submitted", k["submitted"])
r2c3.metric("Past deadline", k["overdue"], delta_color="inverse")
r2c4.metric("Awarded Grants", k["awarded_grants"])

st.divider()


# -----------------------------------------------------------------------------
# Role-aware quick-start cards
# -----------------------------------------------------------------------------
st.subheader("Where to start")

CARDS = [
    ("Pipeline", "pages/01_Pipeline.py", "📚", "Screen → Review → Tracking → Summary",
     "Friday-scan + manual submissions through the full lifecycle: 4 tabs (Screen, Review, Tracking, Summary)."),
    ("Grants", "pages/02_Grants.py", "💼", "Active Grants",
     "Grants under donor review or already awarded, with reporting deadlines."),
    ("Activity", "pages/03_Activity.py", "🗒", "Team check-ins + Engagements",
     "Two tabs — weekly meeting notes and donor engagement touchpoints."),
    ("Report", "pages/04_Report.py", "📊", "KPI dashboard",
     "Activity dashboard tracing the full pipeline — search → triage → reviews → engagements → grants secured."),
    ("User", "pages/05_User.py", "👤", "Profile · Password · Access",
     "Manage your profile, change your password, see what you can access. Admins also get a Manage Users tab."),
]
if role == "admin":
    CARDS.append(("Admin", "pages/06_Admin.py", "⚙", "Settings · Data · Donor Sources · Scans",
                  "Org profile, year setting, Excel sync, currency rates, the full Data backend "
                  "(incl. RFP Records), donor sources, manual scans."))

cols = st.columns(3)
for i, (page, path, icon, headline, body) in enumerate(CARDS):
    with cols[i % 3]:
        st.markdown(
            f"<div class='quickcard'><h4>{icon} &nbsp; {headline}</h4><p>{body}</p></div>",
            unsafe_allow_html=True,
        )
        if st.button(f"Open {page}", key=f"qs_{page}", use_container_width=True):
            st.switch_page(path)


# -----------------------------------------------------------------------------
# How to use guide
# -----------------------------------------------------------------------------
st.divider()
with st.expander("📖 How to use this app", expanded=False):
    st.markdown(
        """
        **Weekly rhythm**
        1. **Friday morning** — the automated scanner pulls new opportunities from the
           configured donor sources and emails the team a digest. New rows appear in
           **Screenings** with an auto-recommendation (Proceed / Park / Decline).
        2. **Friday → Sunday** — anyone who finds an RFP outside the scan submits it
           via **Submit**. The form runs duplicate detection, computes an alignment
           score, and tags it to next Monday's review week.
        3. **Monday 09:00** — the BDT call. Open **Screenings**, walk through each
           opportunity. For deep discussion of a single RFP, open **Review** and use
           the badge grid. Capture meeting notes in **Meeting Log**, linked to the
           RFP under discussion. Decisions saved here override the auto-recommendation
           and are timestamped.
        4. **Mid-week** — proposal teams work the **Tracking** pipeline. Update stage,
           progress, next action, deadlines. Log donor calls in **Engagement Log**.

        **Roles**
        - **Admin** — everything, plus user management, scan triggers, donor source curation.
        - **Reviewer** — confirm decisions, edit RFPs, but no delete.
        - **Collaborator** — submit RFPs and read dashboards.

        **Need help?** Most pages have a one-line caption at the top explaining what
        they're for. The **Data** page is the master record — every column in the
        old Excel screener lives there and can be edited via the Edit modal.
        """
    )
