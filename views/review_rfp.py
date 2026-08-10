"""Page 2 — Review (team-review tool).

One opportunity at a time with **inline-editable** 9-criterion grid. As the
team toggles Yes/Partial/No on each criterion the alignment-score gauge and
auto-recommendation update live. A single "Save changes" button persists
decision + rationale + the nine criterion values + the recomputed score.

At top: a compact overview table of every RFP in the selected week, with a
decision badge, so the team can jump to any record without using Prev/Next.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import dropdowns, settings
from core.review_week import all_weeks_for_year, review_week_label, upcoming_review_week_label
from core.scorer import (
    CRITERIA, criterion_score, default_response, score_submission,
)
from core.records import clean_df, drop_concluded
from db.supabase_client import get_client, safe_execute
from views.rfp_editor import render_rfp_editor

# auth handled by wrapper page
user = st.session_state["app_user"]
role = user.get("role", "collaborator")
# Status/decision editing is a routine team-meeting task open to ANY tenant member
# (collaborator included); only destructive actions (Delete, via is_admin below) stay gated.
from core import permissions as _perm
can_edit = _perm.can_edit_status(user)
is_admin = role in ("super_user", "admin")
sb = get_client()

year = settings.get_year()
st.markdown(
    f"<h2 style='font-size:1.55rem;font-weight:700;color:#334155;"
    f"margin:0.15rem 0 0.5rem;'>Weekly Reviewing ({year})</h2>",
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Week + data
# -----------------------------------------------------------------------------
all_weeks = all_weeks_for_year(year)
default_week = review_week_label()
if default_week not in all_weeks:
    all_weeks = [default_week] + all_weeks

# DEEP LINK — /pipelines?uid=<uid> opens that RFP directly (used by the past-deadline nudge
# on Submit, so the user doesn't have to hunt for the row they just created). The picker is
# week-scoped, so we set the week from the row itself; applied once per uid so the reviewer
# can still navigate away afterwards.
_deep_uid = (st.query_params.get("uid") or "").strip()
if _deep_uid and st.session_state.get("_deep_uid_applied") != _deep_uid:
    try:
        _dr = (get_client().table("rfp_submissions").select("uid,review_week")
               .eq("uid", _deep_uid).limit(1).execute().data or [])
        if _dr:
            _dwk = (_dr[0].get("review_week") or "").strip()
            if _dwk:
                if _dwk not in all_weeks:
                    all_weeks = [_dwk] + all_weeks      # keep an out-of-range week selectable
                st.session_state["review_rfp_week"] = _dwk
            st.session_state["review_rfp_selected_uid"] = _deep_uid
        else:
            st.warning(f"Couldn't find RFP `{_deep_uid}` — it may have been deleted.")
    except Exception:
        pass
    st.session_state["_deep_uid_applied"] = _deep_uid

# Week selector + RFP selector on the same row, with year inline.
@st.cache_data(ttl=30)
def _fetch(week: str) -> pd.DataFrame:
    try:
        res = safe_execute(
            get_client()
            .table("rfp_submissions")
            .select("*")
            .eq("review_week", week)
            .eq("is_duplicate", False)
            .order("alignment_score", desc=True)
        )
    except Exception as exc:
        st.warning(f"Couldn't load this week's RFPs right now (network issue): {exc}")
        return pd.DataFrame()
    return clean_df(pd.DataFrame(res.data or []))


wc, rc = st.columns([1, 3])
with wc:
    sel_week = st.selectbox(
        f"Review week", all_weeks, index=all_weeks.index(default_week),
        key="review_rfp_week",
    )

df = _fetch(sel_week)
# Concluded grants (won/submitted) are tracked under Grants + counted in Summary —
# keep them out of the active Review list (e.g. HAPPI: Completed + Approved).
df = drop_concluded(df)
if df.empty:
    with rc:
        st.selectbox(f"RFPs in {sel_week} (0)", ["(no records)"], disabled=True)
    st.info(f"No RFPs recorded for **{sel_week}**.")
    st.stop()


def _label(r) -> str:
    # Filter pandas NaN ("nan") / None / NaT to a dash for the label.
    raw_dec = r.get("decision")
    try:
        if pd.isna(raw_dec):
            raw_dec = None
    except (TypeError, ValueError):
        pass
    dec = str(raw_dec).strip() if raw_dec else "—"
    if dec.lower() in ("nan", "nat", "none"):
        dec = "—"
    # Show the FULL title — truncating loses the opportunity-specific signal
    # the user needs to find the right row. Streamlit's dropdown will wrap
    # or scroll horizontally for long labels.
    return f"{r['uid']} · [{dec}] · {r.get('opportunity_title') or '(no title)'}"


labels = [_label(r) for _, r in df.iterrows()]
uids = [r["uid"] for _, r in df.iterrows()]

# Persist the user's selection across reruns (Save changes triggers a
# rerun that would otherwise reset the dropdown to index 0). We track the
# UID — not the label — because the label changes when decision is
# updated (e.g. [—] → [Proceed]) and a label-based default would fail to
# match after a save.
_remembered_uid = st.session_state.get("review_rfp_selected_uid")
_default_idx = uids.index(_remembered_uid) if _remembered_uid in uids else 0

with rc:
    picked = st.selectbox(
        f"RFPs in {sel_week} ({len(df)})",
        labels,
        index=_default_idx,
        key="review_rfp_picker",
    )

idx = labels.index(picked)
row = df.iloc[idx].to_dict()
# Remember which UID is being viewed so the next rerun lands here.
st.session_state["review_rfp_selected_uid"] = row["uid"]

st.divider()


# ── Single source of truth ──────────────────────────────────────────────────
# Org/donor context + LIVE derivation, computed ONCE here so the header decision
# chip, the criteria grid and the gauge all agree. A row counts as HUMAN-REVIEWED
# only when `decision_date` is stamped (a real Review save); otherwise the stored
# criteria/decision are stale auto-score snapshots and we defer to the derivation.
from core import matching as _matching
from core import org_profile as _orgp
from core import settings as _settings
from core import criteria_factors as _cf
from core import criteria_derive as _cderive
_org_prof = _orgp.get_profile()
_org_set = _settings.get_org()
_donor = None
try:
    _fa = (row.get("funding_agency") or "").strip()
    if _fa:
        # Robust acronym/short/full-name resolution so the funder joins its donor intel
        # (an exact ilike missed e.g. "Grand Challenges" → "Bill & Melinda Gates
        # Foundation"), making the donor-intel fallback (HQ, scope, priorities) available.
        from core.donor_intel import match_donor as _match_donor
        _donor = _match_donor(_fa, fuzzy=False)
except Exception:
    _donor = None
# Fold the stored LLM call-flags (compliance_flags) into an EFFECTIVE donor so the
# Review's live derivation sees call-detected signals (experience, requires-PI,
# entity-type, prior-beneficiary, …) — consistent with scan-time scoring. `_donor`
# (real match) is kept only for the "no funder profile" note.
import json as _json
try:
    _rfp_flags = _json.loads(row.get("call_compliance_flags") or "{}")
    if not isinstance(_rfp_flags, dict):
        _rfp_flags = {}
except Exception:
    _rfp_flags = {}
_donor_eff = _cderive._merge_rfp_compliance(_donor, _rfp_flags)
try:
    _derived = _cderive.derive_criteria(row, _org_prof, _donor_eff, _org_set)
except Exception:
    _derived = {}
_reviewed = bool(str(row.get("decision_date") or "").strip())   # genuine human Review save


def _baseline_val(key):
    """Criterion value shown in BOTH view & edit — the LIVE derivation, never the
    stored column.

    It used to return the stored value whenever `decision_date` was stamped, on the
    reasoning that a reviewed row's human answer must persist. But the component panel
    beneath the label is recomputed on every render, so the stored label FROZE while its
    own components moved: PREFER-9 read "Tight but doable, with a team" next to
    components at 2/2 · 100% (the bid-time component flips to 1.0 once the bid is in).
    That affected all nine criteria, not just PREFER-9.

    A human verdict is still authoritative — it is just recorded at COMPONENT level now
    (`criteria_component_overrides`), where it both moves the label and stays visible as
    "set by reviewer" in the panel. The stored column remains the historical record of
    what was answered at submit time. See core.criteria_review.criterion_label."""
    return _derived.get(key) or row.get(key)


# PERSISTENT SYSTEM decision — computed from the pure DERIVED criteria (the system's
# own read), NOT the human-editable values. It does NOT change as the reviewer edits
# the criteria; that's what makes the system-vs-human comparison meaningful. (The Bid
# Strength gauge number IS live and DOES move with edits.)
try:
    _sysvals = {k: (_derived.get(k) or row.get(k)) for k in CRITERIA}
    _pm = _matching.composite_match({**row, **_sysvals}, _org_prof, _donor_eff, _org_set)
    _pcomp = round(_pm["composite"], 1)          # 100% of the 9 weighted criteria
    _pfatal, _ = _cderive.fatal_decline(_org_prof, row, _donor_eff, _org_set)
    _sys_dec = ("Decline" if _pfatal else
                "Proceed" if _pcomp >= 90 else "Park" if _pcomp >= 70 else "Decline")
except Exception:
    _sys_dec = row.get("auto_recommendation") or "Park"


# -----------------------------------------------------------------------------
# Header + key details
# -----------------------------------------------------------------------------
DECISION_COLOR = {
    "Proceed": ("#dcf5e3", "Proceed"),
    "Proceed as sub": ("#dcf5e3", "Proceed as sub"),
    "Park": ("#fff4cc", "Park"),
    "Decline": ("#fde2e2", "Decline"),
}
# Top-right chip = the human's FINAL decision when the row was genuinely reviewed
# (decision_date stamped → persists the override); otherwise the LIVE system
# decision (`_sys_dec`), so it always agrees with the gauge suggestion and the
# auto-decision — never a stale auto-score snapshot.
if _reviewed and str(row.get("decision") or "").strip():
    _chip_dec = str(row.get("decision"))
else:
    _chip_dec = _sys_dec
bg, _ = DECISION_COLOR.get(_chip_dec, ("#eee", _chip_dec))
label = _chip_dec or "Pending"

hcol1, hcol2 = st.columns([4, 1])
hcol1.subheader(row["opportunity_title"])
hcol1.caption(f"UID `{row['uid']}` · Funder: **{row.get('funding_agency') or '—'}**")
hcol2.markdown(
    f"<div style='background:{bg};padding:14px 18px;border-radius:8px;"
    f"text-align:center;font-weight:600;font-size:1.05rem;margin-top:6px'>{label}</div>",
    unsafe_allow_html=True,
)


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if isinstance(v, list):
        # normalise via _as_list so double-encoded values (a list whose element is
        # itself a JSON-stringified list, e.g. ['["Sub-Saharan Africa"]']) render clean
        return ", ".join(_cderive._as_list(v)) or "—"
    return str(v)


def _safe_str(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v)


# Detail layout — ONE CSS-grid block so we control it precisely: Key details
# spans both rows on the left; Value / Focus Areas / Geographic Scope are equal
# cards on row 1; Brief description spans row 2 beside Key details. Value is
# currency-formatted with an inline USD conversion when not already USD.
import html as _html

def _esc(v) -> str:
    # Escape HTML, and neutralise "$" so Streamlit's markdown doesn't render
    # "$2 million … $50k" as a LaTeX math block (the garbled-italics bug).
    return _html.escape(_fmt(v)).replace("$", "&#36;")

_CCY = {"USD": ("US", "$"), "EUR": ("EU", "€"), "GBP": ("GB", "£")}


def _value_html() -> str:
    raw = row.get("call_award_value")
    try:
        amt = float(raw)
        if pd.isna(amt):
            amt = 0.0
    except (TypeError, ValueError):
        amt = 0.0
    if amt <= 0:
        return "—"
    code = (str(row.get("currency")).strip().split()[0].upper()
            if row.get("currency") else "USD") or "USD"
    pre, sym = _CCY.get(code, (code, ""))
    orig = f"{pre} {sym}{amt:,.0f}" if sym else f"{pre} {amt:,.0f}"
    if code != "USD":
        usd = amt * dropdowns.usd_rate(row.get("currency"))
        return (f"{orig} <span style='color:#8a8a8a'>/ &asymp;US ${usd:,.0f}</span>")
    return orig


def _kd(label: str, value: str) -> str:
    return (f"<div style='margin-bottom:12px'>"
            f"<div style='font-weight:700;color:#243524'>{label}</div>"
            f"<div style='color:#5a5a5a'>{value}</div></div>")


_kd_rows = (
    _kd("Applicant role", _esc(row.get("applicant_role")))
    + _kd("Window", _esc(row.get("funding_window")))
    + _kd("Deadline", _esc(row.get("call_submission_deadline")))
    + _kd("Award date", _esc(row.get("expected_award_date")))
    + _kd("Duration", f"{_esc(row.get('project_duration'))} mo")
)

from core.records import clean_brief as _clean_brief
# Display guard: never show a RAW attachment/legalese dump ("[General_conditions.pdf] …
# 1.1 …"). clean_brief strips the attachment tag and returns "" when the stored brief is
# still raw boilerplate (old pre-synthesis rows), so we fall back to a neutral line + the
# call link instead of contract clauses. New rows carry a synthesised brief and pass through.
_clean = _clean_brief(row.get("brief_description"), row.get("raw_text"))
_brief = (_esc(_clean).rstrip() if _clean
          else "<span style='color:#8a8a8a'>Summary not yet available — open the call for "
               "full details.</span>")
if row.get("opportunity_link"):
    _brief += (f" <a href='{_html.escape(str(row['opportunity_link']))}' "
               f"target='_blank' style='white-space:nowrap'>Learn more&hellip; &#8599;</a>")

_CARD = ("background:#fff;border:1px solid #e6e6e6;border-radius:10px;"
         "padding:14px 16px")
_HDR = "font-weight:700;color:#16734a;margin-bottom:8px;font-size:0.95rem"
st.markdown(
    f"""
