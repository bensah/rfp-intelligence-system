"""Page 8 — Data (line-list of every RFP, auto + manual).

UX flow:
  1. Filter the table.
  2. Pick a page size (10 / 25 / 50 / 100 / 1000).
  3. Click a row in the read-only table to select it.
  4. Three action buttons appear under the table: Edit · Delete · Share.
  5. Each opens a modal overlay (st.dialog).

Edit modal organises ~60 fields into tabs. Delete modal is the overlay
confirmation. Share modal: Download CSV / Send via Resend / Copy markdown.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from io import StringIO

import pandas as pd
import streamlit as st

from core import dropdowns
from core.mailer import MailerNotConfigured, send_email
from core.scorer import score_submission
from db.supabase_client import get_client

# auth handled by wrapper page
user = st.session_state["app_user"]
role = user.get("role", "collaborator")
sb = get_client()

is_admin = role in ("super_user", "admin")
can_edit = role in ("super_user", "admin", "reviewer")

st.subheader("Records — All RFPs")
st.caption(
    "Every submission (auto-scanned + manually captured). "
    "Click a row to select, then use the Edit / Delete / Share buttons below the table. "
    f"You are signed in as **{role}** — "
    f"{'full edit + delete' if is_admin else ('edit only' if can_edit else 'read-only')}."
)


# -----------------------------------------------------------------------------
# Data fetch
# -----------------------------------------------------------------------------
@st.cache_data(ttl=30)
def _fetch_all() -> pd.DataFrame:
    # Order by created_at (true INSERTION time, DB-defaulted now() on insert and
    # never touched by Excel sync or updates) so the list reads newest-added →
    # oldest. A freshly-synced, non-duplicate RFP gets the latest created_at and
    # lands on top; re-synced existing rows keep their place. submitted_at is a
    # secondary tiebreaker for same-batch inserts.
    res = (
        get_client()
        .table("rfp_submissions")
        .select("*")
        .order("created_at", desc=True)
        .order("submitted_at", desc=True)
        .execute()
    )
    return pd.DataFrame(res.data or [])


df = _fetch_all()
if df.empty:
    st.info("No RFPs yet. Submit one via the Submit page or trigger a scan from Admin.")
    st.stop()


# -----------------------------------------------------------------------------
# Filters — one widget per filterable column. Open by default.
# -----------------------------------------------------------------------------
# Compute Prob tier per row (same logic as Highlights Section B)
from core.pipeline import prob_tier as _prob_tier_shared
df["_prob"] = df["alignment_score"].apply(lambda s: _prob_tier_shared(s, short=True))

with st.expander("Filters", expanded=True):
    fc1, fc2, fc3, fc4, fc5 = st.columns(5)
    f_source = fc1.multiselect("Source", sorted(df["source"].dropna().unique().tolist()))
    f_dec = fc2.multiselect("Decision", sorted(df["decision"].dropna().unique().tolist()))
    f_prob = fc3.multiselect("Probability tier", ["High", "Medium", "Low"])
    f_progress = fc4.multiselect(
        "Progress status",
        sorted(df["progress_status"].dropna().unique().tolist()) if "progress_status" in df.columns else [],
    )
    f_donor_dec = fc5.multiselect(
        "Donor decision",
        sorted(df["donor_decision"].dropna().unique().tolist()) if "donor_decision" in df.columns else [],
    )

    gc1, gc2, gc3, gc4 = st.columns([2, 2, 2, 1])
    f_week = gc1.multiselect("Review week", sorted(df["review_week"].dropna().unique().tolist()))
    f_funder = gc2.multiselect("Funder", sorted(df["funding_agency"].dropna().unique().tolist()))
    text_q = gc3.text_input("Title contains")
    show_dups = gc4.checkbox("Include duplicates", value=False)

mask = pd.Series(True, index=df.index)
if f_source:
    mask &= df["source"].isin(f_source)
if f_dec:
    mask &= df["decision"].isin(f_dec)
if f_prob:
    mask &= df["_prob"].isin(f_prob)
if f_progress:
    mask &= df["progress_status"].isin(f_progress)
if f_donor_dec:
    mask &= df["donor_decision"].isin(f_donor_dec)
if f_week:
    mask &= df["review_week"].isin(f_week)
if f_funder:
    mask &= df["funding_agency"].isin(f_funder)
if text_q:
    mask &= df["opportunity_title"].fillna("").str.contains(text_q, case=False)
if not show_dups:
    mask &= ~df["is_duplicate"].fillna(False)

fdf = df[mask].copy().reset_index(drop=True)

# Live tier breakdown for the current filtered view
if not fdf.empty:
    tier_counts = fdf["_prob"].value_counts()
    h, m, l = (int(tier_counts.get(t, 0)) for t in ("High", "Medium", "Low"))
    st.caption(
        f"**Current view:** {len(fdf)} of {len(df)} rows · "
        f"Tier breakdown — **{h}** High · **{m}** Medium · **{l}** Low"
    )


# -----------------------------------------------------------------------------
# Pagination
# -----------------------------------------------------------------------------
pc1, pc2, pc3, pc4 = st.columns([1, 1, 4, 1])
page_size = pc1.selectbox("Per page", [10, 25, 50, 100, 1000], index=0, key="page_size")
total_pages = max(1, (len(fdf) + page_size - 1) // page_size)
page = pc2.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1, key="page_num")
pc3.markdown(
    f"<div style='padding-top: 28px; color: #555;'>Page <b>{page}</b> of <b>{total_pages}</b> · "
    f"<b>{len(fdf)}</b> matching row{'s' if len(fdf)!=1 else ''} (of {len(df)} total)</div>",
    unsafe_allow_html=True,
)
if pc4.button("🔄 Refresh", width='stretch'):
    st.cache_data.clear()
    st.rerun()

start = (page - 1) * page_size
end = start + page_size
view_df = fdf.iloc[start:end].reset_index(drop=True)


# -----------------------------------------------------------------------------
# Display table with single-row selection
# -----------------------------------------------------------------------------
DISPLAY = [
    "form_id",          # canonical identifier (was "uid" — same value, but
                        # form_id is the column name in the Excel workbook so
                        # Excel-round-trip stays consistent).
    "source",
    "search_date",
    "opportunity_title",
    "opportunity_link",   # clickable on first view (no audit mode needed)
    "funding_agency",
    "applicant_role",
    "submission_deadline",
    "estimated_value",
    "currency",
    "_value_usd",       # derived: live USD conversion "≈ $X (CUR Y @ date)"
    "alignment_score",
    "_prob",            # derived from alignment_score
    "auto_recommendation",
    "decision",
    "stage",
    "progress_status",
    "donor_decision",
    "is_duplicate",
]


from core.pipeline import prob_tier as _prob_tier_full


def _prob_tier(score):
    """Short label ("High" / "Medium" / "Low") — shared thresholds from core.pipeline."""
    return _prob_tier_full(score, short=True)


def _usd_display(amount, currency) -> str:
    """'≈ $X (CUR Y @ rate-date)' using a LIVE FX rate (core.fx, cached). Plain
    '$X' for USD; '' when there's no amount or the currency is unknown."""
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return ""
    if not amt:
        return ""
    cur = (str(currency).strip().split()[0].upper() if currency else "")
    if not cur or cur == "USD":
        return f"${amt:,.0f}"
    try:
        from core import fx
        r = fx.to_usd(amt, cur)
    except Exception:
        return ""
    usd = r.get("usd")
    if usd is None:
        return ""
    from datetime import date as _date
    rd = _date.today().isoformat()   # live rate = as of today
    return f"≈ ${usd:,.0f} ({cur} {amt:,.0f} @ {rd})"


