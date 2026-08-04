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

import html as _html
from collections import defaultdict
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core import dropdowns, settings
from core.currency import format_money
from core.scorer import criterion_score
from core.records import clean_record, clean_df
from core.pipeline import days_to_deadline, deadline_status, usd_value
from core.review_week import all_weeks_for_year, week_bounds
from db.supabase_client import get_client
from views.rfp_editor import render_rfp_editor

# auth handled by wrapper page
user = st.session_state["app_user"]
sb = get_client()
is_admin = user.get("role") in ("super_user", "admin")

year = settings.get_year()
today = date.today()

st.markdown(
    f"<h2 style='font-size:1.55rem;font-weight:700;color:#334155;"
    f"margin:0.15rem 0 0.5rem;'>Year-to-Date Tracking ({year})</h2>"
    # Compact metric-value font so long values (e.g. '€1,500,000') fit the card.
    "<style>[data-testid='stMetricValue']{font-size:1.4rem;line-height:1.2;"
    "white-space:normal;overflow:visible;}</style>",
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
    df = clean_df(pd.DataFrame(res.data or []))
    if df.empty:
        return df

    # YTD by submitted_at
    df["_submitted_at"] = pd.to_datetime(df["submitted_at"], errors="coerce", format="ISO8601")
    df = df[df["_submitted_at"].dt.year == year].copy()

    # Exclude submitted/approved/completed
    dd_lower = df["donor_decision"].fillna("").str.lower()
    df = df[dd_lower.isin({"", "not submitted"})].copy()
    if "progress_status" in df:
        # Progress status OVERRIDES: a Completed (submitted) or Discontinued row
        # drops off the active pipeline entirely — it must not appear in the card,
        # the dropdown, or the table even though its decision is still "Proceed".
        df = df[~df["progress_status"].fillna("").astype(str).str.strip()
                .str.lower().isin({"completed", "discontinued"})].copy()

    # Not overdue (allow null deadlines)
    df["_deadline_date"] = pd.to_datetime(df["call_submission_deadline"], errors="coerce", format="ISO8601").dt.date
    df = df[df["_deadline_date"].isna() | (df["_deadline_date"] >= today)].copy()

    if df.empty:
        return df

    # Derived columns
    df["_dtd"] = df["call_submission_deadline"].apply(days_to_deadline)
    df["_dstat"] = df["call_submission_deadline"].apply(deadline_status)
    df["_usd"] = df.apply(lambda r: usd_value(r.get("call_award_value"), r.get("currency")), axis=1)

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
        # NaN (a float) is truthy, so guard on type — a blank pandas cell would
        # otherwise slip past `if l` and break ", ".join (expected str, got float).
        return ", ".join(sorted({l.strip() for l in leads
                                 if isinstance(l, str) and l.strip()}))

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
PICK_KEY = "tracking_pick_label"          # selectbox widget state
TABLE_KEY = "tracking_browse"             # dataframe widget state


def _label(r: dict) -> str:
    return f"{r['uid']} · {(r.get('funding_agency') or '—')[:30]} · {(r.get('opportunity_title') or '')[:80]}"


labels = [_label(r) for _, r in df.iterrows()]
uid_by_label = {_label(r): r["uid"] for _, r in df.iterrows()}
label_by_uid = {uid: lbl for lbl, uid in uid_by_label.items()}
_uids = df["uid"].tolist()

# Single source of truth = SELECTED_UID_KEY. The selectbox AND the table both write
# it ONLY from their own on-change callbacks (which run once, before the next render),
# so the two widgets never fight across reruns — this is what kills the flicker /
# auto-uncheck loop. No manual st.rerun() anywhere.
if st.session_state.get(SELECTED_UID_KEY) not in _uids:
    st.session_state[SELECTED_UID_KEY] = _uids[0]
if st.session_state.get(PICK_KEY) not in labels:        # keep selectbox in sync
    st.session_state[PICK_KEY] = label_by_uid[st.session_state[SELECTED_UID_KEY]]


def _on_pick() -> None:
    st.session_state[SELECTED_UID_KEY] = uid_by_label.get(
        st.session_state.get(PICK_KEY), st.session_state[SELECTED_UID_KEY])


def _on_table_select() -> None:
    state = st.session_state.get(TABLE_KEY)
    sel = getattr(state, "selection", None)
    if sel is None and isinstance(state, dict):
        sel = state.get("selection")
    rows = getattr(sel, "rows", None)
    if rows is None and isinstance(sel, dict):
        rows = sel.get("rows")
    if rows:
        uid = _uids[rows[0]]
        st.session_state[SELECTED_UID_KEY] = uid
        st.session_state[PICK_KEY] = label_by_uid.get(uid, st.session_state.get(PICK_KEY))


st.selectbox(
    "Select RFP to review", labels, key=PICK_KEY, on_change=_on_pick,
    help="Or click a row in the table at the bottom.",
)
selected_uid = st.session_state[SELECTED_UID_KEY]

row = clean_record(df[df["uid"] == selected_uid].iloc[0].to_dict())

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
    top4.metric("Value", format_money(row.get("call_award_value"), row.get("currency")))
    top4.caption(f"≈ ${row.get('_usd') or 0:,.0f} USD")

    d1, d2, d3 = st.columns(3)
    d1.markdown(f"**Submission deadline**  \n{row.get('call_submission_deadline') or '—'}")
    d1.markdown(f"**Expected award**  \n{row.get('expected_award_date') or '—'}")
    d2.markdown(f"**Proposal lead(s)**  \n{row.get('all_leads') or '—'}")
    d2.markdown(f"**Stage**  \n{row.get('stage') or '—'}")
    d3.markdown(f"**Progress status**  \n{row.get('progress_status') or '—'}")
    d3.markdown(f"**Geography**  \n{', '.join(row.get('call_geographic_scope') or []) or '—'}")

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
# How to apply — funding-call link + AI-written step-by-step + the Apply button
# (opens the funder's application portal in a new tab). Helps the client go from
# "this is a fit" straight to applying, without leaving the platform.
# -----------------------------------------------------------------------------
with st.container(border=True):
    st.markdown("### 📝 How to apply")
    _how = (row.get("how_to_apply") or "").strip()
    if _how:
        # Escape $ so the steps don't render as LaTeX; LLM returns "1. …" lines.
        st.markdown(_how.replace("$", "\\$"))
    else:
        st.caption("_The step-by-step guide is written by the AI during extraction. "
                   "It'll appear here once this opportunity has been processed "
                   "(run the synthesis backfill / next extraction)._")
    # Funding-call link goes right AFTER the steps.
    _call_link = row.get("opportunity_link")
    if _call_link:
        st.markdown(f"📄 [Access opportunity here]({_call_link})")
    # Apply button — specific apply_url, else the donor's persistent submission
    # portal (so future calls from the same funder inherit it), else the call link.
    _apply_url = row.get("apply_url")
    if not _apply_url:
        try:
            _fa = (row.get("funding_agency") or "").strip()
            if _fa:
                _dp = (sb.table("donor_intel").select("donor_submission_portal_url")
                       .ilike("donor", _fa).limit(1).execute().data or [])
                if _dp and _dp[0].get("donor_submission_portal_url"):
                    _apply_url = _dp[0]["donor_submission_portal_url"]
        except Exception:
            pass
    _apply_url = _apply_url or _call_link
    if _apply_url:
        ac1, _acsp = st.columns([2, 5])
        ac1.link_button("🚀 Apply on the funder's portal ↗", _apply_url,
                        type="primary", width='stretch')


# -----------------------------------------------------------------------------
# Browse table — all active Proceed RFPs YTD (click to swap selection)
# -----------------------------------------------------------------------------
st.markdown("")
st.subheader(f"All Active Proceed RFPs ({len(df)})")
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

st.dataframe(
    browse,
    width='stretch',
    hide_index=True,
    selection_mode="single-row",
    on_select=_on_table_select,          # callback updates the shared selection
    key=TABLE_KEY,                       # keyed → selection persists (no auto-uncheck)
    column_config={
        "Days":      st.column_config.NumberColumn("Days to deadline"),
        "USD value": st.column_config.NumberColumn("USD value", format="$%.0f"),
    },
)


# -----------------------------------------------------------------------------
# Quick edit — Role / Stage / Lead(s) for the selected RFP, without leaving the
# Tracking page. (Full field editing still lives on the Data page.)
# -----------------------------------------------------------------------------
@st.dialog("Edit RFP — Role / Stage / Lead", width="large")
def _edit_tracking(r: dict) -> None:
    st.markdown(f"**`{r['uid']}`** — {r.get('opportunity_title') or ''}")
    _roles = list(dropdowns.get("applicant_roles") or ["Prime", "Sub", "Technical"])
    _cur_role = r.get("applicant_role")
    if _cur_role and _cur_role not in _roles:
        _roles = [_cur_role] + _roles
    role = st.selectbox("Role", _roles,
                        index=_roles.index(_cur_role) if _cur_role in _roles else 0)
    _stages = list(dropdowns.get("stages") or [
        "Identification & screening", "Go/no-go decision & bid planning",
        "Proposal development", "Final packaging & submission",
        "Post-submission follow-up"])
    _cur_stage = r.get("stage")
    if _cur_stage and _cur_stage not in _stages:
        _stages = [_cur_stage] + _stages
    stage = st.selectbox("Stage", _stages,
                         index=_stages.index(_cur_stage) if _cur_stage in _stages else 0)
    lead = st.text_input("Proposal lead(s)", value=(r.get("proposal_lead") or ""))
    if st.button("💾 Save changes", type="primary", width='stretch'):
        sb.table("rfp_submissions").update({
            "applicant_role": role, "stage": stage,
            "proposal_lead": (lead.strip() or None),
        }).eq("uid", r["uid"]).execute()
        st.cache_data.clear()
        st.success(f"Saved {r['uid']}.")
        st.rerun()


def _esc(v) -> str:
    return _html.escape(str(v if v not in (None, "") else "—"))


def _view_meta_card(label: str, value: str, sub: str = "") -> str:
    """One compact metric tile for the details dialog."""
    return (
        f"<div style='flex:1 1 22%;min-width:128px;background:#f8fafc;"
        f"border:1px solid #e2e8f0;border-radius:8px;padding:8px 11px'>"
        f"<div style='font-size:.68rem;color:#64748b;text-transform:uppercase;"
        f"letter-spacing:.04em;font-weight:600'>{_esc(label)}</div>"
        f"<div style='font-size:.96rem;font-weight:700;color:#0f172a;"
        f"line-height:1.25;margin-top:2px'>{_esc(value)}</div>"
        + (f"<div style='font-size:.7rem;color:#94a3b8;margin-top:1px'>{_esc(sub)}</div>"
           if sub else "")
        + "</div>"
    )


@st.dialog("RFP details", width="large")
def _view_rfp(r: dict) -> None:
    """Read-only, top-to-bottom view of every detail for one Proceed RFP."""
    _status = r.get("_dstat") or "On Track"
    _icon, _sbg = BADGE.get(_status, ("⚪", "#eee"))
    _dtd = r.get("_dtd")
    _days = f"{int(_dtd):+d}" if pd.notna(_dtd) else "—"
    _val = format_money(r.get("call_award_value"), r.get("currency"))
    _usd = f"≈ ${float(r.get('_usd') or 0):,.0f} USD"

    # ── Header banner ──────────────────────────────────────────────────────
    st.markdown(
        "<div style='background:linear-gradient(95deg,#0f766e,#0d9488);color:#fff;"
        "padding:15px 18px;border-radius:11px'>"
        f"<div style='font-size:1.2rem;font-weight:700;line-height:1.3'>"
        f"{_esc(r.get('opportunity_title'))}</div>"
        f"<div style='opacity:.92;margin-top:3px;font-size:.95rem'>"
        f"{_esc(r.get('funding_agency'))}</div>"
        "<div style='margin-top:9px;display:flex;gap:7px;flex-wrap:wrap;align-items:center'>"
        "<span style='background:rgba(255,255,255,.22);padding:3px 11px;border-radius:20px;"
        f"font-size:.78rem;font-weight:600'>{_esc(r.get('decision'))}</span>"
        f"<span style='background:{_sbg};color:#1f2937;padding:3px 11px;border-radius:20px;"
        f"font-size:.78rem;font-weight:600'>{_icon} {_esc(_status)}</span>"
        f"<span style='opacity:.85;font-size:.76rem;font-family:monospace'>"
        f"{_esc(r.get('uid'))}</span>"
        "</div></div>",
        unsafe_allow_html=True,
    )

    # ── Metric tiles ───────────────────────────────────────────────────────
    tiles = [
        ("Value", _val, _usd),
        ("Days to deadline", _days, ""),
        ("Deadline", r.get("call_submission_deadline") or "—", ""),
        ("Expected award", r.get("expected_award_date") or "—", ""),
        ("Applicant role", r.get("applicant_role") or "—", ""),
        ("Funding window", r.get("funding_window") or "—", ""),
        ("Stage", r.get("stage") or "—", ""),
        ("Progress", r.get("progress_status") or "—", ""),
    ]
    if r.get("alignment_score") not in (None, ""):
        tiles.insert(1, ("Bid strength", f"{r.get('alignment_score')}/100", ""))
    st.markdown(
        "<div style='display:flex;flex-wrap:wrap;gap:8px;margin:11px 0 4px'>"
        + "".join(_view_meta_card(*t) for t in tiles) + "</div>",
        unsafe_allow_html=True,
    )

    # ── At-a-glance fields ─────────────────────────────────────────────────
    g1, g2 = st.columns(2)
    g1.markdown(f"**🌍 Geography**  \n{', '.join(r.get('call_geographic_scope') or []) or '—'}")
    g1.markdown(f"**👥 Proposal lead(s)**  \n{r.get('all_leads') or r.get('proposal_lead') or '—'}")
    g2.markdown(f"**🎯 Focus areas**  \n{', '.join(r.get('call_domain_areas') or []) or '—'}")
    g2.markdown(f"**⏱ Duration**  \n{r.get('project_duration') or '—'}")

    # ── Eligibility outcome — the 9 MUST/PREFER high-level outputs (labels only,
    #    no component breakdown). Colour: green=Yes/Strong · amber=partial/Not sure
    #    (Park) · red=fail. "Not sure" (None) reads amber, per the scoring model.
    st.divider()
    st.markdown("**🧮 Eligibility outcome**")
    _elig = [
        ("qualification", "MUST 1 · Legal status & qualification"),
        ("strategic_fit", "MUST 2 · Strategic fit"),
        ("capacity", "MUST 3 · Implementation capacity"),
        ("geographic_fit", "MUST 4 · Geographic fit"),
        ("cofinancing", "MUST 5 · Cofinancing & compliance"),
        ("funding_quality", "PREFER 6 · Funding quality"),
        ("funder_relationship", "PREFER 7 · Donor relationship"),
        ("competitiveness", "PREFER 8 · Competitiveness"),
        ("bid_effort", "PREFER 9 · Bid effort"),
    ]
    _palette = {2: ("#15803d", "#f0fdf4"), 1: ("#b45309", "#fffbeb"),
                0: ("#b91c1c", "#fef2f2")}
    _rows = []
    for _k, _name in _elig:
        _v = r.get(_k)
        _fg, _bg = _palette.get(criterion_score(_v), ("#b45309", "#fffbeb"))  # None→amber (Park)
        _rows.append(
            f"<div style='flex:1 1 46%;min-width:210px;border-left:4px solid {_fg};"
            f"background:{_bg};border-radius:6px;padding:6px 11px'>"
            f"<div style='font-size:.68rem;color:#64748b;font-weight:600'>{_esc(_name)}</div>"
            f"<div style='font-size:.9rem;font-weight:700;color:{_fg}'>"
            f"{_esc(_v if _v not in (None, '') else 'Not sure')}</div></div>")
    st.markdown(
        "<div style='display:flex;flex-wrap:wrap;gap:7px;margin-top:4px'>"
        + "".join(_rows) + "</div>",
        unsafe_allow_html=True,
    )

    # ── Narrative sections (only render what exists) ───────────────────────
    def _section(title: str, body) -> None:
        body = (str(body) or "").strip()
        if body:
            st.markdown(f"**{title}**")
            st.markdown(body.replace("$", "\\$"))

    st.divider()
    _section("📋 Brief description", r.get("brief_description"))
    _section("🧭 Why this decision", r.get("decision_note"))
    _section("⚠️ Key risks", r.get("key_risks"))
    _section("✅ Compliance requirements", r.get("compliance_requirements"))
    _section("🎯 Eligibility specifics", r.get("eligibility_specifics"))
    _section("📝 How to apply", r.get("how_to_apply"))
    _section("📋 Application checklist", r.get("application_checklist"))

    # ── Links / apply ──────────────────────────────────────────────────────
    _link = r.get("opportunity_link")
    if _link:
        st.markdown(f"📄 [Access opportunity here]({_link})")
    _apply = r.get("apply_url") or _link
    if _apply:
        st.link_button("🚀 Apply on the funder's portal ↗", _apply, type="primary")


_vb, _eb1, _eb2, _ebsp = st.columns([2, 2, 2, 2])
if _vb.button("👁 View", type="primary", width='stretch'):
    _view_rfp(row)
# if _eb2.button("✏ Edit (Role / Stage / Lead)", width='stretch'):
#     _edit_tracking(row)
if _eb1.button("✏️ Edit RFP", type="primary", width='stretch'):
    render_rfp_editor(row, sb=sb, user=user, is_admin=is_admin)