<div style="display:grid;grid-template-columns:1.25fr 1fr 1fr 1fr;gap:12px;align-items:stretch">
  <div style="{_CARD};grid-row:span 2">
    <div style="{_HDR}">Key details</div>
    {_kd_rows}
  </div>
  <div style="{_CARD}">
    <div style="{_HDR}">Value</div>
    <div style="font-size:1.02rem;color:#222">{_value_html()}</div>
  </div>
  <div style="{_CARD}">
    <div style="{_HDR}">Focus Areas</div>
    <div style="color:#333">{_esc(row.get('call_domain_areas'))}</div>
  </div>
  <div style="{_CARD}">
    <div style="{_HDR}">Geographic Scope</div>
    <div style="color:#333">{_esc(row.get('call_geographic_scope'))}</div>
  </div>
  <div style="{_CARD};grid-column:2 / span 3">
    <div style="{_HDR}">Brief description</div>
    <div style="color:#333;line-height:1.55">{_brief}</div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

st.divider()


# -----------------------------------------------------------------------------
# Inline-editable eligibility grid + live gauge
# -----------------------------------------------------------------------------
LABELS = {
    "qualification": "MUST 1 · Legal status & qualification",
    "strategic_fit":  "MUST 2 · Strategic fit",
    "capacity":  "MUST 3 · Implementation capacity",
    "geographic_fit":      "MUST 4 · Geographic fit",
    "cofinancing":     "MUST 5 · Cofinancing & compliance",
    "funding_quality": "PREFER 6 · Funding quality",
    "funder_relationship":     "PREFER 7 · Donor relationship",
    "competitiveness":     "PREFER 8 · Competitiveness",
    "bid_effort":           "PREFER 9 · Bid effort",
}