# Column visibility toggle — when on, show every DB column for direct
# audit against the source workbook. Off = curated DISPLAY subset.
show_all_cols = st.checkbox(
    "Show all columns (audit mode)",
    value=False,
    help="On = every column from rfp_submissions; off = curated subset.",
)
if show_all_cols:
    # Audit mode shows every column from rfp_submissions except `uid`
    # (it's the same value as `form_id` — keeping both was visually
    # redundant and confused the Excel round-trip).
    visible_cols = [
        c for c in view_df.columns
        if not c.startswith("_") and c != "uid"
    ]
else:
    visible_cols = DISPLAY

table = view_df.reindex(columns=visible_cols).copy()
if "submission_deadline" in table:
    table["submission_deadline"] = pd.to_datetime(
        table["submission_deadline"], errors="coerce", format="ISO8601").dt.date
if "search_date" in table:
    # Actual scan date + time (from search_date), not the "Week 23" label.
    # format="ISO8601" is REQUIRED: auto/manual rows carry microseconds
    # (…:44.627419+00:00) while migration rows don't (…:02+00:00). Without it,
    # pandas≥2.0 infers ONE format from the first row and coerces the rest to
    # NaT — which is why some rows showed a blank Search date.
    table["search_date"] = pd.to_datetime(
        table["search_date"], errors="coerce", format="ISO8601")
if "alignment_score" in table and "_prob" in visible_cols:
    table["_prob"] = table["alignment_score"].apply(_prob_tier)
if "_value_usd" in table.columns:
    _ev = (view_df["estimated_value"] if "estimated_value" in view_df.columns
           else [None] * len(table))
    _cc = (view_df["currency"] if "currency" in view_df.columns
           else [None] * len(table))
    table["_value_usd"] = [_usd_display(a, c) for a, c in zip(list(_ev), list(_cc))]

# Backend columns are snake_case (consistent); the DISPLAY shows friendly,
# underscore-free labels. Curated key columns keep concise custom labels; every
# other column (incl. audit mode) is auto-labelled from its snake_case name so
# nothing shows as raw "snake_case".
_LABEL_ACRONYMS = {
    "uid": "UID", "rfp": "RFP", "id": "ID", "url": "URL", "usd": "USD",
    "ytd": "YTD", "hiv": "HIV", "tb": "TB", "mnch": "MNCH", "hss": "HSS",
    "ngo": "NGO", "must": "MUST", "prefer": "PREFER", "fx": "FX",
}


def _friendly_label(col: str) -> str:
    """snake_case -> 'Sentence case' label, acronyms upper, digits kept."""
    words = str(col).strip("_").replace("_", " ").split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in _LABEL_ACRONYMS:
            out.append(_LABEL_ACRONYMS[lw])
        elif w.isdigit():
            out.append(w)
        elif i == 0:
            out.append(w.capitalize())
        else:
            out.append(lw)
    return " ".join(out) or col


