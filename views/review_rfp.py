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
    CRITERIA, CRITERION_RESPONSES, criterion_score, default_response, score_submission,
)
from core.records import clean_df, drop_concluded
from db.supabase_client import get_client, safe_execute

# auth handled by wrapper page
user = st.session_state["app_user"]
role = user.get("role", "collaborator")
can_edit = role in ("super_user", "admin", "reviewer")
sb = get_client()

year = settings.get_year()
st.markdown(
    f"<h2 style='font-size:1.55rem;font-weight:700;color:#334155;"
    f"margin:0.15rem 0 0.5rem;'>Weekly Triage Pipeline ({year})</h2>",
    unsafe_allow_html=True,
)


# -----------------------------------------------------------------------------
# Week + data
# -----------------------------------------------------------------------------
all_weeks = all_weeks_for_year(year)
default_week = review_week_label()
if default_week not in all_weeks:
    all_weeks = [default_week] + all_weeks

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
    """Criterion value shown in BOTH view & edit: the human's saved value when the
    row was genuinely reviewed (persists overrides), else the live derivation."""
    stored = row.get(key)
    if _reviewed and stored not in (None, ""):
        return stored
    return _derived.get(key) or stored


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

from core.records import strip_html as _strip_html
_brief = _esc(_strip_html(row.get("brief_description"))).rstrip()
if row.get("opportunity_link"):
    _brief += (f" <a href='{_html.escape(str(row['opportunity_link']))}' "
               f"target='_blank' style='white-space:nowrap'>Opportunity link &#8599;</a>")

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

# Seed widget session_state on first render of this UID — from the BASELINE values
# (human-reviewed → stored; else derived) so edit mode starts from exactly what
# view shows. Sentinel seeds once per UID per session so edits aren't clobbered.
_seed_sentinel = f"_review_seeded_v5_{row['uid']}"
if _seed_sentinel not in st.session_state:
    for _k in CRITERIA:
        st.session_state[f"elig_{row['uid']}_{_k}"] = _coerce_elig(_baseline_val(_k), _k)
    st.session_state[f"decline_{row['uid']}"] = "Yes" if row.get("decline_flags_present") else "No"
    st.session_state[f"risks_{row['uid']}"] = _safe_str(row.get("key_risks"))
    _stored_dec = row.get("decision") or row.get("auto_recommendation")
    if _stored_dec:
        st.session_state[f"dec_{row['uid']}"] = _stored_dec
    st.session_state[f"rat_{row['uid']}"] = _safe_str(row.get("decision_note"))
    st.session_state[_seed_sentinel] = True

# VIEW-FIRST: everything is read-only until the reviewer clicks "Edit RFP".
# Save / Cancel appear only in edit mode; saving returns to the plain display.
_edit_key = f"_review_edit_{row['uid']}"
edit_mode = can_edit and bool(st.session_state.get(_edit_key))


def _exit_edit_and_reset():
    """Leave edit mode and drop in-progress widget state so it re-seeds from DB."""
    for _k in CRITERIA:
        st.session_state.pop(f"elig_{row['uid']}_{_k}", None)
    for _p in ("decline_", "risks_", "dec_", "rat_"):
        st.session_state.pop(f"{_p}{row['uid']}", None)
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

# Header only — the Edit RFP toggle lives on the "Rate this RFP" row at the
# bottom of the screen (Save / Cancel appear in the Team-decision section).
st.markdown("**Eligibility criteria**  "
            + ("_— editing; Save or Cancel below_"
               if edit_mode else "_— score · winning factors / total · confidence_"))

# Fatal verdict + factor breakdown — computed ONCE here (single source) so each
# criterion's collapsible card and the gauge all read the same numbers.
_OR_KEYS = {"funder_relationship"}   # geographic_fit is now a single tiered component
_FMARK = {True: ("✓", "#1a7f37"), False: ("✗", "#c0392b"), None: ("?", "#8a6d00")}
try:
    _is_fatal, _trigger = _cderive.fatal_decline(_org_prof, row, _donor_eff, _org_set)
    _bd = _cderive.factor_breakdown(row, _org_prof, _donor_eff, _org_set)
except Exception:
    _is_fatal, _trigger, _bd = False, None, {}