def _coerce_elig(v, key: str) -> str:
    """Pre-select the best per-criterion response for a stored value — maps
    legacy True/Partial/False by score and passes new rich labels through
    (core.scorer.default_response)."""
    try:
        if v is not None and pd.isna(v):
            v = None
    except (TypeError, ValueError):
        pass
    return default_response(key, v)


# Detect if this is a scraped/automated submission (no human criteria yet)
is_scraped = (row.get("source") or "").lower() in {"scraper", "scrape", "auto", "rss"}
stored_has_values = any(
    not (v is None or (isinstance(v, float) and pd.isna(v)))
    for v in [row.get(k) for k in CRITERIA]
)

# (Org/donor context + `_derived` + `_reviewed` + `_baseline_val` are computed at
# the top, before the header — single source of truth.)

# Seed widget session_state on first render of this UID. The nine criterion widgets are
# NOT seeded any more: there is no criterion-level widget to seed — each criterion is
# edited through its components, and every component widget takes its own default from the
# live derivation on each render. Sentinel seeds once per UID per session.
_seed_sentinel = f"_review_seeded_v6_{row['uid']}"
if _seed_sentinel not in st.session_state:
    st.session_state[f"decline_{row['uid']}"] = "Yes" if row.get("decline_flags_present") else "No"
    st.session_state[f"risks_{row['uid']}"] = _safe_str(row.get("key_risks"))
    _stored_dec = row.get("decision") or row.get("auto_recommendation")
    if _stored_dec:
        st.session_state[f"dec_{row['uid']}"] = _stored_dec
    st.session_state[f"rat_{row['uid']}"] = _safe_str(row.get("decision_note"))
    st.session_state[_seed_sentinel] = True

# VIEW-FIRST: everything is read-only until the reviewer clicks "Update Decision".
# Save / Cancel appear only in edit mode; saving returns to the plain display.
_edit_key = f"_review_edit_{row['uid']}"
edit_mode = can_edit and bool(st.session_state.get(_edit_key))


def _exit_edit_and_reset():
    """Leave edit mode and drop in-progress widget state so it re-seeds from DB.

    The per-component selections and their touched-flags MUST go too: they are what
    decides which values get persisted as a human verdict, so an abandoned edit that
    survived here would be saved as somebody's answer the next time this row was
    touched."""
    _uid = row["uid"]
    for _k in CRITERIA:
        _clear_comp_edits(_uid, _k, _bd.get(_k) or [])
    for _p in ("decline_", "risks_", "dec_", "rat_"):
        st.session_state.pop(f"{_p}{_uid}", None)
    st.session_state.pop(_seed_sentinel, None)
    st.session_state[_edit_key] = False


_BADGE = {2: "🟢", 1: "🟡", 0: "🔴"}


def _crit_badge(label_val) -> str:
    # "Not sure" / undetermined (criterion_score None) → 🟡 (Park), not ⚪ — an
    # unknown criterion routes to Park, value 1 (owner 2026-06-29).
    return _BADGE.get(criterion_score(label_val), "🟡")


# (Org/donor context + the live derivation `_derived` are computed ABOVE, before
# the seed block, so BOTH view and edit baseline the criteria from the DERIVED
# values and the two screens always show the same numbers.)

# Header only — the Update Decision toggle lives on the "Rate this RFP" row at the
# bottom of the screen (Save / Cancel appear in the Team-decision section).
st.markdown("**Eligibility criteria**  "
            + ("_— editing; Save or Cancel below_"
               if edit_mode else "_— score · winning factors / total · confidence_"))

