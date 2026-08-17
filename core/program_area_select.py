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


def program_area_multiselect(label, current, key, *, container=None, help="",
                             disabled=False):
    """One flat multi-select over the whole taxonomy; returns canonical keys.

    A FORM-SAFE sibling of `program_area_picker`. That one reveals a sub-area selector for
    each chosen category, which needs a rerun between the two choices — inside an
    `st.form` nothing reruns until submit, so the second selector never appears. This is
    the version for a dialog or form: every sub-area is offered at once, grouped in the
    label so the category is still visible.

    Used wherever a PERSON records their own program areas. Those fields were free text
    ("e.g. 'Vaccines, MCH, Malaria'"), which put user-entered vocabulary next to a graded
    taxonomy the rest of the app matches on — so a colleague could type "TD" or "malaria
    control" and nothing downstream could line it up with a call's themes. Now the same
    vocabulary everywhere, which is what makes a declaration usable as evidence in MUST-2.
    """
    c = container or st
    cur = _as_selection(current)
    # Legacy free text is preserved as far as it can be resolved, and silently dropped
    # where it cannot — a stray "TD" has no canonical meaning and should not be offered
    # back as though it were a real area.
    known = [k for k in cur if k in PROGRAM_AREA_KEYWORDS]
    if not known and cur:
        try:
            from core.program_area_classifier import expand as _expand
            known = sorted(_expand(cur))
        except Exception:
            known = []
    options = sorted(PROGRAM_AREA_KEYWORDS,
                     key=lambda k: (category_full(k), subarea_label(k)))
    kwargs = dict(
        help=help or "Pick from the shared programme-area list, so what you record here "
                     "matches how calls and funders are classified.",
        format_func=lambda k: f"{subarea_label(k)} · {category_full(k)}",
        disabled=disabled)
    # `default` ONLY on the first render. Passing it on every run alongside a `key` makes
    # Streamlit reset the widget from the default instead of honouring what the user
    # picked - the selection is discarded AND the rerun that does it consumes the click on
    # the surrounding form's submit button. Inside the Add-user dialog that reads exactly
    # as "Create user does nothing": one click is swallowed, a second works. This file's
    # own history records the same failure from a different cause (accept_new_options on a
    # selectbox in this form), so it is worth being explicit about here.
    if key in st.session_state:
        return c.multiselect(label, options, key=key, **kwargs)
    return c.multiselect(label, options, default=[k for k in known if k in options],
                         key=key, **kwargs)


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
    """Lightweight, clean parent/child grading: ONE Category multiselect (pick the
    parents) + ONE st.data_editor whose rows are those categories' sub-areas, with
    a single editable Priority (0–5) column. Returns (selection_keys, ratings_dict).

    Only TWO widgets total (the per-row-widgets version was too heavy: ~4 widgets
    per area, each interaction re-running the whole script). Rating 0 = not a
    priority / no track record → that sub-area is simply not selected. Grades are
    persisted by KEY in session_state, so changing the category set never resets a
    grade you've already entered."""
    c = container or st
    c.markdown(f"**{label}**")
    if help:
        c.caption(help)

    sel0 = [k for k in _as_selection(current_selection) if k in PROGRAM_AREA_KEYWORDS]
    # Persistent {child key: grade}, seeded once from the saved data.
    sk = f"{key_prefix}_ratings_state"
    if sk not in st.session_state:
        st.session_state[sk] = {k: v for k, v in _as_rating_map(current_ratings).items()}
        for k in sel0:                       # selected-but-ungraded → default medium
            st.session_state[sk].setdefault(k, 3)
    rstate = st.session_state[sk]

    # Category (parent) multiselect — default to the categories already in use.
    cats_key = f"{key_prefix}_cats"
    if cats_key not in st.session_state:
        st.session_state[cats_key] = [cat for cat in CATEGORIES
                                      if cat in {category_full(k) for k in sel0}]
    cats = c.multiselect(
        "Categories (parent areas)", CATEGORIES, key=cats_key,
        help="Pick the parent categories you work in / fund; their sub-areas appear "
             "below to grade. Set a sub-area to 0 to drop it.")

    child_keys = [k for cat in cats for sub in TAXONOMY.get(cat, [])
                  if (k := key_for(cat, sub))]
    if not child_keys:
        c.caption("Pick one or more categories to grade their sub-areas.")
        return [], {}

    c.caption(f"Grade each sub-area — {RATING_LEGEND}")
    # IMPORTANT: build the editor's DataFrame ONLY when the row-set (categories)
    # changes, and keep it in session_state. Rebuilding `data` from a source we
    # also write back to on every rerun made the keyed data_editor "fight" the new
    # data → the cell flickered and reverted (the 5→3 bug). Now the editor owns
    # its edits within a stable row-set; we just read the return to persist.
    sig = "|".join(child_keys)
    df_key, sig_key = f"{key_prefix}_df", f"{key_prefix}_sig"
    if st.session_state.get(sig_key) != sig:
        st.session_state[df_key] = pd.DataFrame({
            "Category": [category_full(k) for k in child_keys],
            "Sub-area": [subarea_label(k) for k in child_keys],
            "Priority (0–5)": [int(rstate.get(k, 0)) for k in child_keys],
        })
        st.session_state[sig_key] = sig
    _edited = c.data_editor(
        st.session_state[df_key], hide_index=True, width="stretch", num_rows="fixed",
        key=f"{key_prefix}_tbl_{sig}",
        column_config={
            "Category": st.column_config.TextColumn("Category", disabled=True),
            "Sub-area": st.column_config.TextColumn("Sub-area", disabled=True, width="medium"),
            "Priority (0–5)": st.column_config.NumberColumn(
                "Priority (0–5)", min_value=0, max_value=5, step=1, help=RATING_LEGEND),
        })
    for k, v in zip(child_keys, _edited["Priority (0–5)"].tolist()):
        try:
            rstate[k] = max(0, min(5, int(v)))
        except (TypeError, ValueError):
            rstate[k] = int(rstate.get(k, 0))

    # Selection = sub-areas graded > 0 (0 = absent / not a priority).
    out_sel = [k for k in child_keys if rstate.get(k, 0) > 0]
    out_rat = {k: int(rstate[k]) for k in out_sel}
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
