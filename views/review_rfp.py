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
    CRITERIA, CRITERION_RESPONSES, default_response, score_submission,
)
from db.supabase_client import get_client

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
    res = (
        get_client()
        .table("rfp_submissions")
        .select("*")
        .eq("review_week", week)
        .eq("is_duplicate", False)
        .order("alignment_score", desc=True)
        .execute()
    )
    return pd.DataFrame(res.data or [])


wc, rc = st.columns([1, 3])
with wc:
    sel_week = st.selectbox(
        f"Review week", all_weeks, index=all_weeks.index(default_week),
        key="review_rfp_week",
    )

df = _fetch(sel_week)
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


# -----------------------------------------------------------------------------
# Header + key details
# -----------------------------------------------------------------------------
DECISION_COLOR = {
    "Proceed": ("#dcf5e3", "Proceed"),
    "Proceed as sub": ("#dcf5e3", "Proceed as sub"),
    "Park": ("#fff4cc", "Park"),
    "Decline": ("#fde2e2", "Decline"),
}
bg, label = DECISION_COLOR.get(row.get("decision") or "", ("#eee", "Unassigned"))

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
        return ", ".join(map(str, v)) or "—"
    return str(v)


def _safe_str(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v)


det1, det2, det3 = st.columns(3)
with det1:
    st.markdown("**Key details**")
    st.write(f"Applicant role: {_fmt(row.get('applicant_role'))}")
    st.write(f"Window: {_fmt(row.get('funding_window'))}")
    st.write(f"Deadline: {_fmt(row.get('submission_deadline'))}")
    st.write(f"Award date: {_fmt(row.get('expected_award_date'))}")
    st.write(f"Duration: {_fmt(row.get('project_duration'))} mo")
with det2:
    st.markdown("**Value**")
    val = row.get("estimated_value")
    st.write(f"Estimated: {_fmt(val)} {_fmt(row.get('currency'))}")
    usd = (val or 0) * dropdowns.usd_rate(row.get("currency"))
    st.write(f"≈ ${usd:,.0f} USD")
    st.markdown("**Program area**")
    st.write(_fmt(row.get("program_area")))
    st.write(f"Geography: {_fmt(row.get('geographic_scope'))}")
with det3:
    st.markdown("**Brief description**")
    st.write(_fmt(row.get("brief_description")))
    if row.get("opportunity_link"):
        st.markdown(f"[Opportunity link]({row['opportunity_link']})")

st.divider()