_explicit_cfg = {
    "form_id": st.column_config.TextColumn("UID", width="small"),
    "source": st.column_config.TextColumn("Source", width="small"),
    "search_date": st.column_config.DatetimeColumn(
        "Search date", format="YYYY-MM-DD HH:mm"),
    "opportunity_title": st.column_config.TextColumn("Title", width="large"),
    "opportunity_link": st.column_config.LinkColumn(
        "Link", display_text="Open ↗", width="small",
        help="Open the opportunity page in a new tab.",
    ),
    "funding_agency": st.column_config.TextColumn("Funder"),
    "applicant_role": st.column_config.TextColumn("Role", width="small"),
    "submission_deadline": st.column_config.DateColumn("Deadline"),
    "estimated_value": st.column_config.NumberColumn("Value", format="%.0f"),
    "currency": st.column_config.TextColumn("Currency", width="small"),
    "_value_usd": st.column_config.TextColumn("Value (USD)", width="medium"),
    "alignment_score": st.column_config.NumberColumn("Score", format="%.1f"),
    "_prob": st.column_config.TextColumn("Probability", width="small"),
    "auto_recommendation": st.column_config.TextColumn("Auto recommendation"),
    "decision": st.column_config.TextColumn("Decision"),
    "stage": st.column_config.TextColumn("Stage"),
    "progress_status": st.column_config.TextColumn("Progress status"),
    "donor_decision": st.column_config.TextColumn("Donor decision"),
    "is_duplicate": st.column_config.CheckboxColumn("Duplicate", width="small"),
    # DB column is `support_roles` (holds tech/finance/compliance roles); the
    # Excel header was renamed to just "Support", so show it that way too.
    "support_roles": st.column_config.TextColumn("Support"),
}
_col_cfg = dict(_explicit_cfg)
for _c in table.columns:
    if _c not in _col_cfg:
        _col_cfg[_c] = st.column_config.TextColumn(_friendly_label(_c))

event = st.dataframe(
    table,
    width='stretch',
    hide_index=True,
    selection_mode="multi-row",
    on_select="rerun",
    column_config=_col_cfg,
)

selected_rows = event.selection.rows if event and getattr(event, "selection", None) else []
if not selected_rows:
    st.info(
        "👆 Click one or more rows. Single select → Edit / Delete / Share. "
        "Multi-select → Delete / Share (batch)."
    )
    st.stop()

# Resolve every selected row's full record (we look up by UID against the
# unfiltered df so column subsets / pagination don't truncate fields).
selected_uids = [view_df.iloc[r]["uid"] for r in selected_rows]
selected_full_rows = [
    df[df["uid"] == uid].iloc[0].to_dict()
    for uid in selected_uids
    if not df[df["uid"] == uid].empty
]
is_multi = len(selected_full_rows) > 1

if is_multi:
    st.success(
        f"**{len(selected_full_rows)} RFPs selected.** Edit is disabled for "
        "multi-select. Use Delete or Share to act on all of them."
    )
else:
    selected_row = selected_full_rows[0]
    st.success(
        f"Selected: **{selected_row['uid']}** — {selected_row['opportunity_title'][:80]}"
    )

# -----------------------------------------------------------------------------
# Action buttons — Edit hidden when multiple rows are selected
# -----------------------------------------------------------------------------
if is_multi:
    ab2, ab3, _ = st.columns([1, 1, 6])
    edit_clicked = False
    blacklist_clicked = False
    delete_clicked = ab2.button(
        f"🗑 Delete {len(selected_full_rows)} RFPs",
        width='stretch', disabled=not is_admin,
        help=None if is_admin else "Admins only.",
    )
    share_clicked = ab3.button(
        f"📤 Share {len(selected_full_rows)} RFPs",
        width='stretch',
    )
else:
    ab1, ab2, ab3, ab4, ab5, ab6, _ = st.columns([1, 1, 1, 1.3, 0.7, 0.7, 2.3])
    edit_clicked = ab1.button("✏ Edit", width='stretch', disabled=not can_edit)
    delete_clicked = ab2.button(
        "🗑 Delete", width='stretch', disabled=not is_admin,
        help=None if is_admin else "Admins only.",
    )
    share_clicked = ab3.button("📤 Share", width='stretch')
    blacklist_clicked = ab4.button(
        "🚫 Blacklist", width='stretch', disabled=not is_admin,
        help="Block this source URL / section from future scans."
             if is_admin else "Admins only.",
    )
    # 👍 / 👎 — label this RFP good/bad as a training signal (ML Phase 1).
    good_clicked = ab5.button("👍", width='stretch',
                              help="Mark this a GOOD match (training signal).")
    bad_clicked = ab6.button("👎", width='stretch',
                             help="Mark this a BAD match (training signal).")
    if good_clicked or bad_clicked:
        try:
            from core import decision_log
            decision_log.log_feedback(
                selected_full_rows[0], "good" if good_clicked else "bad",
                by=user.get("email"))
            st.toast(("👍 Marked good" if good_clicked else "👎 Marked bad")
                     + " — thanks, this trains the scorer.", icon="🧠")
        except Exception as exc:
            st.warning(f"Couldn't record feedback: {exc}")


