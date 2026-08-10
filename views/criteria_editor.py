"""The edit-mode component editor for one criterion — the widget itself.

Split out of `views/review_rfp.py` so it can be driven by `streamlit.testing.AppTest`.
The page runs the auth gate at import time, so the editor was unreachable from a test; a
Streamlit widget bug had already been found here by hand that no amount of reading the
code would have shown, which is exactly the argument for being able to test it.

This module imports Streamlit but performs no auth and touches no database.
"""
from __future__ import annotations

import html as _html

import streamlit as st

from core import criteria_review as _crev
from core.scorer import criterion_score

# "—" is NOT a score: it means the component carries no human verdict. An UNSURE component
# (nothing measured) shows "—" rather than 0.0, because 0.0 reads as a measured failure —
# an unrecorded co-financing capacity and a co-financing capacity of zero are different
# answers and must not share a rendering.
DASH = "—"
COMP_OPTS = [DASH, "0", "0.5", "1"]


def _esc(v) -> str:
    # Escape HTML, and neutralise "$" so Streamlit's markdown doesn't render a value as a
    # LaTeX math block.
    return _html.escape("" if v is None else str(v)).replace("$", "&#36;")


def _label_color(lbl) -> str:
    """green (2) / amber (1) / red (0) / grey for a classification label."""
    return {2: "#1a7f37", 1: "#b8860b", 0: "#c0392b"}.get(criterion_score(lbl), "#777")


def crit_dirty_key(uid: str, key: str) -> str:
    """Session flag: the reviewer edited SOME component of this criterion."""
    return f"qdirty_{uid}_{key}"


def comp_dirty_key(uid: str, key: str, ck: str) -> str:
    """Session flag: the reviewer touched THIS component.

    Tracked per COMPONENT, not per criterion. A per-criterion flag meant one edit
    persisted the derived score of every other active component in that criterion as
    though a human had chosen it, freezing the derivation for components nobody looked at
    so a later scoring fix could never reach them."""
    return f"qcdirty_{uid}_{key}_{ck}"


def comp_widget_key(uid: str, key: str, ck: str) -> str:
    return f"qsel_{uid}_{key}_{ck}"


def _mk_change(qk: str, cdirty: str, compdirty: str):
    """on_change fires ONLY for a genuine user interaction, so it is the reliable signal
    that the reviewer actually chose this value — as opposed to the editor re-rendering
    with derived defaults.

    The flag records THAT they chose, never WHAT they chose. It used to be set to
    `value != DASH`, which threw away the one case that matters most: picking "—" on a
    component the scan had scored cleared the flag, so the edit vanished, the derived
    score came back, and the row kept counting while the box sat on "—"."""
    def _cb():
        st.session_state[compdirty] = True
        st.session_state[cdirty] = True
    return _cb


def session_edits(uid: str, key: str, items: list[dict]) -> dict[str, float | None]:
    """The reviewer's explicit component verdicts for this criterion.

    A component they never touched is ABSENT, so the derivation keeps driving it. A
    component they touched carries their choice:

        0 / 0.5 / 1  a score  — activates the component and counts it
        None ("—")   CLEARED  — "do not score this", so it leaves the denominator

    "—" was previously indistinguishable from "never touched", which is why clearing a
    component the scan had scored appeared to do nothing at all."""
    edits: dict[str, float | None] = {}
    for it in items or []:
        ck = str(it.get("key"))
        if not st.session_state.get(comp_dirty_key(uid, key, ck)):
            continue
        raw = st.session_state.get(comp_widget_key(uid, key, ck))
        edits[ck] = None if raw in (None, DASH) else _crev.snap(raw)
    return edits


def clear_session_edits(uid: str, key: str, items: list[dict]) -> None:
    """Drop in-progress state for one criterion.

    The per-component selections and their touched-flags decide which values get persisted
    as a human verdict, so an abandoned edit that survived would be saved as somebody's
    answer the next time the row was touched."""
    st.session_state.pop(crit_dirty_key(uid, key), None)
    for it in items or []:
        ck = str(it.get("key"))
        st.session_state.pop(comp_widget_key(uid, key, ck), None)
        st.session_state.pop(comp_dirty_key(uid, key, ck), None)