def _factor_html(ckey: str) -> str:
    """Component pass/fail rows for ONE criterion — the body of its collapsible
    card. Shows EVERY sub-factor: ✓ met · ✗ failed · ? uncertain · ? not-applicable
    (greyed, not required by this call → excluded from the denominator). OR-criteria
    show the satisfied path ✓ and the unused alternatives as neutral ○."""
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
        _sym, _col = _FMARK.get(_it.get("met"), ("?", "#8a6d00"))
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
        _sym, _col = _FMARK.get(_it.get("met"), ("?", "#8a6d00"))
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
    is_or = ckey in _OR_KEYS
    any_met = any(f["met"] is True for f in facts if f.get("active", True))
    out = []
    for f in facts:
        if not f.get("active", True):
            sym, col, suffix = "?", "#b8860b", (" <span style='color:#aaa'>"
                "(Not sure — not stated by this call; excluded from the count)</span>")
        elif f.get("_detail") is not None:
            # Graded component (e.g. track record): band symbol + ratio detail, never
            # the bare "?" — a real score, not an undetermined one.
            _gsc = f.get("score") or 0.0
            sym, col = (("✓", "#1a7f37") if _gsc >= 1.0 else
                        ("◐", "#b8860b") if _gsc >= 0.5 else ("✗", "#c0392b"))
            suffix = f" <span style='color:#888'>— {_esc(f['_detail'])}</span>"
        elif is_or and any_met and f["met"] is not True:
            sym, col, suffix = "○", "#999", (" <span style='color:#aaa'>"
                "(alternative route — not needed)</span>")
        else:
            sym, col = _FMARK[f["met"]]
            suffix = (" <span style='color:#aaa'>(no restriction — defaults to pass)</span>"
                      if f.get("default") else "")
        lock = " 🔒" if f.get("fatal") else ""
        out.append(f"<div style='font-size:0.82rem;margin:2px 0'>"
                   f"<span style='color:{col};font-weight:700'>{sym}</span> "
                   f"{_esc(f['name'])}{lock}{suffix}</div>")
    out.append("<div style='color:#aaa;font-size:0.72rem;margin-top:6px'>"
               "✓ met · ✗ failed · ? Not sure (not stated — excluded) · ○ alt-route · "
               "🔒 fatal gate (failing it auto-Declines)</div>")
    return "".join(out)


def _qual_rule(scores: list[float], by_key=None) -> str:
    """MUST-1 roll-up: any 0 → No · any 0.5 → Mostly · all 1 → Yes."""
    if any(s <= 0.0 for s in scores):
        return "No, not eligible"
    if any(s == 0.5 for s in scores):
        return "Mostly, one item unclear"
    return "Yes, fully"


def _strat_rule(scores: list[float], by_key=None) -> str:
    """MUST-2 roll-up: BEST-aligned theme wins — 1 → Strongly · 0.5 → Limited · else Off."""
    best = max(scores, default=0.0)
    return {1.0: "Strongly aligns", 0.5: "Limited priority"}.get(best, "Off-strategy")


def _cap_rule(scores: list[float], by_key=None) -> str:
    """MUST-3 roll-up (gate, like MUST-1): any 0 → No, beyond us · any 0.5 → stretch ·
    all 1 → comfortably."""
    if any(s <= 0.0 for s in scores):
        return "No, beyond us"
    if any(s == 0.5 for s in scores):
        return "Yes, but a stretch"
    return "Yes, comfortably"


def _geo_rule(scores: list[float], by_key=None) -> str:
    """MUST-4 roll-up: single tiered component — 1 → own presence · 0.5 → via a
    partner · else no presence."""
    best = max(scores, default=0.0)
    return {1.0: "Yes, our own presence",
            0.5: "Yes, via a partner"}.get(best, "No presence there")


def _cofin_rule(scores: list[float], by_key=None) -> str:
    """MUST-5 roll-up — Met / Not Met framing (MUST-5 spans co-financing AND the
    compliance gates SAM/tax-exempt/…). ANY unmet active component (hard gate OR
    co-financing) → 'Not met', overriding the rest · any 0.5 → 'Partial, with effort' ·
    all 1 → 'Yes, fully met'."""
    if any(s <= 0.0 for s in scores):
        return "Not met"
    if any(s == 0.5 for s in scores):
        return "Partial, with effort"
    return "Yes, fully met"


# ── PREFER roll-up rules (component scores → the criterion's response label) ────────
# Same contract as the MUST rules so PREFER 6-9 render with the SAME component editor.
# Labels MUST match core.scorer.CRITERION_RESPONSES for that key (so Save stores a
# valid value). Two need keyed access (`by_key`): relationship (grantee outranks
# contact) and bid-effort (a time × team matrix).
def _fq_rule(scores: list[float], by_key=None) -> str:
    """PREFER-6 funding quality: ratio of active size-fit components."""
    if not scores:
        return "Not sure"
    r = sum(scores) / len(scores)
    return "High" if r >= 0.75 else ("Moderate" if r >= 0.4 else "Low")