# -----------------------------------------------------------------------------
# Modal: Edit
# -----------------------------------------------------------------------------
@st.dialog("Edit RFP", width="large")
def edit_dialog(row: dict) -> None:
    st.markdown(f"**`{row['uid']}`** — {row.get('opportunity_title') if isinstance(row.get('opportunity_title'), str) else ''}")
    # Provenance line — who submitted this and when, so an editor can reach out.
    _sub_by = (row.get("submitted_by") or "").strip() or "—"
    _sub_email = (row.get("submitted_by_email") or "").strip()
    _sd = pd.to_datetime(row.get("search_date"), errors="coerce")
    _sd_str = _sd.strftime("%d %b %Y, %H:%M") if pd.notna(_sd) else "date unknown"
    _who = f"**{_sub_by}**" + (f" · {_sub_email}" if _sub_email else "")
    st.caption(f"📥 Submitted by {_who} · on {_sd_str}")
    tab_opp, tab_elig, tab_dec, tab_team, tab_award = st.tabs(
        ["Opportunity", "Eligibility", "Decision & Pipeline", "Team", "Award"]
    )

    def _date(v):
        if v is None or v == "" or (not isinstance(v, str) and pd.isna(v)):
            return None
        try:
            ts = pd.to_datetime(v, errors="coerce")
            if pd.isna(ts):
                return None
            return ts.date()
        except Exception:
            return None

    def _is_blank(v) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            return v == ""
        try:
            return bool(pd.isna(v))
        except (TypeError, ValueError):
            return False

    def _str(v) -> str:
        """Return '' for None / NaN / NaT so widgets don't render 'nan'."""
        return "" if _is_blank(v) else str(v)

    def _num(v) -> float:
        try:
            f = float(v)
            return 0.0 if pd.isna(f) else f
        except (TypeError, ValueError):
            return 0.0

    def _opt(label, key, options, current):
        opts = ["—"] + list(options)
        if _is_blank(current):
            current = None
        elif current not in opts:
            opts.append(current)
        idx = opts.index(current) if current in opts else 0
        return st.selectbox(label, opts, index=idx, key=f"edit_{key}_{row['uid']}")

    def _multi_options(predefined: list, current):
        """Merge predefined options with any stored values not yet in the list."""
        cur = list(current) if current else []
        extras = [v for v in cur if v not in predefined]
        return list(predefined) + extras

    def _multi_default(current):
        return list(current) if current else []

    def _val(v):
        return None if v in ("—", "", None) else v

    with tab_opp:
        c1, c2 = st.columns([2, 1])
        title_in = c1.text_input("Title *", value=_str(row.get("opportunity_title")), key=f"e_title_{row['uid']}")
        oppid_in = c2.text_input("Opportunity ID", value=_str(row.get("opportunity_id")), key=f"e_oid_{row['uid']}")
        funder_in = st.text_input("Funder *", value=_str(row.get("funding_agency")), key=f"e_fund_{row['uid']}")
        brief_in = st.text_area("Brief description", value=_str(row.get("brief_description")), height=110, key=f"e_brief_{row['uid']}")
        c3, c4, c5 = st.columns(3)
        dp = c3.date_input("Date posted", value=_date(row.get("date_posted")), key=f"e_dp_{row['uid']}")
        dl = c4.date_input("Submission deadline", value=_date(row.get("submission_deadline")), key=f"e_dl_{row['uid']}")
        ad = c5.date_input("Expected award", value=_date(row.get("expected_award_date")), key=f"e_ad_{row['uid']}")
        c6, c7, c8, c9 = st.columns(4)
        with c6:
            role_in = _opt("Applicant role", "role", dropdowns.get("applicant_role"), row.get("applicant_role"))
        with c7:
            win_in = _opt("Funding window", "win", dropdowns.get("funding_window"), row.get("funding_window"))
        with c8:
            tta_in = _opt("Time to award", "tta", dropdowns.get("time_to_award"), row.get("time_to_award"))
        with c9:
            fmt_in = _opt("Submission format", "fmt", dropdowns.get("submission_format"), row.get("submission_format"))
        c10, c11, c12 = st.columns([2, 1, 1])
        link_in = c10.text_input("Opportunity link", value=_str(row.get("opportunity_link")), key=f"e_link_{row['uid']}")
        val_in = c11.number_input("Estimated value", min_value=0.0, step=10000.0,
                                  value=_num(row.get("estimated_value")), key=f"e_val_{row['uid']}")
        cur_options = [c["code"] for c in dropdowns.load().get("currencies", [])]
        with c12:
            cur_in = _opt("Currency", "cur", cur_options, row.get("currency"))
        c13, c14 = st.columns(2)
        dur_in = c13.number_input("Duration (months)", min_value=0, step=1,
                                  value=int(_num(row.get("project_duration"))), key=f"e_dur_{row['uid']}")
        focus_in = c14.text_input("Focus theme", value=_str(row.get("focus_theme")), key=f"e_focus_{row['uid']}")
        geo_in = st.multiselect(
            "Geographic scope",
            _multi_options(dropdowns.get("geographic_scope"), row.get("geographic_scope")),
            default=_multi_default(row.get("geographic_scope")),
            key=f"e_geo_{row['uid']}",
        )
        prog_in = st.multiselect(
            "Program area(s)",
            _multi_options(dropdowns.get("program_areas"), row.get("program_area")),
            default=_multi_default(row.get("program_area")),
            key=f"e_prog_{row['uid']}",
        )

    def _coerce_elig_edit(v) -> str:
        try:
            if v is None or pd.isna(v):
                return "Partial"
        except (TypeError, ValueError):
            pass
        s = str(v).strip().lower()
        if s in ("yes", "y", "true", "1"):
            return "True"
        if s in ("partial", "p"):
            return "Partial"
        if s in ("no", "n", "false", "0"):
            return "False"
        return "Partial"

    def _elig(label, key, current):
        elig_list = list(dropdowns.get("eligibility_values"))
        coerced = _coerce_elig_edit(current)
        idx = elig_list.index(coerced) if coerced in elig_list else 0
        return st.selectbox(label, elig_list, index=idx, key=f"edit_{key}_{row['uid']}")

    with tab_elig:
        fcol, _spacer = st.columns([1, 3])
        with fcol:
            feas_in = _opt("Feasibility", "feas", dropdowns.get("feasibility"), row.get("feasibility"))
        gl, gr = st.columns(2)
        with gl:
            m1 = _elig("MUST 1 — Govt alignment", "m1", row.get("must_1_govt_alignment"))
            m2 = _elig("MUST 2 — Strategic fit", "m2", row.get("must_2_strategic_fit"))
            m3 = _elig("MUST 3 — Implementable", "m3", row.get("must_3_implementable"))
            m4 = _elig("MUST 4 — Compliant", "m4", row.get("must_4_compliant"))
            m5 = _elig("MUST 5 — Resourcing", "m5", row.get("must_5_resourcing"))
        with gr:
            p6 = _elig("PREFER 6 — Funding quality", "p6", row.get("prefer_6_funding_quality"))
            p7 = _elig("PREFER 7 — Monitorable", "p7", row.get("prefer_7_monitorable"))
            p8 = _elig("PREFER 8 — Partnership", "p8", row.get("prefer_8_partnership"))
            p9 = _elig("PREFER 9 — Scale", "p9", row.get("prefer_9_scale"))
        decline_in = st.radio(
            "Decline flags present?", ["No", "Yes"], horizontal=True,
            index=1 if row.get("decline_flags_present") else 0, key=f"e_decline_{row['uid']}",
        )
        risks_in = st.text_area("Key risks", value=_str(row.get("key_risks")), height=90, key=f"e_risks_{row['uid']}")
        st.caption(
            f"Stored alignment score: **{_num(row.get('alignment_score')):.1f}** · "
            f"auto-recommendation: **{_str(row.get('auto_recommendation')) or '—'}**. "
            "Save will recompute these from the values above."
        )

    with tab_dec:
        c1, c2 = st.columns([1, 3])
        with c1:
            dec_in = _opt("Decision", "dec", dropdowns.get("decisions"), row.get("decision"))
        rat_in = c2.text_area("Rationale", value=_str(row.get("decision_rationale")), height=70, key=f"e_rat_{row['uid']}")
        c_sub, c_stage, c_prog = st.columns([1, 1, 1])
        submissions_in = c_sub.number_input(
            "Submissions",
            min_value=1, step=1,
            value=int(_num(row.get("submissions") or 1)),
            key=f"e_subs_{row['uid']}",
            help="How many times this RFP was actually submitted to the donor. Default 1.",
        )
        with c_stage:
            stage_in = _opt("Stage", "stage", dropdowns.get("stages"), row.get("stage"))
        with c_prog:
            prog_status = _opt("Progress status", "ps", dropdowns.get("progress_status"), row.get("progress_status"))
        c5, c6 = st.columns(2)
        with c5:
            donor_dec = _opt("Donor decision", "dd", dropdowns.get("donor_decision"), row.get("donor_decision"))
        assigned = c6.text_input("Assigned to", value=_str(row.get("assigned_to")), key=f"e_assn_{row['uid']}")
        c7, c8 = st.columns(2)
        action_dl = c7.date_input("Action deadline", value=_date(row.get("action_deadline")), key=f"e_actdl_{row['uid']}")
        last_upd = c8.date_input("Last update", value=_date(row.get("last_update")), key=f"e_lu_{row['uid']}")
        next_a = st.text_input("Next action", value=_str(row.get("next_action")), key=f"e_na_{row['uid']}")
        remarks_in = st.text_area("Remarks", value=_str(row.get("remarks")), height=70, key=f"e_rem_{row['uid']}")

    with tab_team:
        team = list(dropdowns.get("team_members"))
        base_team = [m for m in team if m not in ("Other", "All")]
        # Names typed via "Other" → added to the roster on Save.
        _new_members: list[str] = []

        def _team_single(label, key, current):
            opts = ["—"] + base_team + ["Other"]
            cur = None if _is_blank(current) else current
            if cur and cur not in opts:
                opts.insert(1, cur)        # preserve a stored name off the roster
            sel = st.selectbox(label, opts,
                               index=opts.index(cur) if cur in opts else 0,
                               key=f"e_{key}_{row['uid']}")
            if sel == "Other":
                spec = (st.text_input(
                    f"↳ If other member, please specify ({label.lower()})",
                    key=f"e_{key}_oth_{row['uid']}") or "").strip()
                if spec:
                    _new_members.append(spec)
                return spec or None
            return None if sel in ("—", "") else sel

        def _team_multi(label, key, current, help=None):
            cur = (list(current) if isinstance(current, (list, tuple))
                   else [v.strip() for v in str(current).split(",") if v.strip()]
                   if current else [])
            extras = [v for v in cur if v not in base_team and v not in ("All", "Other")]
            opts = ["All"] + base_team + extras + ["Other"]
            sel = st.multiselect(label, opts,
                                 default=[d for d in cur if d in opts],
                                 key=f"e_{key}_{row['uid']}", help=help)
            chosen = [s for s in sel if s not in ("All", "Other")]
            if "All" in sel:                # "All" = the whole roster
                chosen = list(base_team)
            if "Other" in sel:
                raw = st.text_input(
                    f"↳ If other, please specify additional {label.lower()} "
                    f"(comma-separated)", key=f"e_{key}_oth_{row['uid']}") or ""
                typed = [v.strip() for v in raw.split(",") if v.strip()]
                _new_members.extend(typed)
                chosen = chosen + typed
            return chosen or None

        lead = _team_single("Proposal lead", "lead", row.get("proposal_lead"))
        contribs = _team_multi("Contributors", "contrib", row.get("contributors"))
        reviewers = _team_multi("Reviewers", "rev", row.get("reviewers"))
        support = _team_multi(
            "Support", "supp", row.get("support_roles"),
            help="e.g. tech / finance / compliance")

    with tab_award:
        c1, c2 = st.columns(2)
        doa = c1.date_input("Date of approval", value=_date(row.get("date_of_approval")), key=f"e_doa_{row['uid']}")
        secured = c2.number_input("Amount secured", min_value=0.0, step=10000.0,
                                  value=_num(row.get("amount_secured")), key=f"e_sec_{row['uid']}")
        c3, c4 = st.columns(2)
        with c3:
            cur_sec = _opt(
                "Currency secured", "cursec",
                [c["code"] for c in dropdowns.load().get("currencies", [])],
                row.get("currency_secured"),
            )
        po = c4.text_input("Donor program officer", value=_str(row.get("donor_program_officer")), key=f"e_po_{row['uid']}")
        c5, c6 = st.columns(2)
        ko = c5.date_input("Kick-off date", value=_date(row.get("kickoff_date")), key=f"e_ko_{row['uid']}")
        ns = c6.text_input("Next step", value=_str(row.get("next_step")), key=f"e_ns_{row['uid']}")

    st.divider()
    bs, bd, bc = st.columns([1, 1, 1])
    save_pressed = bs.button("💾 Save changes", type="primary", width='stretch')
    delete_pressed = bd.button("🗑 Delete this RFP", width='stretch', disabled=not is_admin)
    cancel_pressed = bc.button("Cancel", width='stretch')

    if cancel_pressed:
        st.rerun()

    if delete_pressed:
        sb.table("rfp_submissions").delete().eq("uid", row["uid"]).execute()
        st.cache_data.clear()
        st.toast(f"Deleted {row['uid']}", icon="🗑")
        st.rerun()

    if save_pressed:
        if not title_in.strip() or not funder_in.strip():
            st.error("Title and Funder are required.")
            return
        # Grow the team roster with any names typed via "Other".
        if _new_members:
            try:
                from core import settings as _set
                _set.set_team_members((_set.get_team_members() or []) + _new_members)
            except Exception:
                pass
        # Eligibility values always come back as True / Partial / False (no "—")
        vals = {
            "must_1_govt_alignment": m1,
            "must_2_strategic_fit": m2,
            "must_3_implementable": m3,
            "must_4_compliant": m4,
            "must_5_resourcing": m5,
            "prefer_6_funding_quality": p6,
            "prefer_7_monitorable": p7,
            "prefer_8_partnership": p8,
            "prefer_9_scale": p9,
        }
        decline_bool = decline_in == "Yes"
        align, rec = score_submission(vals, decline_bool)
        update = {
            "opportunity_title": title_in.strip(),
            "opportunity_id": _val(oppid_in.strip()),
            "funding_agency": funder_in.strip(),
            "brief_description": _val(brief_in),
            "date_posted": dp.isoformat() if isinstance(dp, date) else None,
            "submission_deadline": dl.isoformat() if isinstance(dl, date) else None,
            "expected_award_date": ad.isoformat() if isinstance(ad, date) else None,
            "applicant_role": _val(role_in),
            "funding_window": _val(win_in),
            "time_to_award": _val(tta_in),
            "submission_format": _val(fmt_in),
            "opportunity_link": _val(link_in),
            "estimated_value": float(val_in) if val_in else None,
            "currency": _val(cur_in),
            "project_duration": int(dur_in) if dur_in else None,
            "focus_theme": _val(focus_in),
            "geographic_scope": geo_in or None,
            "program_area": prog_in or None,
            "feasibility": _val(feas_in),
            **vals,
            "decline_flags_present": decline_bool,
            "key_risks": _val(risks_in),
            "alignment_score": align,
            "auto_recommendation": rec,
            "submissions": int(submissions_in) if submissions_in else 1,
            "decision": _val(dec_in),
            "decision_rationale": _val(rat_in),
            "stage": _val(stage_in),
            "progress_status": _val(prog_status),
            "donor_decision": _val(donor_dec),
            "assigned_to": _val(assigned),
            "action_deadline": action_dl.isoformat() if isinstance(action_dl, date) else None,
            "last_update": last_upd.isoformat() if isinstance(last_upd, date) else None,
            "next_action": _val(next_a),
            "remarks": _val(remarks_in),
            "proposal_lead": lead,
            "contributors": contribs,
            "reviewers": reviewers,
            "support_roles": (", ".join(support) if support else None),
            "date_of_approval": doa.isoformat() if isinstance(doa, date) else None,
            "amount_secured": float(secured) if secured else None,
            "currency_secured": _val(cur_sec),
            "donor_program_officer": _val(po),
            "kickoff_date": ko.isoformat() if isinstance(ko, date) else None,
            "next_step": _val(ns),
            "decision_overridden_by": user.get("email"),
            "decision_overridden_at": datetime.now(timezone.utc).isoformat(),
        }
        sb.table("rfp_submissions").update(update).eq("uid", row["uid"]).execute()
        # ML Phase 1/3 — log the human decision as a labeled signal on save.
        # Captures CONFIRMATIONS (reviewer kept the recommended decision) as
        # well as overrides — logging only changes would bias the model toward
        # disagreement. log_decision dedups per record so repeated saves of the
        # same decision don't pile up.
        _new_dec = _val(dec_in)
        if _new_dec:
            try:
                from core import decision_log
                decision_log.log_decision({**row, **update}, _new_dec,
                                          by=user.get("email"))
            except Exception:
                pass
        st.cache_data.clear()
        st.toast(f"Saved {row['uid']} · score {align:.1f} → {rec}", icon="✅")
        st.rerun()