def render_component_editor(uid: str, key: str, title: str, items: list[dict],
                            derived_label, *, collect: dict | None = None) -> str:
    """EDIT-mode composite criterion — the SAME shape for all nine.

    The classification is CALCULATED from the component values and shown INLINE next to
    the criterion title, bold and colour-coded. There is NO dropdown for the criterion
    itself: a criterion label is a function of its components, so offering a free choice
    let the two disagree. (Changing the criterion directly belongs in Edit RFP and on
    Submit, which is where the RFP's own fields are edited.)

    MUST-1 used to fall back to a manual dropdown whenever no component was active, which
    is why it rendered unlike every other criterion. Every component is now shown and
    editable — including ones this call didn't impose — so there is always something to
    edit and the fallback is gone. Setting a value on a greyed component ACTIVATES it.

    `collect[key]` receives exactly the values the HUMAN set, which is what gets persisted
    as their verdict.
    """
    items = items or []
    edits = session_edits(uid, key, items)
    eff = _crev.with_session_edits(items, edits)
    lbl = _crev.criterion_label(key, eff, derived_label, session_scores=edits or None)
    if collect is not None:
        collect[key] = dict(edits)
    st.markdown(
        f"<div style='font-size:0.95rem;margin:0.15rem 0 0.1rem'>"
        f"<span style='font-weight:700'>{_esc(title)}</span>&nbsp; → &nbsp;"
        f"<span style='color:{_label_color(lbl)};font-weight:800'>{_esc(lbl)}</span>"
        f"</div>", unsafe_allow_html=True)
    st.caption("Set any component (0 · none / 0.5 · partial / 1 · full) — the "
               "classification recalculates. **—** means not scored and is greyed out; "
               "give it a value and it activates and starts counting. Greyed rows aren't "
               "required by this call, but setting one asserts that it applies.")
    # PREFER-6 / PREFER-8 are named by their own weighted model, so editing their
    # components moves the count but NOT the label. Say that here, or a reviewer sets a
    # value, watches the label sit still, and reasonably concludes the editor is broken.
    if key in _crev.DERIVATION_AUTHORITATIVE:
        st.caption(":blue[This criterion is scored by its own weighted model, so your "
                   "component edits are recorded and shown but do **not** rename it.]")
    # Iterate the EFFECTIVE items (derivation + this session's edits), not the raw ones.
    # Reading `active` off the raw item meant a component the reviewer had just scored kept
    # rendering greyed and captioned "not required" while its chosen value sat in the box
    # beside it: the edit counted towards the label but looked inert.
    for it, ef in zip(items, eff):
        ck = str(it.get("key"))
        # The SCOPE decision still comes from the derivation (SAM/UEI stays unreachable),
        # but everything visual is decided by the value now showing in the box.
        editable = _crev.is_editable(it)
        # THE WIDGET'S OWN VALUE IS AUTHORITATIVE once it exists. Streamlit ignores `index`
        # for a keyed widget that already has session state, so recomputing the default here
        # and styling from THAT let the two disagree: the box showed "—" while the row was
        # styled from the derivation's 0.5, stayed dark, and kept counting.
        qk = comp_widget_key(uid, key, ck)
        cur = st.session_state.get(qk)
        if cur not in COMP_OPTS:
            # First render: preselect the MEASURED value; an unmeasured component shows "—"
            # so a reviewer can tell "we don't know" from "we know, and it's zero".
            cur = (f"{_crev.component_score(ef):g}"
                   if (_crev.is_scored(ef) and ef.get("active")) else DASH)
        # THE RULE (owner 2026-08-10): a component is greyed exactly when it has NO VALUE.
        # "—" = not scored, excluded from the count → greyed. Any of 0 / 0.5 / 1 = live and
        # counting → normal weight. One rule, whether the value came from the scan/cron or
        # from a human, so the same state is never rendered two different ways.
        has_value = cur != DASH
        c1, c2 = st.columns([4, 1])
        c1.markdown(
            f"<div style='padding-top:0.5rem;font-size:0.9rem;"
            f"{'' if has_value else 'color:#aaa'}'>{_esc(it.get('name') or ck)}"
            + (" 🔒" if it.get("hard") else "")
            # "not required" describes what the CALL imposed, so it must disappear the moment
            # the row carries a value — a scored row IS required, by whoever scored it.
            + ("" if (has_value or it.get("active")) else " · not required")
            + (" <span style='color:#1a7f37;font-size:0.72rem'>· cleared by you</span>"
               if ef.get("_cleared") else
               " <span style='color:#1a7f37;font-size:0.72rem'>· set by you</span>"
               if ef.get("_override") else "")
            + ("" if editable else " · not applicable to this funder")
            + "</div>", unsafe_allow_html=True)
        c2.selectbox(
            it.get("name") or ck, COMP_OPTS,
            index=COMP_OPTS.index(cur) if cur in COMP_OPTS else 0,
            key=qk, disabled=not editable,
            on_change=_mk_change(qk, crit_dirty_key(uid, key),
                                 comp_dirty_key(uid, key, ck)),
            label_visibility="collapsed")
    return lbl