def _rel_rule(scores: list[float], by_key=None) -> str:
    """PREFER-7 donor relationship (OR-tiers): grantee is strongest, then any contact."""
    bk = by_key or {}
    if bk.get("rel_grantee", 0.0) >= 1.0:
        return "Current/past grantee"
    if bk.get("rel_grantee", 0.0) >= 0.5 or bk.get("rel_contact", 0.0) >= 0.5:
        return "Some contact"
    return "None"


def _comp_rule(scores: list[float], by_key=None) -> str:
    """PREFER-8 competitiveness: track record dominates, else the overall signal ratio."""
    if not scores:
        return "Not sure"
    bk = by_key or {}
    r = sum(scores) / len(scores)
    track = bk.get("comp_track")
    if (track is not None and track >= 1.0) or r >= 0.66:
        return "Strong (limited field / incumbent / clear edge)"
    if (track is not None and track >= 0.5) or r >= 0.34:
        return "Moderate"
    return "Weak (wide-open)"


def _bid_rule(scores: list[float], by_key=None) -> str:
    """PREFER-9 bid effort: a time × business-development-team matrix. The reviewer can
    set the time component to 1 (ample) / 0.5 (tight) / 0 (not enough)."""
    bk = by_key or {}
    t = bk.get("bid_time", 1.0)               # inactive (no deadline) → assume ample
    has_team = bk.get("bid_team", 0.0) >= 1.0
    if t >= 1.0:
        return "Ample time, sufficient resources" if has_team else "Ample time, but no dedicated team"
    if t >= 0.5:
        return "Tight but doable, with a team" if has_team else "Tight, and no dedicated team"
    return "Not enough time, even with a team" if has_team else "Not enough time, no team"