# -----------------------------------------------------------------------------
# Modal: Delete (overlay confirmation)
# -----------------------------------------------------------------------------
@st.dialog("Confirm delete", width="medium")
def delete_dialog(rows: list[dict]) -> None:
    n = len(rows)
    if n == 1:
        row = rows[0]
        st.error("This permanently deletes the record. There is no undo.")
        st.markdown(
            f"- **UID:** `{row['uid']}`\n"
            f"- **Title:** {row.get('opportunity_title') or '(no title)'}\n"
            f"- **Funder:** {row.get('funding_agency') or '—'}"
        )
    else:
        st.error(f"This permanently deletes **{n} records**. There is no undo.")
        preview = rows[:12]
        for r in preview:
            st.markdown(
                f"- `{r['uid']}` — {(r.get('opportunity_title') or '(no title)')[:80]}"
            )
        if n > len(preview):
            st.markdown(f"_… and {n - len(preview)} more_")
    c1, c2 = st.columns(2)
    if c1.button(
        f"Confirm delete ({n})" if n > 1 else "Confirm delete",
        type="primary", width='stretch',
    ):
        uids = [r["uid"] for r in rows]
        sb.table("rfp_submissions").delete().in_("uid", uids).execute()
        st.cache_data.clear()
        st.toast(f"Deleted {n} record(s)", icon="🗑")
        st.rerun()
    if c2.button("Cancel", width='stretch'):
        st.rerun()