# -----------------------------------------------------------------------------
# Inline-editable eligibility grid + live gauge
# -----------------------------------------------------------------------------
LABELS = {
    "qualification": "MUST 1 · Organisational qualification",
    "strategic_fit":  "MUST 2 · Strategic fit",
    "capacity":  "MUST 3 · Delivery capacity",
    "geographic_fit":      "MUST 4 · Geographic fit",
    "cofinancing":     "MUST 5 · Co-financing requirements",
    "funding_quality": "PREFER 6 · Funding quality",
    "funder_relationship":     "PREFER 7 · Funder relationship",
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

# Seed widget session_state from DB on first render of this UID. Without this,
# Streamlit widgets keep stale session_state across reruns and ignore the
# `index` parameter — leaving dropdowns showing "—" even when the row has
# values. The sentinel ensures we only seed once per UID per session, so the
# user's in-progress edits aren't clobbered on every rerun.
# Sentinel includes a schema version so a code change invalidates stale
# session_state from older labels (Yes/No → True/False).
_seed_sentinel = f"_review_seeded_v3_{row['uid']}"
if _seed_sentinel not in st.session_state:
    for _k in CRITERIA:
        st.session_state[f"elig_{row['uid']}_{_k}"] = _coerce_elig(row.get(_k), _k)
    st.session_state[f"decline_{row['uid']}"] = "Yes" if row.get("decline_flags_present") else "No"
    st.session_state[f"risks_{row['uid']}"] = _safe_str(row.get("key_risks"))
    _stored_dec = row.get("decision") or row.get("auto_recommendation")
    if _stored_dec:
        st.session_state[f"dec_{row['uid']}"] = _stored_dec
    st.session_state[f"rat_{row['uid']}"] = _safe_str(row.get("decision_note"))
    st.session_state[_seed_sentinel] = True

# "Reset to stored values" — clears the sentinel + widget state so values re-seed
if can_edit:
    rcol, _spacer_r = st.columns([2, 6])
    if rcol.button("↺ Reset to stored values", help="Discard unsaved edits and re-show DB values"):
        for k in CRITERIA:
            st.session_state.pop(f"elig_{row['uid']}_{k}", None)
        st.session_state.pop(f"decline_{row['uid']}", None)
        st.session_state.pop(f"risks_{row['uid']}", None)
        st.session_state.pop(f"dec_{row['uid']}", None)
        st.session_state.pop(f"rat_{row['uid']}", None)
        st.session_state.pop(_seed_sentinel, None)
        st.rerun()

grid_col, gauge_col = st.columns([3, 2])

with grid_col:
    st.markdown("**Eligibility criteria — click to change**")
    if not stored_has_values:
        if is_scraped:
            st.caption(
                "_This RFP came from the automated scanner. No criteria are scored yet — "
                "humans review and pick Yes / Partial / No below._"
            )
        else:
            st.caption(
                "_No criterion values are stored for this RFP. "
                "Pick a response for each criterion below and click Save changes._"
            )
    g1, g2 = st.columns(2)
    edited_values: dict[str, str] = {}
    for i, key in enumerate(CRITERIA):
        target = g1 if i < 5 else g2
        with target:
            opts = CRITERION_RESPONSES.get(key, [])
            current = _coerce_elig(row.get(key), key)
            idx = opts.index(current) if current in opts else 0
            picked = st.selectbox(
                LABELS[key],
                opts,
                index=idx,
                key=f"elig_{row['uid']}_{key}",
                disabled=not can_edit,
            )
            edited_values[key] = picked  # one of the per-criterion response labels

    df_col1, df_col2 = st.columns([1, 3])
    decline_in = df_col1.radio(
        "Decline flags?", ["No", "Yes"], horizontal=True,
        index=1 if row.get("decline_flags_present") else 0,
        key=f"decline_{row['uid']}", disabled=not can_edit,
    )
    risks_in = df_col2.text_input(
        "Key risks (one line)",
        value=_safe_str(row.get("key_risks")),
        key=f"risks_{row['uid']}", disabled=not can_edit,
    )

# Compute LIVE score from edited values
live_score, live_rec = score_submission(edited_values, decline_in == "Yes")

# Composite org × donor × RFP match: 80% eligibility criteria + 20% donor-org
# relationship (thematic / geographic / route), with the hard MUST gate. The
# gauge shows this composite; criteria-only is the delta reference.
from core import matching as _matching
from core import org_profile as _orgp
from core import settings as _settings
_org_prof = _orgp.get_profile()
_org_set = _settings.get_org()
_donor = None
try:
    _fa = (row.get("funding_agency") or "").strip()
    if _fa:
        _dq = (sb.table("donor_intel").select("*")
               .ilike("donor", _fa).limit(1).execute().data or [])
        _donor = _dq[0] if _dq else None
except Exception:
    _donor = None
_match = _matching.composite_match({**row, **edited_values}, _org_prof, _donor, _org_set)


def _tick(v: float) -> str:
    return "✓" if v >= 1 else ("~" if v >= 0.5 else "✗")


with gauge_col:
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number+delta",
            value=_match["composite"],
            title={"text": "Composite match", "font": {"size": 14}},
            domain={"x": [0, 1], "y": [0.25, 1]},
            delta={
                "reference": live_score,          # vs criteria-only
                "valueformat": ".1f",
                "position": "bottom",
            },
            number={"suffix": " / 100", "font": {"size": 32}},
            gauge={
                "axis": {"range": [0, 100], "tickwidth": 1},
                "bar": {"color": "#00703C", "thickness": 0.25},
                "bgcolor": "white",
                "borderwidth": 0,
                "steps": [
                    {"range": [0, 45],  "color": "#fde2e2"},
                    {"range": [45, 70], "color": "#fff4cc"},
                    {"range": [70, 100], "color": "#dcf5e3"},
                ],
            },
        )
    )
    fig.update_layout(height=280, margin=dict(t=20, b=20, l=20, r=20))
    st.plotly_chart(fig, width='stretch')
    _ex = _match["extras"]
    st.markdown(
        f"<div style='text-align:center;color:#333;font-size:0.9rem'>"
        f"<b>Match: {_match['decision']}</b>"
        f"{' · ⚠ a MUST is “No”' if _match['must_no'] else ''}</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"composite {_match['composite']:.0f} = 80% criteria "
        f"({_match['criteria_score']:.0f}) + 20% donor-org ({_match['extras_score']:.0f})  ·  "
        f"donor-org: thematic {_tick(_ex['donor_thematic_fit'])} · "
        f"geo {_tick(_ex['donor_geographic_fit'])} · route {_tick(_ex['donor_route_fit'])} · "
        f"relationship {_tick(_ex.get('donor_relationship_fit', 0.5))}"
        + ("" if _donor else "  ·  _(no donor profile matched — donor-org neutral)_")
    )
    st.caption(
        f"criteria-only auto-rec: **{live_rec}**  ·  stored: "
        f"{row.get('auto_recommendation') or '—'} ({(row.get('alignment_score') or 0):.1f})"
    )

st.divider()


# -----------------------------------------------------------------------------
# Decision + save
# -----------------------------------------------------------------------------
st.subheader("Team decision")
decisions = dropdowns.get("decisions")
choices = list(decisions)
current_dec = row.get("decision") or live_rec
if current_dec and current_dec not in choices:
    choices = [current_dec] + choices

dc1, dc2 = st.columns([1, 3])
new_decision = dc1.selectbox(
    "Decision",
    choices,
    index=choices.index(current_dec) if current_dec in choices else 0,
    key=f"dec_{row['uid']}",
    disabled=not can_edit,
)
new_rationale = dc2.text_area(
    "Decision rationale (2-3 lines)",
    value=_safe_str(row.get("decision_note")),
    key=f"rat_{row['uid']}",
    height=90,
    disabled=not can_edit,
)

bsave, _bspace = st.columns([1, 4])
if bsave.button("💾 Save changes", type="primary", disabled=not can_edit, width='stretch'):
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
    st.cache_data.clear()
    st.success(
        f"Saved {row['uid']} · new score {live_score:.1f} → **{live_rec}** · "
        f"decision **{new_decision}**."
    )
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
