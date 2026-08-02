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

import html as _html
from datetime import date, datetime, timezone
from io import StringIO

import pandas as pd
import streamlit as st

from core import dropdowns
from core.currency import format_money
from core.mailer import MailerNotConfigured, send_email
from core.pipeline import days_to_deadline, usd_value
from core.records import clean_record, clean_df
from core.scorer import (score_submission, criterion_score,
                         CRITERION_RESPONSES, default_response)
from db.supabase_client import get_client, safe_execute
from views.rfp_editor import render_rfp_editor

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
        .order("uid", desc=True)          # deterministic final tiebreaker — stable order
        .execute()
    )
    return clean_df(pd.DataFrame(res.data or []))


df = _fetch_all()
if df.empty:
    st.info("No RFPs yet. Submit one via the Submit page or trigger a scan from Admin.")
    st.stop()

# Newest first by Search date (recently-found RFPs on top). search_date is an ISO
# string/datetime; parse for a correct chronological sort (NaT/blank sink to the
# bottom). This is the table's primary order regardless of insertion order.
df["_search_dt"] = pd.to_datetime(df.get("search_date"), errors="coerce", format="ISO8601")
df = df.sort_values("_search_dt", ascending=False, na_position="last").reset_index(drop=True)


# -----------------------------------------------------------------------------
# LIVE scoring — single source of truth (shared with Review / Screen / View modal).
# The stored alignment_score / auto_recommendation are a scan-time SNAPSHOT that goes
# stale when the org profile, donor intel, or scoring logic change. Recompute fresh via
# core.assessment.assess_row (the SAME path as the Review gauge) so this table, its
# Probability filter, and the View modal never disagree with Review. Cached + busted by
# a profile/settings signature so a profile edit refreshes every row.
# -----------------------------------------------------------------------------
import json as _json_live
import hashlib as _hashlib_live
from core import org_profile as _op_live, settings as _stg_live
from core.assessment import assess_row as _assess_row

_prof_sig = _hashlib_live.sha1(
    (_json_live.dumps(_op_live.get_profile(), sort_keys=True, default=str)
     + _json_live.dumps(_stg_live.get_org(), sort_keys=True, default=str)).encode()
).hexdigest()[:12]


# PER-ROW memo in session_state so navigation / pagination / delete don't re-score every
# row. Only genuinely new or edited rows are scored (see core.live_scoring); the memo
# survives st.cache_data.clear() (which the delete/edit handlers fire) because it lives in
# session_state, not the data cache.
try:
    from core.live_scoring import scores_for as _scores_for
    _memo = st.session_state.setdefault("_records_score_memo", {})
    with st.spinner("Scoring rows…"):        # only visible when real work happens
        _sc, _n_scored = _scores_for(df.to_dict("records"), _prof_sig, _memo)
    df["alignment_score"] = df["uid"].map(
        lambda u: (_sc.get(str(u)) or {}).get("alignment_score"))
    df["auto_recommendation"] = df["uid"].map(
        lambda u: (_sc.get(str(u)) or {}).get("auto_recommendation"))
except Exception:
    pass  # fall back to stored columns if live scoring is unavailable


# -----------------------------------------------------------------------------
# Filters — one widget per filterable column. Open by default.
# -----------------------------------------------------------------------------
# Compute Prob tier per row (same logic as Highlights Section B)
from core.pipeline import prob_tier as _prob_tier_shared
df["_prob"] = df["alignment_score"].apply(lambda s: _prob_tier_shared(s, short=True))

with st.expander("Filters", expanded=False):
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
    "call_submission_deadline",
    "call_award_value",
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
if "call_submission_deadline" in table:
    table["call_submission_deadline"] = pd.to_datetime(
        table["call_submission_deadline"], errors="coerce", format="ISO8601").dt.date
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
    _ev = (view_df["call_award_value"] if "call_award_value" in view_df.columns
           else [None] * len(table))
    _cc = (view_df["currency"] if "currency" in view_df.columns
           else [None] * len(table))
    table["_value_usd"] = [_usd_display(a, c) for a, c in zip(list(_ev), list(_cc))]