# Fatal verdict + factor breakdown — computed ONCE here (single source) so each
# criterion's collapsible card and the gauge all read the same numbers.
from core import criteria_review as _crev   # roll-up rules + label/count (testable)
_OR_KEYS = _crev.OR_KEYS   # geographic_fit is now a single tiered component
# Component verdict symbol — SCORE-driven, and shared with the factor model so the
# symbol and the score can't drift apart. "?" means UNDETERMINED (the call stated
# nothing → excluded from the count); a MEASURED partial (0.5, e.g. a partial priority
# match) gets ◐ instead, because "we don't know" and "we know, and it's halfway" are
# different answers. Applies to EVERY criterion, not just MUST-2 (owner 2026-08-06).
# See core.criteria_derive.component_mark.
_mark = _cderive.component_mark

# Persisted HUMAN component verdicts (migration 087). Merged over the derived breakdown
# so a reviewer's answer beats the inference — the derivation reads org profile / donor
# intel / call text and can be wrong or stale; a human who read the call is authoritative.
_overrides = row.get("criteria_component_overrides")
if isinstance(_overrides, str):
    try:
        _overrides = _json.loads(_overrides or "{}")
    except Exception:
        _overrides = {}
if not isinstance(_overrides, dict):
    _overrides = {}
try:
    _is_fatal, _trigger = _cderive.fatal_decline(_org_prof, row, _donor_eff, _org_set)
    _bd = _cderive.factor_breakdown(row, _org_prof, _donor_eff, _org_set,
                                    overrides=_overrides)
except Exception:
    _is_fatal, _trigger, _bd = False, None, {}


def _factor_html(ckey: str, label=None) -> str:
    """Component pass/fail rows for ONE criterion — the body of its collapsible
    card. Shows EVERY sub-factor: ✓ met · ◐ partly met (measured, between pass and
    fail) · ✗ failed · ? not-applicable (greyed, not required by this call → excluded
    from the denominator). OR-criteria show the satisfied path ✓ and the unused
    alternatives as neutral ○."""
    facts = _bd.get(ckey) or []
    if not facts:
        return "<span style='color:#999;font-size:0.82rem'>No sub-factors.</span>"
    if ckey == "strategic_fit":
        # MUST-2 is ONE component "Strategic priority fitness" (0/0.5/1); the matched/
        # detected theme counts + terms are shown as an info line (themes are NOT
        # separate components).
        _a = [f for f in facts if f.get("active", True)]
        if not _a:
            return ("<span style='color:#8a6d00;font-size:0.82rem'>? Not sure — no "
                    "funder strategy / theme data stated.</span>")
        _it = _a[0]
        _sc = _it.get("score") or 0
        _sym, _col = _mark(_it)
        _bl = {1.0: "strong priority match", 0.5: "partial priority match",
               0.0: "off-strategy"}.get(_sc, str(_sc))
        _l1 = (f"<div style='font-size:0.82rem;margin:2px 0'>"
               f"<span style='color:{_col};font-weight:700'>{_sym}</span> "
               f"Strategic priority fitness — {_sc:g} ({_bl})</div>")
        _m, _d, _terms = _it.get("_matched", 0), _it.get("_detected", 0), (_it.get("_terms") or "")
        _l2 = (f"<div style='font-size:0.78rem;color:#666;margin:2px 0'>"
               f"Matched {_m} of {_d} funder theme(s)"
               + ((": " + _esc(_terms)) if _terms else "") + "</div>")
        return _l1 + _l2
    if ckey == "geographic_fit":
        # MUST-4 — ONE tiered component: own presence / via a partner / no presence.
        _a = [f for f in facts if f.get("active", True)]
        _it = _a[0] if _a else None
        if not _it:
            return ("<span style='color:#8a6d00;font-size:0.82rem'>? Not sure — no "
                    "geographic scope stated by the call/donor.</span>")
        _sc = _it.get("score") or 0
        _sym, _col = _mark(_it)
        _lbl = {1.0: "Yes, our own presence", 0.5: "Yes, via a partner"}.get(
            _sc, "No presence there")
        _via = _it.get("_via") or ""
        _scope = _it.get("_scope") or ""
        _l1 = (f"<div style='font-size:0.82rem;margin:2px 0'>"
               f"<span style='color:{_col};font-weight:700'>{_sym}</span> "
               f"Geographic presence — {_lbl}"
               + (f" <span style='color:#888'>({_esc(_via)})</span>" if _via else "")
               + "</div>")
        _l2 = (f"<div style='font-size:0.78rem;color:#666;margin:2px 0'>"
               f"Call/donor scope: {_esc(_scope) if _scope else '—'}</div>")
        return _l1 + _l2
    # MUST-5 all-clear: when the call/donor imposed nothing, that single row IS the
    # answer and the eleven greyed "not stated by this call" rows beneath it are noise.
    # Show them only once a real requirement has been detected (owner 2026-08-06).
    if any(f.get("key") == "compliance_all_clear" and f.get("active") for f in facts):
        facts = [f for f in facts if f.get("active")]
    is_or = ckey in _OR_KEYS
    any_met = any(f["met"] is True for f in facts if f.get("active", True))
    out = []
    for f in facts:
        if not f.get("active", True):
            sym, col, suffix = "?", "#b8860b", (" <span style='color:#aaa'>"
                "(Not sure — not stated by this call; excluded from the count)</span>")
        elif f.get("_detail") is not None:
            # Graded component (track record, financial capacity, experience bar):
            # band symbol + the measurement behind it.
            sym, col = _mark(f)
            suffix = f" <span style='color:#888'>— {_esc(f['_detail'])}</span>"
        elif is_or and any_met and f["met"] is not True:
            sym, col, suffix = "○", "#999", (" <span style='color:#aaa'>"
                "(alternative route — not needed)</span>")
        else:
            sym, col = _mark(f)
            suffix = (" <span style='color:#aaa'>(no restriction — defaults to pass)</span>"
                      if f.get("default") else "")
        lock = " 🔒" if f.get("fatal") else ""
        # A reviewer's saved verdict beats the derivation — say so, so nobody wonders why
        # the card disagrees with the underlying profile/donor data.
        human = (" <span style='color:#1a7f37;font-size:0.72rem'>· set by reviewer</span>"
                 if f.get("_override") else "")
        out.append(f"<div style='font-size:0.82rem;margin:2px 0'>"
                   f"<span style='color:{col};font-weight:700'>{sym}</span> "
                   f"{_esc(f['name'])}{lock}{suffix}{human}</div>")
    out.append("<div style='color:#aaa;font-size:0.72rem;margin-top:6px'>"
               "✓ met · ◐ partly met (measured, between pass and fail) · ✗ failed · "
               "? Not sure (not stated — excluded) · ○ alt-route · "
               "🔒 fatal gate (failing it auto-Declines) · "
               "<span style='color:#1a7f37'>set by reviewer</span> = human verdict, "
               "overrides the system</div>")
    # PREFER-6 / PREFER-8 are named by their own weighted model, not by this ratio, so the
    # two can legitimately differ. Say why — otherwise the card reads exactly like the
    # frozen-label defect it is not (both numbers here are live).
    _note = _crev.label_source_note(ckey, facts, label)
    if _note:
        out.append("<div style='margin-top:8px;border-left:3px solid #1f7a8c;"
                   "background:#eef7fa;padding:7px 10px;border-radius:6px;"
                   f"color:#155e6b;font-size:0.78rem'>{_esc(_note)}</div>")
    return "".join(out)




