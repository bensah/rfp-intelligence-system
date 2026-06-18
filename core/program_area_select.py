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

import streamlit as st

from core.program_area_classifier import (
    CATEGORIES, PROGRAM_AREA_KEYWORDS, TAXONOMY,
    category_full, key_for, subarea_label,
)


def program_area_picker(label, current, key_prefix, *, container=None, help=""):
    """Render the Category → sub-area picker; return the selected list (canonical
    sub-area keys, plus a Category name where the whole category was chosen)."""
    c = container or st
    current = [str(x) for x in (current or [])]
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