# -----------------------------------------------------------------------------
# Modal: Share
# -----------------------------------------------------------------------------
def _markdown_summary(row: dict) -> str:
    return (
        f"# {row.get('opportunity_title') or '(untitled)'}\n\n"
        f"- **UID:** {row.get('uid')}\n"
        f"- **Funder:** {row.get('funding_agency') or '—'}\n"
        f"- **Applicant role:** {row.get('applicant_role') or '—'}\n"
        f"- **Window:** {row.get('funding_window') or '—'}\n"
        f"- **Deadline:** {row.get('submission_deadline') or '—'}\n"
        f"- **Estimated value:** {row.get('estimated_value') or '—'} {row.get('currency') or ''}\n"
        f"- **Geography:** {', '.join(row.get('geographic_scope') or []) or '—'}\n"
        f"- **Program area:** {', '.join(row.get('program_area') or []) or '—'}\n"
        f"- **Alignment score:** {row.get('alignment_score') or 0:.1f} / 100\n"
        f"- **Auto-recommendation:** {row.get('auto_recommendation') or '—'}\n"
        f"- **Decision:** {row.get('decision') or '—'}\n"
        f"- **Link:** {row.get('opportunity_link') or '—'}\n\n"
        f"## Brief\n{row.get('brief_description') or '_(no description)_'}\n\n"
        f"## Key risks\n{row.get('key_risks') or '_(none recorded)_'}\n"
    )