def _crit_label_color(lbl: str) -> str:
    """Dynamic colour for a classification label: green (2) / amber (1) / red (0) / grey."""
    return {2: "#1a7f37", 1: "#b8860b", 0: "#c0392b"}.get(criterion_score(lbl), "#777")


def _apply_component_writethrough(comp_scores: dict, donor_eff: dict | None,
                                  rfp_row: dict, by_email: str | None) -> list[str]:
    """Push donor-list-backed component verdicts back into the ORG PROFILE so the fix is
    durable and the derivation agrees on the next render (see core.criteria_writethrough:
    these components read a profile donor list, so saving the RFP alone changes nothing).
    Returns human-readable notes; empty = nothing changed, nothing written."""
    from core import criteria_writethrough as _cwt
    prof = _orgp.get_profile()
    changes, notes = _cwt.plan_writethrough(
        comp_scores, prof, donor_eff, rfp_row, _cderive._canonical_donor_match)
    if changes:
        prof.update(changes)
        _orgp.set_profile(prof, updated_by=by_email)
    return notes


# The edit-mode component editor is a Streamlit widget, so it lives in its own module —
# importable, and therefore drivable by streamlit.testing.AppTest (this page runs the auth
# gate at import time, so the editor could not be reached from a test at all).
from views.criteria_editor import (
    clear_session_edits as _clear_comp_edits,
    render_component_editor as _render_comp_editor,
)


grid_col, gauge_col = st.columns([3, 2])

with grid_col:
    if edit_mode and not stored_has_values and is_scraped:
        st.caption("_Automated scan — no criteria scored yet. Pick a response for "
                   "each criterion below, then Save._")
    g1, g2 = st.columns(2)
    edited_values: dict[str, str] = {}
    # Per-component scores captured during render, so Save can WRITE THROUGH the ones
    # backed by an org-profile field (see _WRITE_THROUGH below).
    _component_scores: dict[str, dict[str, float]] = {}
    for i, key in enumerate(CRITERIA):
        target = g1 if i < 5 else g2
        # SINGLE SOURCE OF TRUTH for the label, in BOTH modes: it is COMPUTED, never read
        # from the stored column. The derivation names the criterion unless a reviewer has
        # overridden one of its components, in which case their verdict does — so the
        # label can never contradict the component panel printed underneath it. See
        # core.criteria_review.criterion_label.
        _items = _bd.get(key) or []          # ALL components (active + inactive)
        current = _coerce_elig(
            _crev.criterion_label(key, _items, _baseline_val(key)), key)
        with target:
            if edit_mode:
                edited_values[key] = _render_comp_editor(
                    row["uid"], key, LABELS[key], _items, _baseline_val(key),
                    collect=_component_scores)
            else:
                edited_values[key] = current   # feeds the live gauge
                # Each criterion is its OWN collapsible card — click to expand and
                # see the component sub-factors (✓/✗/?) behind it. Title is BOLD (no
                # colour); the value LABEL is colour-coded. "Not sure" (no active
                # component → value 1 / Park) reads amber, NOT grey/red.
                _is_not_sure = criterion_score(current) is None
                _vc = ("orange" if _is_not_sure else
                       {2: "green", 1: "orange", 0: "red"}.get(criterion_score(current), "gray"))
                _ratio = _crev.count_text(key, _items, current, _is_not_sure)
                with st.expander(
                        f"{_crit_badge(current)}  **{LABELS[key]}** — "
                        f":{_vc}[{current or 'Not sure'}]  ·  {_ratio}"):
                    st.markdown(_factor_html(key, current), unsafe_allow_html=True)

# Composite org × donor × RFP match: Bid Strength = 100% of the 9 weighted criteria
# (MUST .65 + PREFER .35), with the hard MUST/fatal gate. (The old 20% donor-org
# extras were dropped 2026-06-29 — they duplicated MUST-2/4/5-route/PREFER-7.)
# (org / donor / settings already fetched above the grid)
_match = _matching.composite_match({**row, **edited_values}, _org_prof, _donor_eff, _org_set)


_MUST_KEYS = CRITERIA[:5]


def _review_decision(crit_vals: dict, composite: float, fatal: bool = False,
                     below_award_floor: bool = False) -> str:
    """Mirror auto_scorer.recommend_from_composite EXACTLY so the gauge's
    suggestion always matches the stored Auto-decision: a 🔒 non-dynamic factor
    explicitly failed → Decline; else ≥90 Proceed · 70–89 Park · <70 Decline. A
    below-award-floor call (funding below the org's minimum target) caps a would-be
    Proceed at Park (2026-07-28). (2026-06-26: replaced the old blanket 'any MUST<2'.)"""
    if fatal:
        return "Decline"
    rec = "Proceed" if composite >= 90 else "Park" if composite >= 70 else "Decline"
    if rec == "Proceed" and below_award_floor:
        return "Park"
    return rec