def _snap(v) -> float:
    """Coerce any input to the nearest allowed component score: 0 / 0.5 / 1."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = 0.0
    return max(0.0, min(1.0, round(v * 2) / 2))


def _factor_score(it: dict) -> float:
    """A component's editable 0/0.5/1 score. MUST score-factors carry `score`; the
    PREFER met-based factors don't, so map met → score (True→1 · False→0 · None→0.5)
    so both kinds render in the same numeric component editor."""
    sc = it.get("score")
    if sc is not None:
        return _snap(sc)
    met = it.get("met")
    return 1.0 if met is True else (0.0 if met is False else 0.5)


def _crit_label_color(lbl: str) -> str:
    """Dynamic colour for a classification label: green (2) / amber (1) / red (0) / grey."""
    return {2: "#1a7f37", 1: "#b8860b", 0: "#c0392b"}.get(criterion_score(lbl), "#777")


def _mk_snap(qk: str):
    """Callback factory — snap a component box to 0 / 0.5 / 1 on change."""
    def _cb():
        st.session_state[qk] = _snap(st.session_state.get(qk, 0.0))
    return _cb


def _item_score_editor(uid: str, key: str, items: list[dict], opts: list[str],
                       current: str, rule) -> str:
    """EDIT-mode composite criterion. The classification is CALCULATED from the ACTIVE
    component scores (0/0.5/1) and shown INLINE next to the criterion title — bold and
    colour-coded — with NO dropdown; the user edits the component numbers and the label
    follows (hard gate: any non-dynamic 0 → fail; soft polarities are baked into the
    component scores). When no component is active (nothing imposed by this call), fall
    back to a manual dropdown."""
    active = [it for it in items if it.get("active")]
    if not active:
        lk = f"elig_{uid}_{key}"
        idx = opts.index(current) if current in opts else 0
        return st.selectbox(LABELS[key], opts, index=idx, key=lk)

    # Classification = rule over the CURRENT active component values (session, else the
    # derived default). Computed BEFORE the inputs so the inline label reflects edits.
    scores = []
    by_key = {}
    for it in active:
        qk = f"qnum_{uid}_{key}_{it['key']}"
        sc = _snap(st.session_state.get(qk, _factor_score(it)))
        scores.append(sc)
        by_key[str(it.get("key"))] = sc
    lbl = rule(scores, by_key)
    st.markdown(
        f"<div style='font-size:0.95rem;margin:0.15rem 0 0.1rem'>"
        f"<span style='font-weight:700'>{_esc(LABELS[key])}</span>&nbsp; → &nbsp;"
        f"<span style='color:{_crit_label_color(lbl)};font-weight:800'>{_esc(lbl)}</span>"
        f"</div>", unsafe_allow_html=True)
    st.caption("Set each component (0 · none / 0.5 · partial / 1 · full) — the "
               "classification recalculates. Greyed rows aren't required by this call.")
    for it in items:                     # ALL components — active editable, inactive greyed
        ik = str(it.get("key"))
        is_act = bool(it.get("active"))
        c1, c2 = st.columns([4, 1])
        c1.markdown(
            f"<div style='padding-top:0.5rem;font-size:0.9rem;"
            f"{'' if is_act else 'color:#aaa'}'>{_esc(it.get('name') or ik)}"
            + (" 🔒" if it.get('hard') else "")
            + ("" if is_act else " · not required") + "</div>", unsafe_allow_html=True)
        if is_act:
            qk = f"qnum_{uid}_{key}_{ik}"
            c2.number_input(
                it.get("name") or ik, min_value=0.0, max_value=1.0, step=0.5,
                value=_factor_score(it),
                key=qk, on_change=_mk_snap(qk), format="%.1f", label_visibility="collapsed")
        else:
            c2.number_input(                       # empty/disabled until activated
                it.get("name") or ik, min_value=0.0, max_value=1.0, step=0.5, value=0.0,
                key=f"qnuminactive_{uid}_{key}_{ik}", disabled=True,
                format="%.1f", label_visibility="collapsed")
    return lbl


grid_col, gauge_col = st.columns([3, 2])

with grid_col:
    if edit_mode and not stored_has_values and is_scraped:
        st.caption("_Automated scan — no criteria scored yet. Pick a response for "
                   "each criterion below, then Save._")
    g1, g2 = st.columns(2)
    edited_values: dict[str, str] = {}
    for i, key in enumerate(CRITERIA):
        target = g1 if i < 5 else g2
        # SINGLE SOURCE OF TRUTH: in VIEW mode the ONE live derivation (_derived,
        # the same one "Why this score" uses) drives the grid label, the badge, the
        # factor panel AND the gauge — so they can never show different answers for
        # the same criterion (all 5 MUST + 4 PREFER). EDIT mode loads the stored
        # value so a reviewer can revise it. Falls back to stored only where the
        # derivation can't determine a value (so we never lose a real answer).
        # Single source of truth: BOTH view and edit baseline from `_baseline_val`
        # (human-reviewed → saved value persists; else live derivation) so the two
        # screens always agree.
        current = _coerce_elig(_baseline_val(key), key)
        with target:
            if edit_mode:
                opts = CRITERION_RESPONSES.get(key, [])
                # ALL 9 criteria are edited via their component sub-factors (0/0.5/1);
                # the label derives from the components and flips the stored verdict on
                # Save. PREFER 6-9 now render the SAME component editor as MUST 1-5.
                _RULES = {"qualification": _qual_rule, "strategic_fit": _strat_rule,
                          "capacity": _cap_rule, "geographic_fit": _geo_rule,
                          "cofinancing": _cofin_rule, "funding_quality": _fq_rule,
                          "funder_relationship": _rel_rule, "competitiveness": _comp_rule,
                          "bid_effort": _bid_rule}
                if key in _RULES:
                    _items = _bd.get(key) or []     # ALL components (active + inactive)
                    edited_values[key] = _item_score_editor(
                        row["uid"], key, _items, opts, current, _RULES[key])
                else:
                    idx = opts.index(current) if current in opts else 0
                    edited_values[key] = st.selectbox(
                        LABELS[key], opts, index=idx, key=f"elig_{row['uid']}_{key}")
            else:
                # VIEW mode = the ONE live derivation (single source of truth): the label
                # must not diverge from the live factor panel + count. A row reviewed
                # before a scoring fix would otherwise freeze a stale label beside a live
                # count (e.g. Funding quality "Moderate" next to 4/4 · 100%). EDIT mode
                # still loads the saved value (above) so a reviewer resumes their work.
                current = _coerce_elig(_derived.get(key) or _baseline_val(key), key)
                edited_values[key] = current   # derived value feeds the live gauge
                _act = [f for f in (_bd.get(key) or []) if f.get("active", True)]
                if key in ("qualification", "capacity", "cofinancing"):
                    # MUST-1 / MUST-3 / MUST-5 ratio = Σ component scores ÷ activated
                    # components (NOT benefit-of-doubt won/total). den 0 → "Not sure".
                    _num = sum((f.get("score") or 0) for f in _act)
                    _total = len(_act)
                    _won_disp = f"{_num:g}"
                    _pct = round(_num / _total * 100) if _total else 0
                elif key in ("strategic_fit", "geographic_fit"):
                    # MUST-2 / MUST-4 = ONE component scored 0/0.5/1.
                    _it0 = _act[0] if _act else None
                    _sc0 = (_it0.get("score") or 0) if _it0 else 0
                    _won_disp = f"{_sc0:g}"
                    _total = 1 if _act else 0
                    _pct = round(_sc0 * 100)
                else:
                    # won/total over MEASURABLE components only: an unmeasurable factor
                    # (met=None with no score — can't tell from call OR donor intel) is
                    # EXCLUDED from BOTH numerator and denominator, never a benefit-of-doubt
                    # "win"; a graded component contributes its FRACTIONAL score, not a full
                    # win. This mirrors the criterion label's own mean, so count and label
                    # agree (e.g. PREFER-6 with only the award-value factor failing → 0/1).
                    _meas = [f for f in _act
                             if f.get("score") is not None or f.get("met") is not None]
                    _num = sum((f["score"] if f.get("score") is not None
                                else (1.0 if f["met"] else 0.0)) for f in _meas)
                    _total = len(_meas)
                    _won_disp = f"{_num:g}"
                    _pct = round(_num / _total * 100) if _total else 0
                # Each criterion is its OWN collapsible card — click to expand and
                # see the component sub-factors (✓/✗/?) behind it. Title is BOLD (no
                # colour); the value LABEL is colour-coded. "Not sure" (no active
                # component → value 1 / Park) reads amber, NOT grey/red.
                _is_not_sure = criterion_score(current) is None
                _vc = ("orange" if _is_not_sure else
                       {2: "green", 1: "orange", 0: "red"}.get(criterion_score(current), "gray"))
                # No measurable component → "Not sure · Park" instead of "0/0 · 0%". Also
                # for funding_quality: when the award can't be sized (label "Not sure")
                # don't show a contradictory "0/1 · 0%" beside it — read "Not sure · Park".
                _ratio = ("Not sure · Park"
                          if (not _total or (key == "funding_quality" and _is_not_sure))
                          else f"{_won_disp}/{_total} · {_pct}%")
                with st.expander(
                        f"{_crit_badge(current)}  **{LABELS[key]}** — "
                        f":{_vc}[{current or 'Not sure'}]  ·  {_ratio}"):
                    st.markdown(_factor_html(key), unsafe_allow_html=True)

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
        + f"<div style='color:{_bcol};font-size:0.78rem;margin-top:4px'>"
          f"Confidence: <b>{_band}</b> · data {_bpct}% "
          f"(donor {_dpct}% · call {_cpct}%)</div>"
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
        sc = criterion_score(edited_values.get(key))
        frac = 0.5 if sc is None else sc / 2.0            # Not sure → Park midpoint
        pts = _WEIGHTS[key] * frac * 100.0
        col = "#00703C" if frac >= 1 else "#8a6d00" if frac >= 0.5 else "#b3261e"
        nm = LABELS[key].split(" · ", 1)[-1]
        return (f"<div style='display:flex;justify-content:space-between;padding:1px 0'>"
                f"<span style='color:#555'>{_esc(nm)} "
                f"<span style='color:#aaa'>·{_WEIGHTS[key]:.2f}</span></span>"
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
        # Breathing room between the decision card and the Edit RFP button.
        st.markdown("<div style='margin-top:18px'></div>", unsafe_allow_html=True)
        # Edit RFP sits in the EXACT position Save changes occupies in edit mode
        # (first column of the same [1,1,3] row) — click Edit → Save/Cancel appear
        # here; Save → back to this Edit button.
        be1, _be2, _be3 = st.columns([1, 1, 3])
        if be1.button("✏ Edit RFP", type="primary", width='stretch',
                      key=f"edit_rfp_{row['uid']}",
                      help="Edit the eligibility criteria and record the team decision"):
            st.session_state[_edit_key] = True
            st.rerun()
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
            "decision_date": date.today().isoformat(),
            "decision_overridden_by": user.get("email"),
            "decision_overridden_at": datetime.now(timezone.utc).isoformat(),
        }
        sb.table("rfp_submissions").update(update).eq("uid", row["uid"]).execute()
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