def _html_summary(row: dict) -> str:
    md = _markdown_summary(row)
    lines = []
    for ln in md.splitlines():
        if ln.startswith("# "):
            lines.append(f"<h2>{ln[2:]}</h2>")
        elif ln.startswith("## "):
            lines.append(f"<h3>{ln[3:]}</h3>")
        elif ln.startswith("- "):
            lines.append(f"<li>{ln[2:]}</li>")
        elif ln.strip():
            lines.append(f"<p>{ln}</p>")
    return "<div style='font-family:Arial,sans-serif;'>" + "\n".join(lines) + "</div>"


@st.dialog("Share RFP(s)", width="large")
def share_dialog(rows: list[dict]) -> None:
    n = len(rows)
    if n == 1:
        row = rows[0]
        st.markdown(
            f"**`{row['uid']}`** — "
            f"{row.get('opportunity_title') if isinstance(row.get('opportunity_title'), str) else ''}"
        )
    else:
        st.markdown(f"**{n} RFPs selected.**")
        with st.expander(f"View list ({n})", expanded=False):
            for r in rows:
                st.markdown(
                    f"- `{r['uid']}` — {(r.get('opportunity_title') or '(no title)')[:80]}"
                )

    tab_dl, tab_send, tab_copy = st.tabs(["Download", "Send via email", "Copy summary"])

    with tab_dl:
        st.caption(
            f"Download {'this RFP' if n == 1 else f'all {n} RFPs'} as CSV."
        )
        df_all = pd.DataFrame(rows)
        buf = StringIO()
        df_all.to_csv(buf, index=False)
        from datetime import date as _date
        fname = (
            f"{rows[0]['uid']}.csv" if n == 1
            else f"rfps_{n}_records_{_date.today().isoformat()}.csv"
        )
        st.download_button(
            "⬇ Download CSV",
            data=buf.getvalue(),
            file_name=fname,
            mime="text/csv",
            width='stretch',
        )

    with tab_send:
        st.caption(
            "Sends a formatted summary via Resend. "
            + ("One email per RFP." if n > 1 else "")
        )
        # Use a fixed key — multi-row would otherwise produce uid-specific
        # keys that change when selection changes.
        to = st.text_input("Recipient email *", key="share_to_field")
        default_subject = (
            f"RFP: {rows[0].get('opportunity_title','')[:80]}" if n == 1
            else f"{n} RFPs from RFPIS"
        )
        subject = st.text_input("Subject", value=default_subject, key="share_subj_field")
        note = st.text_area("Optional note", height=80, key="share_note_field")
        if st.button("📤 Send", type="primary", width='stretch', key="share_send_btn"):
            if not to.strip():
                st.error("Recipient email is required.")
            else:
                note_html = (
                    f"<hr><p><i>Note from {user.get('name') or user.get('email')}:</i>"
                    f"<br>{note}</p>"
                    if note.strip() else ""
                )
                if n == 1:
                    html = _html_summary(rows[0]) + note_html
                else:
                    blocks = [_html_summary(r) for r in rows]
                    html = "<hr>".join(blocks) + note_html
                try:
                    send_email(
                        to=[to.strip()], subject=subject, html=html,
                        reply_to=user.get("email"),
                    )
                    st.success(f"Sent to {to}.")
                except MailerNotConfigured as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.error(f"Send failed: {exc}")

    with tab_copy:
        st.caption("Copy this markdown into Slack, email, a doc, etc.")
        if n == 1:
            st.code(_markdown_summary(rows[0]), language="markdown")
        else:
            combined = "\n\n---\n\n".join(_markdown_summary(r) for r in rows)
            st.code(combined, language="markdown")