with gauge_col:
    # ONE consistent calculation: round the two components to 1dp, then compute the
    # composite from THOSE — so the displayed arithmetic reconciles exactly and the
    # gauge/box agree. Bid Strength shown as a half-up rounded integer (92.5→93).
    _crit_s = round(_match["criteria_score"], 1)
    _comp = round(_match["composite"], 1)               # = the 9 weighted criteria
    _comp_int = int(_comp + 0.5)                        # round half up
    _dec = _review_decision(  # SAME rule as the stored Auto-decision
        edited_values, _comp, fatal=_is_fatal,
        below_award_floor=_cderive.below_award_floor(row, _org_prof))
    _pill = {"Proceed": ("#dcf5e3", "#00703C"), "Park": ("#fff4cc", "#8a6d00"),
             "Decline": ("#fde2e2", "#b3261e")}.get(_dec, ("#eee", "#333"))
    _fit = ("Strong fit" if _comp >= 80 else "Moderate fit" if _comp > 50 else "Low fit")

    # CONFIDENCE — how much DATA backs this prediction (donor mapping + call extraction).
    # A "Proceed" on a 30%-mapped donor is shakier than one on 90% — surface it so the
    # reviewer can weight the suggestion. (E3c: data quality → prediction confidence.)
    from core import data_quality as _dq2
    _dpct, _, _ = _dq2.donor_completeness(_donor)
    _cpct, _, _ = _dq2.call_completeness(row)
    _band, _bpct = _dq2.confidence_band(_dpct, _cpct)
    _bcol = {"High": "#00703C", "Medium": "#8a6d00", "Low": "#b3261e"}[_band]
    # E3d: LOW confidence widens the Park band — a thin-data Proceed parks for review.
    _sug_adj, _conf_note = _dq2.confidence_adjusted(_sys_dec, _band)

    # GREEN BOX — Bid Strength is the BOLD headline, placed ABOVE the meter; the
    # system suggestion + confidence take the smaller secondary style below it.
    st.markdown(
        f"<div style='text-align:center;background:{_pill[0]};border-radius:10px;"
        f"padding:10px 14px;margin-bottom:10px'>"
        f"<span style='color:{_pill[1]};font-weight:700;font-size:1.15rem'>"
        f"Bid Strength {_comp_int}/100 — {_fit}</span>"
        + (f"<div style='color:#b3261e;font-size:0.8rem;margin-top:4px'>⚠ Fatal gate: "
           f"{_esc(_trigger)} → Decline.</div>" if _is_fatal else "")
        + f"<div style='color:#3a3a3a;font-size:0.9rem;margin-top:5px'>"
          f"System suggestion: <b>{_sug_adj}</b>"
          + (f" <span style='color:#8a6d00'>(was {_esc(_sys_dec)})</span>"
             if _sug_adj != _sys_dec else "") + "</div>"
        # "donor 0%" meant two different things — no funder profile at all, or a profile
        # with nothing researched. Say which, so the reviewer knows whether to fix the
        # funder name or fill in the donor record.
        + f"<div style='color:{_bcol};font-size:0.78rem;margin-top:4px'>"
          f"Confidence: <b>{_band}</b> · data {_bpct}% "
          f"(donor {f'{_dpct}%' if _dq2.donor_matched(_donor) else 'no funder profile'}"
          f" · call {_cpct}%)</div>"
        + (f"<div style='color:#8a6d00;font-size:0.74rem;margin-top:3px'>⚠ {_esc(_conf_note)}</div>"
           if _conf_note else "")
        + "</div>",
        unsafe_allow_html=True,
    )

    fig = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=_comp_int,
            title={"text": "Bid Strength", "font": {"size": 14}},
            domain={"x": [0, 1], "y": [0.2, 1]},
            number={"suffix": " / 100", "font": {"size": 32}},
            gauge={
                "axis": {"range": [0, 100], "tickwidth": 1},
                "bar": {"color": "#00703C", "thickness": 0.25},
                "bgcolor": "white",
                "borderwidth": 0,
                "steps": [
                    {"range": [0, 50],  "color": "#fde2e2"},
                    {"range": [50, 80], "color": "#fff4cc"},
                    {"range": [80, 100], "color": "#dcf5e3"},
                ],
            },
        )
    )
    fig.update_layout(height=250, margin=dict(t=10, b=10, l=20, r=20))
    st.plotly_chart(fig, width='stretch')

# ── Aligned row: Decline flags / Key risks (LEFT) ⟷ How Bid Strength is
# calculated (RIGHT) — same row, so the two cards share the same top edge.
dl_col, calc_col = st.columns([3, 2])

with dl_col:
    if edit_mode:
        decline_in = st.radio(
            "Decline flags?", ["No", "Yes"], horizontal=True,
            index=1 if row.get("decline_flags_present") else 0,
            key=f"decline_{row['uid']}")
        risks_in = st.text_input(
            "Key risks (one line)", value=_safe_str(row.get("key_risks")),
            key=f"risks_{row['uid']}")
    else:
        decline_in = "Yes" if row.get("decline_flags_present") else "No"
        risks_in = _safe_str(row.get("key_risks"))
        _risk_disp = (_esc(row.get("key_risks"))
                      if str(row.get("key_risks") or "").strip() else "—")
        # Same card + green-header styling as Key details / Value / Focus Areas.
        st.markdown(
            f"<div style='{_CARD}'>"
            f"<div style='{_HDR}'>Decline flags</div>"
            f"<div style='color:#5a5a5a'>{decline_in}</div>"
            f"<div style='{_HDR};margin-top:12px'>Key risks</div>"
            f"<div style='color:#5a5a5a'>{_risk_disp}</div>"
            f"</div>",
            unsafe_allow_html=True)
    # ⚠ Compliance & hard-gates the RFP imposes (LLM-extracted) — surfaced so a
    # reviewer sees a hidden gate BEFORE committing, not near the deadline.
    _comp_req = (row.get("compliance_requirements") or "").strip()
    if _comp_req and _comp_req.lower() not in ("none stated", "none", "n/a"):
        st.markdown(
            "<div style='margin-top:14px;border-left:3px solid #d9a400;"
            "background:#fff8e6;padding:8px 12px;border-radius:6px'>"
            "<span style='color:#8a6d00;font-weight:700;font-size:0.82rem'>"
            "⚠ Compliance &amp; hard-gates (from the RFP)</span><br>"
            f"<span style='font-size:0.85rem'>{_esc(_comp_req).replace(chr(10), '<br>')}"
            "</span></div>",
            unsafe_allow_html=True)
    # 🎯 Call-specific eligibility constraints (LLM-extracted) — a hidden eligibility
    # gate (e.g. "must focus on UNESCO sites") seen BEFORE committing, like compliance.
    _elig_spec = (row.get("eligibility_specifics") or "").strip()
    if _elig_spec and _elig_spec.lower() not in ("none stated", "none", "n/a"):
        st.markdown(
            "<div style='margin-top:10px;border-left:3px solid #1f7a8c;"
            "background:#eef7fa;padding:8px 12px;border-radius:6px'>"
            "<span style='color:#155e6b;font-weight:700;font-size:0.82rem'>"
            "🎯 Eligibility specifics (from the RFP)</span><br>"
            f"<span style='font-size:0.85rem'>{_esc(_elig_spec).replace(chr(10), '<br>')}"
            "</span></div>",
            unsafe_allow_html=True)

