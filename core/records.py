"""Helpers for turning Supabase rows (loaded via pandas) into clean dicts.

When a list of Supabase rows is loaded into a pandas DataFrame, a blank cell —
or a key that's missing from some rows — becomes NaN (a float). NaN is truthy,
so the common `(row.get("x") or "").strip()` idiom slips past the guard and
.strip() raises `AttributeError: 'float' object has no attribute 'strip'`.

Extract a single record through `clean_record()` (e.g. right after
`df.iloc[0].to_dict()`) to coerce every NaN back to None, so downstream
`or ""` / `.strip()` behave as intended. This only touches the extracted record
— the source DataFrame's dtypes are left alone, so sorting / numeric ops on the
frame are unaffected.
"""
from __future__ import annotations

import html as _html
import math
import re
from typing import Any

# Block-level tags become a space (so "…ET</p><p>The post…" reads as two sentences,
# not one run-on); every other tag is dropped.
_HTML_BLOCK_RE = re.compile(r"(?i)<\s*/?\s*(?:br|p|div|li|tr|ul|ol|h[1-6]|blockquote)\b[^>]*>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: Any) -> Any:
    """Convert scraped HTML (WordPress RSS `content:encoded`, etc.) to clean plain text
    for display: block tags → spaces, all other tags dropped, HTML entities decoded,
    whitespace collapsed. Non-string / tag-free input is returned unchanged. Used so a
    brief_description carrying raw `<p>…</p><a href=…>` never shows literal markup."""
    if not isinstance(text, str) or "<" not in text:
        return text
    s = _HTML_BLOCK_RE.sub(" ", text)
    s = _HTML_TAG_RE.sub("", s)
    s = _html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def md_safe(text: Any, dash: str = "—") -> str:
    """Make free / LLM text safe to render via st.markdown / st.write.

    Streamlit's markdown treats `$ … $` as a LaTeX math block, so a dollar amount
    like "$2.3 million … $5 per day" renders as garbled italics. Replacing "$"
    with the HTML entity `&#36;` shows a literal "$" without triggering LaTeX (the
    entity works in both plain-markdown and unsafe_allow_html contexts). Use this
    anywhere LLM/user text is displayed. None/blank → `dash`.
    """
    if text is None:
        return dash
    s = str(text).strip()
    if not s or s.lower() == "nan":
        return dash
    return s.replace("$", "&#36;")


def clean_record(row: Any) -> dict:
    """Return a plain dict copy of `row` with every NaN value coerced to None."""
    d = dict(row) if row is not None else {}
    return {
        k: (None if (isinstance(v, float) and math.isnan(v)) else v)
        for k, v in d.items()
    }


def clean_df(df):
    """Coerce NaN → None in a DataFrame's OBJECT (text-ish) columns, in place.

    `pd.DataFrame([...dicts...])` is the ONLY place NaN is born in this app —
    Supabase returns real None for blanks/missing keys, but pandas backfills
    missing/empty cells with NaN (a float). Downstream, `(row.get("x") or "")
    .strip()` then crashes because NaN is truthy and floats have no .strip().

    Cleaning at DataFrame-CREATION time fixes the whole class at once: every
    later `.to_dict()`, `.iterrows()`, and Series access yields None, so the
    common string idioms just work. Only OBJECT columns are touched — numeric /
    datetime columns keep their native NaN/NaT, so sorting and math are
    unaffected (a numeric NaN never reaches `.strip()` anyway).

    Returns the same DataFrame (mutated) for convenient inline wrapping:
        df = clean_df(pd.DataFrame(res.data or []))
    """
    try:
        import pandas.api.types as pdt
        if df is None or df.empty:
            return df
        # Text-ish columns (object AND the pandas-3 `str` dtype) that actually
        # carry a missing value. Skip numeric / datetime / timedelta / bool: their
        # NaN/NaT never reaches a string op, and converting them would break
        # sorting. Map every missing marker (np.nan float, pd.NA) to plain None.
        cols = [c for c in df.columns
                if not (pdt.is_numeric_dtype(df[c]) or pdt.is_datetime64_any_dtype(df[c])
                        or pdt.is_timedelta64_dtype(df[c]) or pdt.is_bool_dtype(df[c]))
                and df[c].isna().any()]
        if cols:
            # Assign all at once (not column-by-column) to avoid fragmenting a
            # wide frame, then .copy() to hand back a CONSOLIDATED block manager —
            # otherwise downstream `df["new"] = …` adds raise a fragmentation
            # PerformanceWarning on tables with many columns (e.g. donor_intel).
            sub = df[cols].astype(object)
            df[cols] = sub.where(sub.notna(), None)
            df = df.copy()
    except Exception:  # never let sanitisation break a page
        pass
    return df


def drop_concluded(df):
    """Drop CONCLUDED solicitations from the ACTIVE pipeline views (Screen / Review /
    Tracking): a row whose donor_decision is anything other than blank / 'Not submitted',
    OR whose progress_status is Completed / Discontinued. These are won/closed and are
    tracked under Grants (and still counted in the Home Summary), so they must not clutter
    the active screening/review/tracking lists. Mirrors the inline Tracking filter so all
    three views agree. No-op on an empty / column-less frame; never raises."""
    try:
        if df is None or getattr(df, "empty", True):
            return df
        out = df
        if "donor_decision" in out.columns:
            dd = out["donor_decision"].fillna("").astype(str).str.strip().str.lower()
            out = out[dd.isin({"", "not submitted"})]
        if "progress_status" in out.columns:
            ps = out["progress_status"].fillna("").astype(str).str.strip().str.lower()
            out = out[~ps.isin({"completed", "discontinued"})]
        return out.copy()
    except Exception:
        return df