# -----------------------------------------------------------------------------
# Modal: Blacklist source (one-click hard-reject for future scans)
# -----------------------------------------------------------------------------
from urllib.parse import urlparse as _urlparse


def _suggest_blacklist_pattern(url: str) -> str:
    p = _urlparse(url or "")
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segs = [s for s in (p.path or "").split("/") if s]
    return f"{host}/{segs[0]}" if host and segs else host


@st.dialog("Blacklist source")
def blacklist_dialog(row: dict) -> None:
    url = row.get("opportunity_link") or ""
    st.caption(
        "Future candidates whose URL contains this pattern are rejected before "
        "scoring (never become records). Matched as a case-insensitive substring."
    )
    st.code(url or "(no URL on this record)")
    pattern = st.text_input(
        "Pattern (URL substring to block)",
        value=_suggest_blacklist_pattern(url),
        help="Broaden to a bare domain (e.g. cdc.gov) to block the whole site, "
             "or keep a path (e.g. comicrelief.com/sportrelief) to block a section.",
    )
    reason = st.text_input("Reason / note", value="off-topic — not a call")
    also_delete = st.checkbox("Also delete this record now", value=True)
    if st.button("🚫 Add to blacklist", type="primary"):
        p = (pattern or "").strip().lower()
        if not p:
            st.error("Enter a pattern.")
            return
        try:
            sb.table("scan_blacklist").upsert(
                {"pattern": p, "reason": (reason or None), "created_by": user.get("email")},
                on_conflict="pattern",
            ).execute()
            try:
                from core import blacklist as _blmod
                _blmod.clear_cache()
            except Exception:
                pass
            if also_delete:
                sb.table("rfp_submissions").delete().eq("uid", row["uid"]).execute()
            st.cache_data.clear()
            st.toast(f"Blacklisted '{p}'", icon="🚫")
            st.rerun()
        except Exception as exc:
            st.error(f"Add to blacklist failed: {exc}")


# -----------------------------------------------------------------------------
# Wire button clicks to modals
# -----------------------------------------------------------------------------
if edit_clicked and not is_multi:
    edit_dialog(selected_full_rows[0])
if delete_clicked:
    delete_dialog(selected_full_rows)
if share_clicked:
    share_dialog(selected_full_rows)
if blacklist_clicked and not is_multi:
    blacklist_dialog(selected_full_rows[0])