with calc_col:
    # Per-criterion contribution = weight × (criterion value ÷ 2), so the rows sum to
    # the Bid Strength. MUST .65 + PREFER .35 = 1.0. "Not sure" counts as the Park
    # midpoint (value 1 / 0.5). No 80/20 split — Bid Strength IS the 9 criteria.
    _WEIGHTS = {"qualification": .15, "strategic_fit": .15, "capacity": .15,
                "geographic_fit": .10, "cofinancing": .10, "funding_quality": .08,
                "funder_relationship": .08, "competitiveness": .10, "bid_effort": .09}

    def _contrib_row(key: str) -> str:
        # Reads `edited_values` — the SAME computed label the criterion card shows — so the
        # breakdown and the card can no longer disagree. They used to: the card read the
        # live components while this read the stored label, so MUST-1 was credited its full
        # 15.0 here while its own card reported nothing scored.
        _lbl = edited_values.get(key)
        sc = criterion_score(_lbl)
        frac = 0.5 if sc is None else sc / 2.0            # Not sure → Park midpoint
        pts = _WEIGHTS[key] * frac * 100.0
        col = "#00703C" if frac >= 1 else "#8a6d00" if frac >= 0.5 else "#b3261e"
        nm = LABELS[key].split(" · ", 1)[-1]
        # Say which rows are the Park midpoint BY DEFAULT rather than by measurement —
        # a number with nothing behind it should not look like a measured result.
        _unscored = _crev.count_text(
            key, _bd.get(key) or [], _lbl, sc is None) == _crev.NOT_SCORED
        _note = (" <span style='color:#b8860b;font-size:0.72rem'>· not scored</span>"
                 if _unscored else "")
        return (f"<div style='display:flex;justify-content:space-between;padding:1px 0'>"
                f"<span style='color:#555'>{_esc(nm)} "
                f"<span style='color:#aaa'>·{_WEIGHTS[key]:.2f}</span>{_note}</span>"
                f"<span style='color:{col};font-weight:600'>{pts:.1f}</span></div>")

    st.markdown(
        f"<div style='border:1px solid #e8e8e8;border-radius:10px;padding:12px 14px;"
        f"font-size:0.85rem;line-height:1.5'>"
        f"<div style='color:#778;font-size:0.74rem;letter-spacing:0.04em;margin-bottom:6px'>"
        f"HOW BID STRENGTH IS CALCULATED</div>"
        f"<div style='text-align:center;font-size:0.92rem;margin-bottom:8px'>"
        f"Bid Strength <b>{_comp:.1f}</b> = Σ (weight × criterion ÷ 2) × 100</div>"
        f"<div style='color:#778;font-size:0.74rem;letter-spacing:0.04em;margin-bottom:2px'>"
        f"MUST (weight .65)</div>"
        + "".join(_contrib_row(k) for k in CRITERIA[:5])
        + "<div style='color:#778;font-size:0.74rem;letter-spacing:0.04em;margin:6px 0 2px'>"
          "PREFER (weight .35)</div>"
        + "".join(_contrib_row(k) for k in CRITERIA[5:])
        + "<div style='border-top:1px solid #eee;margin:8px 0 6px'></div>"
        + "<div style='color:#888;font-size:0.76rem'>"
          "<b>Decision</b>: a 🔒 fatal gate failed (legal identity · no geographic "
          "reach · inaccessible funding route) → Decline; else Proceed ≥90 · Park "
          "70–89 · Decline &lt;70. \"Not sure\" criteria score the Park midpoint.<br>"
          "<b>Fitness label</b> (Strong/Moderate/Low): ≥80 · &gt;50–79 · ≤50 — overall "
          "match strength only; it does <i>not</i> set the decision.</div>"
        + "</div>",
        unsafe_allow_html=True,
    )

# LIVE score from the edited/derived values (decline_in is now set above).
live_score, live_rec = score_submission(edited_values, decline_in == "Yes")

st.divider()


# -----------------------------------------------------------------------------
# Decision + save
# -----------------------------------------------------------------------------
st.subheader("Team decision")

