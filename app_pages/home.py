"""Home dashboard — welcome, live KPIs, quick-start cards, how-to guide.

Rendered as the default page by the st.navigation router in `App.py`.
Auth + global header already ran in the router, so this file is
content-only (it reads the logged-in user from session_state).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import excel_sync
from core.records import clean_df
from db.supabase_client import get_client

# Home-specific block-container padding only — global theme (headings,
# .quickcard, metric tiles, buttons) lives in core/app_header._GLOBAL_CSS
# which is injected by render_app_header() in the router.
st.markdown(
    "<style>.block-container { padding-top: 1.5rem; }</style>",
    unsafe_allow_html=True,
)

user = st.session_state["app_user"]
role = user.get("role", "collaborator")
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

# Title + Submit-RFP button on the same row (button top-right).
_title_col, _btn_col = st.columns([5, 1])
with _title_col:
    st.title(f"Welcome, {display_name.split()[0]} 👋")
with _btn_col:
    # Vertical spacer aligns the button roughly with the title baseline.
    st.markdown("<div style='height: 0.8rem'></div>", unsafe_allow_html=True)
    # Opens the standalone Submit page in a NEW browser tab (target=_blank) so the
    # dashboard stays put. The relative href resolves to the page's url_path
    # (App.py: url_path="submit-new-rfp") on both local and Streamlit Cloud.
    st.markdown(
        "<a href='submit-new-rfp' target='_blank' rel='noopener' "
        "title='Capture an opportunity you found outside the Friday scan — opens "
        "in a new tab. Submitted immediately; dedup happens at display time.' "
        "style='display:block;width:100%;box-sizing:border-box;text-align:center;"
        "background:#00703C;color:#ffffff;padding:0.55rem 0.75rem;border-radius:0.5rem;"
        "text-decoration:none;font-weight:600;font-size:0.88rem;line-height:1.25;'>"
        "📝 Submit Discovered RFP</a>",
        unsafe_allow_html=True,
    )


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

    df = clean_df(pd.DataFrame(rfps))
    total_submissions = int(len(df))
    if not df.empty:
        dup_count = int(df["is_duplicate"].fillna(False).sum())
        unique = df[~df["is_duplicate"].fillna(False)].copy()
        unique["_d"] = unique["call_submission_deadline"].apply(_to_date)
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
    ("Pipelines", "app_pages/pipelines.py", "📚", "Screen → Review → Tracking → Summary",
     "Friday-scan + manual submissions through the full lifecycle: 4 tabs (Screen, Review, Tracking, Summary)."),
    ("Grants", "app_pages/grants.py", "💼", "Active Grants",
     "Grants under donor review or already awarded, with reporting deadlines."),
    ("Actions", "app_pages/actions.py", "🗒️", "Team check-ins + Engagements",
     "Three tabs — weekly meeting notes, donor engagement touchpoints, and pending follow-ups."),
    ("Report", "app_pages/report.py", "📊", "KPI dashboard",
     "Activity dashboard tracing the full pipeline — search → triage → reviews → engagements → grants secured."),
    ("Organization", "app_pages/organization.py", "🏢", "Organization",
     "Everything about your organization — profile, bid-fitness, eligibility, partners, team."),
]
if role in ("super_user", "admin"):
    CARDS.append(("Settings", "app_pages/admin.py", "⚙️", "Setup · Users · Data · Sources · Scans",
                  "Org profile, year setting, Excel sync, currency rates, Manage Users + User Access, "
                  "the full Records backend, donor sources, manual scans. Also in the 👤 menu (top-right)."))

cols = st.columns(3)
for i, (page, path, icon, headline, body) in enumerate(CARDS):
    with cols[i % 3]:
        st.markdown(
            f"<div class='quickcard'><h4>{icon} &nbsp; {headline}</h4><p>{body}</p></div>",
            unsafe_allow_html=True,
        )
        if st.button(f"Open {page}", key=f"qs_{page}", width='stretch'):
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
        3. **Monday 09:00** — the team call. Open **Screenings**, walk through each
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

        **Need help?** The **Data** page (under Admin) is the master record — every
        column in the old Excel screener lives there and can be edited via the Edit
        modal.
        """
    )