# Decision is the HUMAN's call — show "Pending" until a reviewer sets it (a blank
# decision means un-reviewed, NOT a decline). The system's suggestion is shown
# separately in the Auto-decision column.
if "decision" in table.columns:
    table["decision"] = (table["decision"].astype("object")
                         .where(table["decision"].notna(), None)
                         .map(lambda v: v if (isinstance(v, str) and v.strip()) else "Pending"))

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
    "call_submission_deadline": st.column_config.DateColumn("Deadline"),
    "call_award_value": st.column_config.NumberColumn("Value", format="%.0f"),
    "currency": st.column_config.TextColumn("Currency", width="small"),
    "_value_usd": st.column_config.TextColumn("Value (USD)", width="medium"),
    "alignment_score": st.column_config.NumberColumn("Score", format="%.1f"),
    "_prob": st.column_config.TextColumn("Probability", width="small"),
    "auto_recommendation": st.column_config.TextColumn("Auto-decision"),
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
    clean_record(df[df["uid"] == uid].iloc[0].to_dict())
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
    view_clicked = False
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
    ab0, ab1, ab2, ab3, ab4, ab5, ab6, ab7, _ = st.columns(
        [1, 1, 1, 1, 1.3, 0.6, 0.6, 0.6, 0.9])
    view_clicked = ab0.button("👁 View", width='stretch',
                              help="Read-only: see every field of this RFP "
                                   "(filled + blank) in one window.")
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
    # 👍 / 😐 / 👎 — label this RFP good/neutral/bad as a training signal.
    # Three-way mirrors the decision classes (Proceed→good, Park→neutral,
    # Decline→bad) so feedback doesn't skew the learning data.
    good_clicked = ab5.button("👍", width='stretch',
                              help="GOOD match — like a Proceed (training signal).")
    neutral_clicked = ab6.button("😐", width='stretch',
                                 help="NEUTRAL — like a Park: unclear / needs review.")
    bad_clicked = ab7.button("👎", width='stretch',
                             help="BAD match — like a Decline (training signal).")
    if good_clicked or neutral_clicked or bad_clicked:
        verdict = "good" if good_clicked else ("neutral" if neutral_clicked else "bad")
        try:
            from core import decision_log
            decision_log.log_feedback(
                selected_full_rows[0], verdict, by=user.get("email"))
            st.toast({"good": "👍 Marked good", "neutral": "😐 Marked neutral",
                      "bad": "👎 Marked bad"}[verdict]
                     + " — thanks, this trains the scorer.", icon="🧠")
        except Exception as exc:
            st.warning(f"Couldn't record feedback: {exc}")


# -----------------------------------------------------------------------------
# Modal: Edit
# -----------------------------------------------------------------------------
def _rv_esc(v) -> str:
    s = "" if v is None else str(v)
    s = s.strip()
    return _html.escape(s if (s and s.lower() != "nan") else "—")


def _rv_meta_card(label: str, value: str, sub: str = "") -> str:
    """One compact metric tile for the View dialog (matches the Tracking design)."""
    return (
        f"<div style='flex:1 1 22%;min-width:128px;background:#f8fafc;"
        f"border:1px solid #e2e8f0;border-radius:8px;padding:8px 11px'>"
        f"<div style='font-size:.68rem;color:#64748b;text-transform:uppercase;"
        f"letter-spacing:.04em;font-weight:600'>{_rv_esc(label)}</div>"
        f"<div style='font-size:.96rem;font-weight:700;color:#0f172a;"
        f"line-height:1.25;margin-top:2px'>{_rv_esc(value)}</div>"
        + (f"<div style='font-size:.7rem;color:#94a3b8;margin-top:1px'>{_rv_esc(sub)}</div>"
           if sub and sub != "—" else "")
        + "</div>"
    )