if not edit_mode:
    # ONLY the human's Final decision + rationale here. The SYSTEM suggestion lives
    # on the gauge (shown side-by-side), so we can later learn how team overrides
    # track system predictions — and, eventually, application outcomes.
    _final_disp = (row.get("decision") if _reviewed else "Pending")
    _final_col = "#00703C" if _final_disp == "Proceed" else (
        "#8a6d00" if _final_disp == "Park" else
        "#b3261e" if _final_disp == "Decline" else "#555")
    st.markdown(
        f"<div style='border:1px solid #e8e8e8;border-radius:10px;background:#fbfbfa;"
        f"padding:14px 16px;display:flex;gap:40px;flex-wrap:wrap'>"
        f"<div><span style='color:#667;font-size:0.78rem'>Final decision "
        f"<span style='color:#aaa'>(team)</span></span><br>"
        f"<b style='font-size:1.05rem;color:{_final_col}'>{_final_disp}</b></div>"
        f"<div style='flex:1;min-width:240px'><span style='color:#667;font-size:0.78rem'>"
        f"Decision rationale</span><br>{_safe_str(row.get('decision_note')) or '—'}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if can_edit:
        # Breathing room between the decision card and the Update Decision button.
        st.markdown("<div style='margin-top:18px'></div>", unsafe_allow_html=True)
        # "Update Decision" sits in the EXACT position Save changes occupies in edit mode
        # (first column of the same [1,1,3] row) — click it → Save/Cancel appear
        # here; Save → back to this button. It scores the criteria and records the team
        # verdict; "Edit RFP" beside it opens the full field-level editor.
        be1, _be2, _be3 = st.columns([1, 1, 3])
        if be1.button("✏ Update Decision", type="primary", width='stretch',
                      key=f"edit_rfp_{row['uid']}",
                      help="Score the eligibility criteria and record the team decision"):
            st.session_state[_edit_key] = True
            st.rerun()
        if _be2.button("✏️ Edit RFP", type="primary", width='stretch',
                       key=f"edit_full_{row['uid']}",
                       help="Open the shared full-RFP editor (all fields)"):
            render_rfp_editor(row, sb=sb, user=user, is_admin=is_admin)
else:
    decisions = dropdowns.get("decisions")
    choices = list(decisions)
    current_dec = row.get("decision") or live_rec
    if current_dec and current_dec not in choices:
        choices = [current_dec] + choices

    dc1, dc2 = st.columns([1, 3])
    new_decision = dc1.selectbox(
        "Decision", choices,
        index=choices.index(current_dec) if current_dec in choices else 0,
        key=f"dec_{row['uid']}")
    new_rationale = dc2.text_area(
        "Decision rationale (2-3 lines)", value=_safe_str(row.get("decision_note")),
        key=f"rat_{row['uid']}", height=90)

    # PREFER-7 "Donor already engaged" (migration 091). A HUMAN answer: nothing the
    # crawler sees can tell us whether someone has approached this funder about THIS
    # call. Unanswered stays out of PREFER-7 entirely rather than scoring 0.
    _eng_opts = {"— not answered": None,
                 "Yes — we have engaged this funder about this opportunity": "yes",
                 "Partial — via a third party on our behalf": "partial",
                 "No — no contact about this opportunity": "no"}
    _eng_cur = str(row.get("donor_engaged") or "").strip().lower()
    _eng_labels = list(_eng_opts)
    _eng_idx = next((i for i, v in enumerate(_eng_opts.values()) if v == (_eng_cur or None)), 0)
    new_engaged = _eng_opts[st.selectbox(
        "Donor already engaged on this opportunity?", _eng_labels, index=_eng_idx,
        key=f"eng_{row['uid']}",
        help="Has anyone approached this funder about THIS call — a meeting, a concept "
             "note, an EOI? The system can't see this, so it only counts once you "
             "answer. 'Partial' covers contact made through a third party on our "
             "behalf. Leave unanswered and it is excluded from Donor relationship.")]

    bsave, bcancel, _bspace = st.columns([1, 1, 3])
    if bsave.button("💾 Save changes", type="primary", width='stretch'):
        update = {
            **edited_values,
            "decline_flags_present": decline_in == "Yes",
            "key_risks": (risks_in.strip() or None),
            "alignment_score": live_score,
            "auto_recommendation": live_rec,
            "decision": new_decision,
            "decision_note": new_rationale.strip() or None,
            "donor_engaged": new_engaged,
            "decision_date": date.today().isoformat(),
            "decision_overridden_by": user.get("email"),
            "decision_overridden_at": datetime.now(timezone.utc).isoformat(),
        }
        # Persist ONLY the components the reviewer actually SET, merged over anything saved
        # before, so the derivation still drives everything else (migration 087).
        #
        # `_component_scores[k]` now holds exactly the reviewer's explicit values —
        # per-COMPONENT, from the on_change flag. It used to hold every ACTIVE component's
        # value for any criterion with one edit, which froze the derived score of
        # components nobody had looked at: a later scoring fix could then never reach them.
        _new_ov = {k: dict(v) for k, v in (_overrides or {}).items() if isinstance(v, dict)}
        for _k in CRITERIA:
            _set = _component_scores.get(_k) or {}
            if _set:
                _new_ov.setdefault(_k, {}).update(_set)
        if _new_ov:
            update["criteria_component_overrides"] = _new_ov
        sb.table("rfp_submissions").update(update).eq("uid", row["uid"]).execute()
        # Donor-list-backed components (e.g. "Authorized signatory (this donor)") are
        # DERIVED from the org profile, so saving the RFP alone can't change them. Push the
        # reviewer's verdict into the profile field it reads from, or the component silently
        # reverts on the next render.
        try:
            _wt_notes = _apply_component_writethrough(
                _component_scores, _donor_eff, row, user.get("email"))
            if _wt_notes:
                st.success("Org profile updated — " + "; ".join(_wt_notes) + ".")
                st.cache_data.clear()          # profile feeds cached derivations
        except Exception as _wexc:
            st.warning(f"Saved the RFP, but couldn't update the org profile: {_wexc}")
        # ML Phase 1/3 — capture the human decision as a labeled signal (this Review
        # screen is a second decision path alongside Records). Dedup in log_decision
        # keeps one current label per record across both paths.
        if new_decision:
            try:
                from core import decision_log
                decision_log.log_decision({**row, **update}, new_decision,
                                          by=user.get("email"))
            except Exception:
                pass
        _exit_edit_and_reset()        # back to the plain display
        st.cache_data.clear()
        st.success(
            f"Saved {row['uid']} · score {live_score:.1f} → **{live_rec}** · "
            f"decision **{new_decision}**.")
        st.rerun()
    if bcancel.button("Cancel", width='stretch'):
        _exit_edit_and_reset()
        st.rerun()


# -----------------------------------------------------------------------------
# Quick feedback — ANY reviewer in the meeting can rate (no admin needed). Feeds
# the learning engine; mirrors the 👍/😐/👎 on the Records page. 3-way so Park
# (Neutral) doesn't skew the signal.
# -----------------------------------------------------------------------------
st.markdown("**Rate this RFP** — quick training signal for the learning engine")
fb1, fb2, fb3, _fbsp = st.columns([1, 1, 1, 5])
_fb_good = fb1.button("👍 Good", key=f"fb_good_{row['uid']}", width='stretch',
                      help="Like a Proceed — a strong match.")
_fb_neutral = fb2.button("😐 Neutral", key=f"fb_neutral_{row['uid']}", width='stretch',
                         help="Like a Park — unclear / needs more info.")
_fb_bad = fb3.button("👎 Bad", key=f"fb_bad_{row['uid']}", width='stretch',
                     help="Like a Decline — poor match.")
if _fb_good or _fb_neutral or _fb_bad:
    _verdict = "good" if _fb_good else ("neutral" if _fb_neutral else "bad")
    try:
        from core import decision_log
        decision_log.log_feedback(row, _verdict, by=user.get("email"))
        st.toast({"good": "👍 Marked good", "neutral": "😐 Marked neutral",
                  "bad": "👎 Marked bad"}[_verdict]
                 + " — thanks, this trains the scorer.", icon="🧠")
    except Exception as exc:
        st.warning(f"Couldn't record feedback: {exc}")

st.divider()


# -----------------------------------------------------------------------------
# Diagnostic — raw stored values for the current RFP
# -----------------------------------------------------------------------------
with st.expander("🔍 Stored values (raw, from database)", expanded=False):
    st.caption(
        "What's currently saved in Supabase for this row. If the dropdowns above "
        "show '—' but you remember filling in values, compare to this table — "
        "if it's blank here, the data isn't in the DB (likely never saved)."
    )
    raw = {
        "uid": row["uid"],
        "source": row.get("source"),
        "decision": row.get("decision"),
        "alignment_score": row.get("alignment_score"),
        "auto_recommendation": row.get("auto_recommendation"),
        "decline_flags_present": row.get("decline_flags_present"),
        "key_risks": row.get("key_risks"),
        **{k: row.get(k) for k in CRITERIA},
    }
    diag_df = pd.DataFrame(
        [(k, ("(empty)" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v)))
         for k, v in raw.items()],
        columns=["Field", "Stored value"],
    )
    st.dataframe(diag_df, width='stretch', hide_index=True)
