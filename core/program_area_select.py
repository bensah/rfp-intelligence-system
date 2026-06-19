"""Reusable hierarchical program-area picker (UI).

ONE widget for every form that captures program areas — org fit profile, donor
profile, RFP submission. Pick a high-level Category; a sub-area selector then
appears for each chosen category (leave it empty to mean the WHOLE category).
Returns a list mixing canonical sub-area keys and Category names;
`program_area_classifier.expand()` turns either form into matchable sub-area
keys, so org ↔ RFP ↔ donor compare on one vocabulary.

Streamlit-only — never imported by the headless scan (which uses the classifier
directly, no Streamlit).
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core.program_area_classifier import (
    CATEGORIES, PROGRAM_AREA_KEYWORDS, TAXONOMY,
    category_full, key_for, subarea_label,
)

# 0–5 priority scale, shared by the donor profile and the org fit profile so the
# two grade on the identical vocabulary and can be correlated for strategic fit.
RATING_LEGEND = ("0 absent · 1 very low · 2 low · 3 medium · 4 high · 5 very high")
RATING_WORD = {0: "None", 1: "Very low", 2: "Low", 3: "Medium", 4: "High", 5: "Very high"}


def _as_selection(current) -> list[str]:
    """Coerce a stored selection (list, or JSON-string list, or a single value)
    into a list of strings. Donor fields store JSON text — iterating that string
    directly would yield characters, so nothing pre-selects."""
    import json as _json
    if current is None:
        return []
    if isinstance(current, str):
        s = current.strip()
        if not s or s.lower() in ("nan", "none", "nat"):
            return []
        try:
            parsed = _json.loads(s)
            current = parsed if isinstance(parsed, list) else [s]
        except (ValueError, TypeError):
            current = [s]
    try:
        return [str(x) for x in current]
    except TypeError:
        return [str(current)]


def _as_rating_map(current_ratings) -> dict:
    """Coerce a stored ratings value (dict or JSON string) to {key: int 0-5}."""
    import json as _json
    raw = current_ratings
    if isinstance(raw, str):
        try:
            raw = _json.loads(raw or "{}")
        except (ValueError, TypeError):
            raw = {}
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[str(k)] = max(0, min(5, int(v)))
            except (TypeError, ValueError):
                continue
    return out


def program_area_picker(label, current, key_prefix, *, container=None, help=""):
    """Render the Category → sub-area picker; return the selected list (canonical
    sub-area keys, plus a Category name where the whole category was chosen)."""
    c = container or st
    current = _as_selection(current)
    cur_keys = [x for x in current if x in PROGRAM_AREA_KEYWORDS]
    cur_cats = [x for x in current if x in CATEGORIES]
    implied = {category_full(k) for k in cur_keys}
    pre_cats = [cat for cat in CATEGORIES if cat in (set(cur_cats) | implied)]

    cats = c.multiselect(label, CATEGORIES, default=pre_cats,
                         key=f"{key_prefix}_cats", help=help)
    out: list[str] = []
    for cat in cats:
        pre_subs = [subarea_label(k) for k in cur_keys if category_full(k) == cat]
        chosen = c.multiselect(
            f"↳ {cat} — specific areas (leave empty = whole category)",
            TAXONOMY[cat], default=pre_subs, key=f"{key_prefix}__{cat}")
        keys = [key_for(cat, s) for s in chosen]
        keys = [k for k in keys if k]
        out.extend(keys or [cat])          # specific keys, else the broad category
    return out


def program_area_rating_editor(label, current_selection, current_ratings, key_prefix,
                               *, container=None, help=""):
    """Hierarchical picker PLUS a 0–5 priority grade for each chosen child
    sub-area. Returns (selection_list, ratings_dict {child_key: int 0-5}).

    Only child sub-areas are graded (a whole-category pick carries no rating) —
    so the donor profile and the org profile grade on the identical key space and
    `strategic_fit_score()` can correlate them. Ratings for de-selected areas are
    dropped automatically."""
    c = container or st
    sel = program_area_picker(label, current_selection, key_prefix, container=c, help=help)
    child_keys = [s for s in sel if s in PROGRAM_AREA_KEYWORDS]
    prev = _as_rating_map(current_ratings)
    ratings: dict[str, int] = {}
    if child_keys:
        c.caption(f"Grade each sub-area 0–5 — {RATING_LEGEND}")
        # Persist grades BY KEY across reruns. st.data_editor stores edits by ROW
        # INDEX, which shift when the picker selection changes — so entries would
        # reset. We keep our own {key: rating} in session_state, rebuild the table
        # from it each run, and give the editor a key that tracks the row set so
        # its index-deltas reset cleanly (values survive via the rebuilt base).
        _sk = f"{key_prefix}_ratings_state"
        rstate = st.session_state.setdefault(_sk, {})
        for k, v in prev.items():            # seed once from the saved value
            rstate.setdefault(k, v)
        _base = pd.DataFrame({
            "Strategic priority area": [f"{subarea_label(k)}  ·  {category_full(k)}"
                                        for k in child_keys],
            "Priority (0–5)": [int(rstate.get(k, 3)) for k in child_keys],
        })
        _edited = c.data_editor(
            _base, hide_index=True, width="stretch", num_rows="fixed",
            key=f"{key_prefix}_rate_tbl_" + "|".join(child_keys),
            column_config={
                "Strategic priority area": st.column_config.TextColumn(
                    "Strategic priority area", disabled=True, width="large"),
                "Priority (0–5)": st.column_config.NumberColumn(
                    "Priority (0–5)", min_value=0, max_value=5, step=1,
                    width="small", help=RATING_LEGEND),
            })
        for k, v in zip(child_keys, _edited["Priority (0–5)"].tolist()):
            try:
                rstate[k] = max(0, min(5, int(v)))
            except (TypeError, ValueError):
                rstate[k] = int(rstate.get(k, 3))
        ratings = {k: int(rstate[k]) for k in child_keys}
    return sel, ratings


def program_area_matrix_editor(label, current_selection, current_ratings, key_prefix,
                               *, container=None, help=""):
    """A clean 3-column cascading grid — Category | Sub-area (filtered to the
    chosen category) | Priority (0–5) — one row per area, with add / remove.
    Returns (selection_keys, ratings_dict {child_key: int 0-5}).

    Built from per-row widgets (not st.data_editor) because a data-editor column's
    options can't depend on another cell in the same row — so the child list could
    not be filtered by the row's category. Row state is kept in session_state keyed
    by a stable per-row id so add/remove don't scramble values."""
    c = container or st
    c.markdown(f"**{label}**")
    if help:
        c.caption(help)
    c.caption(RATING_LEGEND)

    ids_key, nid_key = f"{key_prefix}_ids", f"{key_prefix}_nextid"
    if ids_key not in st.session_state:                     # seed once from saved data
        sel = [k for k in _as_selection(current_selection) if k in PROGRAM_AREA_KEYWORDS]
        rmap = _as_rating_map(current_ratings)
        ids = []
        for n, k in enumerate(sel):
            ids.append(n)
            st.session_state[f"{key_prefix}_cat_{n}"] = category_full(k)
            st.session_state[f"{key_prefix}_sub_{n}"] = subarea_label(k)
            st.session_state[f"{key_prefix}_rate_{n}"] = int(rmap.get(k, 3))
        st.session_state[ids_key] = ids
        st.session_state[nid_key] = len(sel)

    cat_opts = [""] + CATEGORIES
    hdr = c.columns([5, 5, 2, 1])
    hdr[0].caption("**Category**")
    hdr[1].caption("**Sub-area**")
    hdr[2].caption("**Priority (0–5)**")

    remove_id = None
    for i in list(st.session_state[ids_key]):
        cat_key, sub_key, rate_key = (f"{key_prefix}_cat_{i}", f"{key_prefix}_sub_{i}",
                                      f"{key_prefix}_rate_{i}")
        cols = c.columns([5, 5, 2, 1])
        cat = cols[0].selectbox("Category", cat_opts, key=cat_key,
                                label_visibility="collapsed")
        subs = [""] + TAXONOMY.get(cat, [])
        if st.session_state.get(sub_key, "") not in subs:   # category changed → drop stale sub
            st.session_state[sub_key] = ""
        cols[1].selectbox("Sub-area", subs, key=sub_key, label_visibility="collapsed")
        st.session_state.setdefault(rate_key, 3)
        cols[2].number_input("Priority", min_value=0, max_value=5, step=1, key=rate_key,
                             label_visibility="collapsed")
        if cols[3].button("✕", key=f"{key_prefix}_rm_{i}", help="Remove this row"):
            remove_id = i

    if c.button("➕ Add area", key=f"{key_prefix}_add"):
        nid = st.session_state[nid_key]
        st.session_state[f"{key_prefix}_rate_{nid}"] = 3
        st.session_state[ids_key].append(nid)
        st.session_state[nid_key] += 1
        st.rerun()
    if remove_id is not None:
        st.session_state[ids_key] = [x for x in st.session_state[ids_key] if x != remove_id]
        st.rerun()

    out_sel: list[str] = []
    out_rat: dict[str, int] = {}
    for i in st.session_state[ids_key]:
        cat = st.session_state.get(f"{key_prefix}_cat_{i}", "")
        sub = st.session_state.get(f"{key_prefix}_sub_{i}", "")
        if not cat or not sub:
            continue
        k = key_for(cat, sub)
        if k and k not in out_rat:
            out_sel.append(k)
            out_rat[k] = max(0, min(5, int(st.session_state.get(f"{key_prefix}_rate_{i}", 3))))
    return out_sel, out_rat


def rating_bars_html(ratings) -> str:
    """Render {child_key: 0-5} as gradient priority bars (highest first). Returns
    an HTML string for st.markdown(..., unsafe_allow_html=True). Empty -> ''."""
    rmap = _as_rating_map(ratings)
    if not rmap:
        return ""
    rows = []
    for k, v in sorted(rmap.items(), key=lambda kv: (-kv[1], kv[0])):
        pct = v / 5 * 100
        rows.append(
            "<div style='display:flex;align-items:center;gap:10px;margin:4px 0;'>"
            "<div style='flex:0 0 240px;font-size:.86rem;line-height:1.2;'>"
            f"{subarea_label(k)} <span style='color:#94a3b8;font-size:.72rem;'>· "
            f"{category_full(k)}</span></div>"
            "<div style='flex:1;background:#eef2f6;border-radius:6px;height:12px;'>"
            f"<div style='width:{pct:.0f}%;height:12px;border-radius:6px;"
            "background:linear-gradient(90deg,#34a06b,#00703C);'></div></div>"
            "<div style='flex:0 0 92px;text-align:right;font-size:.74rem;"
            f"font-weight:600;color:#0f3d6e;'>{v}/5 · {RATING_WORD[v]}</div></div>")
    return "".join(rows)