# Decision → pill colour for the banner.
_DECISION_PILL = {
    "proceed": ("#15803d", "#dcfce7"), "proceed as sub": ("#15803d", "#dcfce7"),
    "park": ("#b45309", "#fef3c7"), "decline": ("#b91c1c", "#fee2e2"),
}


@st.dialog("View RFP", width="large")
def view_dialog(row: dict) -> None:
    """Polished read-only view of one RFP — gradient header, key-metric tiles, the
    high-level MUST/PREFER eligibility outcome, narrative, then the full operational /
    team / award fields (grouped, every field filled or '—'). No edits here."""
    # LIVE assessment (single source of truth) — overlay fresh Bid Strength / Auto-
    # decision / the 9 criteria so this modal matches the Review gauge, never the stale
    # scan-time snapshot stored on the row.
    try:
        from core.assessment import assess_row as _ar_live, CRITERIA as _arc_live
        _live = _ar_live(row)
        row = {**row, **{k: _live[k] for k in (*_arc_live, "alignment_score",
                                               "auto_recommendation") if k in _live}}
    except Exception:
        pass
    _title = row.get("opportunity_title") if isinstance(row.get("opportunity_title"), str) else ""
    _dec_raw = row.get("decision")
    _dec = _dec_raw if (isinstance(_dec_raw, str) and _dec_raw.strip()) else "Pending"
    _auto = row.get("auto_recommendation") or "—"
    _sc = row.get("alignment_score")
    _val = format_money(row.get("call_award_value"), row.get("currency"))
    try:
        _usd = usd_value(row.get("call_award_value"), row.get("currency"))
    except Exception:
        _usd = None
    _dtd = days_to_deadline(row.get("call_submission_deadline"))
    _pfg, _pbg = _DECISION_PILL.get(str(_dec).strip().lower(), ("#475569", "#e2e8f0"))

    def _disp(v) -> str:
        if v is None:
            return "—"
        if isinstance(v, (list, tuple)):
            s = ", ".join(str(x) for x in v if str(x).strip())
        else:
            s = str(v).strip()
        if not s or s.lower() == "nan":
            return "—"
        # Escape "$" so Streamlit markdown doesn't read "$2.3 million … $5" as a
        # LaTeX math block (&#36; renders as "$" without triggering LaTeX).
        return s.replace("$", "&#36;")

    # ── Header banner ──────────────────────────────────────────────────────
    st.markdown(
        "<div style='background:linear-gradient(95deg,#0f766e,#0d9488);color:#fff;"
        "padding:15px 18px;border-radius:11px'>"
        f"<div style='font-size:1.2rem;font-weight:700;line-height:1.3'>{_rv_esc(_title)}</div>"
        f"<div style='opacity:.92;margin-top:3px;font-size:.95rem'>{_rv_esc(row.get('funding_agency'))}</div>"
        "<div style='margin-top:9px;display:flex;gap:7px;flex-wrap:wrap;align-items:center'>"
        f"<span style='background:{_pbg};color:{_pfg};padding:3px 11px;border-radius:20px;"
        f"font-size:.78rem;font-weight:700'>Decision: {_rv_esc(_dec)}</span>"
        "<span style='background:rgba(255,255,255,.22);padding:3px 11px;border-radius:20px;"
        f"font-size:.78rem;font-weight:600'>Auto: {_rv_esc(_auto)}</span>"
        f"<span style='opacity:.85;font-size:.76rem;font-family:monospace'>{_rv_esc(row.get('uid'))}</span>"
        "</div></div>",
        unsafe_allow_html=True,
    )

    # ── Metric tiles ───────────────────────────────────────────────────────
    tiles = []
    if _sc not in (None, ""):
        tiles.append(("Bid strength", f"{_sc}/100", ""))
    tiles += [
        ("Value", _val, (f"≈ ${_usd:,.0f} USD" if _usd else "")),
        ("Days to deadline", f"{int(_dtd):+d}" if _dtd is not None else "—", ""),
        ("Deadline", row.get("call_submission_deadline") or "—", ""),
        ("Funding window", row.get("funding_window") or "—", ""),
        ("Solicitation type", row.get("solicitation_type") or "—", ""),
        ("Duration", row.get("project_duration") or "—", ""),
        ("Source", row.get("source") or "—", ""),
    ]
    st.markdown(
        "<div style='display:flex;flex-wrap:wrap;gap:8px;margin:11px 0 4px'>"
        + "".join(_rv_meta_card(*t) for t in tiles) + "</div>",
        unsafe_allow_html=True,
    )

    # ── At-a-glance ────────────────────────────────────────────────────────
    g1, g2 = st.columns(2)
    g1.markdown(f"**🌍 Geography**  \n{_disp(row.get('call_geographic_scope'))}")
    g1.markdown(f"**👥 Proposal lead(s)**  \n{_disp(row.get('proposal_lead'))}")
    g2.markdown(f"**🎯 Focus areas**  \n{_disp(row.get('call_domain_areas'))}")
    g2.markdown(f"**📌 Stage / Progress**  \n{_disp(row.get('stage'))} · {_disp(row.get('progress_status'))}")

    # ── Eligibility outcome — high-level MUST/PREFER labels only ───────────
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
        _v = row.get(_k)
        _fg, _bg = _palette.get(criterion_score(_v), ("#b45309", "#fffbeb"))  # None→amber (Park)
        _rows.append(
            f"<div style='flex:1 1 46%;min-width:210px;border-left:4px solid {_fg};"
            f"background:{_bg};border-radius:6px;padding:6px 11px'>"
            f"<div style='font-size:.68rem;color:#64748b;font-weight:600'>{_rv_esc(_name)}</div>"
            f"<div style='font-size:.9rem;font-weight:700;color:{_fg}'>"
            f"{_rv_esc(_v if _v not in (None, '') else 'Not sure')}</div></div>")
    st.markdown(
        "<div style='display:flex;flex-wrap:wrap;gap:7px;margin-top:4px'>"
        + "".join(_rows) + "</div>", unsafe_allow_html=True)

    # ── Narrative sections (only render what exists) ───────────────────────
    def _section(title: str, body) -> None:
        b = ("" if body is None else str(body)).strip()
        if b and b.lower() != "nan":
            st.markdown(f"**{title}**")
            st.markdown(b.replace("$", "\\$"))

    st.divider()
    from core.records import strip_html as _strip_html
    _section("📋 Brief description", _strip_html(row.get("brief_description")))
    _section("🧭 Why this decision", row.get("decision_note"))
    _section("⚠️ Key risks", row.get("key_risks"))
    _section("✅ Compliance requirements", row.get("compliance_requirements"))
    _section("🎯 Eligibility specifics", row.get("eligibility_specifics"))
    _section("📝 How to apply", row.get("how_to_apply"))
    _section("📋 Application checklist", row.get("application_checklist"))
    _link = row.get("opportunity_link")
    if isinstance(_link, str) and _link.strip():
        st.markdown(f"📄 [Access opportunity here]({_link})")

    # ── Full operational / team / award fields (every field, filled or '—') ─
    st.divider()
    _GROUPS = [
        ("Decision & pipeline", [
            ("Decision", "decision"), ("Decision date", "decision_date"),
            ("Decision rationale", "decision_note"), ("Stage", "stage"),
            ("Progress status", "progress_status"), ("Applicant role", "applicant_role"),
            ("Lead applicant", "lead_applicant"), ("Sub applicant", "sub_applicant"),
            ("Next action", "next_action"), ("Assigned to", "assigned_to"),
            ("Action deadline", "action_deadline"), ("Donor decision", "donor_decision"),
            ("Decline flags present", "decline_flags_present"),
            ("Feasibility", "feasibility"), ("Remarks", "remarks")]),
        ("Team", [
            ("Proposal lead", "proposal_lead"), ("Contributors", "contributors"),
            ("Reviewers", "reviewers"), ("Support", "support_roles"),
            ("Submitted by", "submitted_by"), ("Submitted by email", "submitted_by_email")]),
        ("Award & post-award", [
            ("Amount requested", "amount_requested"), ("Date of approval", "date_of_approval"),
            ("Amount secured", "amount_secured"), ("Currency secured", "currency_secured"),
            ("Donor program officer", "donor_program_officer"), ("Next step", "next_step"),
            ("Kick-off date", "kickoff_date"), ("Expected award date", "expected_award_date"),
            ("Time to award", "time_to_award"), ("Date completed", "date_completed"),
            ("Submissions", "submissions")]),
        ("Other opportunity fields", [
            ("Instrument", "instrument_type"), ("Focus theme", "focus_theme"),
            ("Date posted", "date_posted"), ("Currency", "currency"),
            ("Estimated value", "call_award_value"), ("Search date", "search_date")]),
    ]
    for _gname, _fields in _GROUPS:
        _filled = sum(1 for _, k in _fields if _disp(row.get(k)) != "—")
        with st.expander(f"{_gname}  ·  {_filled}/{len(_fields)} filled",
                         expanded=(_gname == "Decision & pipeline")):
            for _lab, _key in _fields:
                if _key == "decision":
                    st.markdown(f"**{_lab}:** {_dec}")
                else:
                    st.markdown(f"**{_lab}:** {_disp(row.get(_key))}")


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
        try:
            # Resolve a FRESH tenant-scoped client at click time — the module-level `sb`
            # was captured once at import and can hold a stale/expired per-session JWT
            # client, whose write then hangs. safe_execute bounds the call with the
            # client's timeouts + a short retry so a transient blip can't stall the dialog.
            with st.spinner("Deleting…"):
                safe_execute(get_client().table("rfp_submissions")
                             .delete().in_("uid", uids))
            st.cache_data.clear()
            st.toast(f"Deleted {n} record(s)", icon="🗑")
            st.rerun()
        except Exception as exc:
            st.error(f"Delete failed — nothing was removed. {type(exc).__name__}: {exc}")
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
        f"- **Deadline:** {row.get('call_submission_deadline') or '—'}\n"
        f"- **Estimated value:** {row.get('call_award_value') or '—'} {row.get('currency') or ''}\n"
        f"- **Geography:** {', '.join(row.get('call_geographic_scope') or []) or '—'}\n"
        f"- **Program area:** {', '.join(row.get('call_domain_areas') or []) or '—'}\n"
        f"- **Alignment score:** {row.get('alignment_score') or 0:.1f} / 100\n"
        f"- **Auto-decision:** {row.get('auto_recommendation') or '—'}\n"
        f"- **Decision:** {row.get('decision') if (isinstance(row.get('decision'), str) and row.get('decision').strip()) else 'Pending'}\n"
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
        df_all = clean_df(pd.DataFrame(rows))
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
                safe_execute(get_client().table("rfp_submissions")
                             .delete().eq("uid", row["uid"]))
            st.cache_data.clear()
            st.toast(f"Blacklisted '{p}'", icon="🚫")
            st.rerun()
        except Exception as exc:
            st.error(f"Add to blacklist failed: {exc}")


# -----------------------------------------------------------------------------
# Wire button clicks to modals
# -----------------------------------------------------------------------------
if view_clicked and not is_multi:
    view_dialog(selected_full_rows[0])
if edit_clicked and not is_multi:
    render_rfp_editor(selected_full_rows[0], sb=sb, user=user, is_admin=is_admin)
if delete_clicked:
    delete_dialog(selected_full_rows)
if share_clicked:
    share_dialog(selected_full_rows)
if blacklist_clicked and not is_multi:
    blacklist_dialog(selected_full_rows[0])
