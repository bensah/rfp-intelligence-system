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


# A brief that opens with an attachment tag ("[General_conditions.pdf] …") is a raw
# document dump, not a synthesised summary. Also catches the .docx/.xlsx/.zip variants.
_ATTACH_TAG_RE = re.compile(r"^\s*\[[^\]]+\.(?:pdf|docx?|xlsx?|pptx?|zip)\]\s*", re.IGNORECASE)
# Decimal sub-clause numbering ("1.1", "2.3", "10.2") — the shape of copied contract
# boilerplate. Requires the N.N decimal form so a real brief's "Stage 1. … Stage 2. …" or
# "Phase 1." (a bare integer + period) is NOT mistaken for legalese. Two+ ⇒ raw.
_CLAUSE_NUM_RE = re.compile(r"(?:^|\s)\d{1,2}\.\d{1,2}\b")
# Contract-boilerplate phrasing that a real call summary never leads with.
_LEGALESE_RE = re.compile(
    r"general conditions of contract|legal status of the part|"
    r"the contractor shall|shall be construed|terms and conditions of contract",
    re.IGNORECASE)


def looks_raw_brief(brief: Any, raw_text: Any = None) -> bool:
    """True when `brief` is RAW scraped/attachment text rather than a clean synthesised
    summary. Single source of truth reused by the render guard, the write choke point, and
    the backfills so 'what counts as raw' is defined once.

    Heuristics: empty; an opening "[file.pdf]" attachment tag; a verbatim prefix of the
    row's raw_text (copied, not synthesised); contract-boilerplate legalese; dense clause
    numbering (≥2 "1.1"-style clauses); or ALL-CAPS-heavy headings (>30% of words)."""
    b = strip_html(brief) if isinstance(brief, str) else brief
    b = (b or "").strip() if isinstance(b, str) else ""
    if not b:
        return True
    if _ATTACH_TAG_RE.search(b):
        return True
    if _LEGALESE_RE.search(b[:400]):
        return True
    if len(_CLAUSE_NUM_RE.findall(b)) >= 2:
        return True
    rt = (raw_text or "").strip() if isinstance(raw_text, str) else ""
    # A brief that is a verbatim opening of raw_text was COPIED, not synthesised. Require a
    # substantial (≥60-char) match so a short shared opener doesn't misfire on real prose.
    if rt and len(b) >= 60 and rt.lower().startswith(b[:200].lower()):
        return True
    # ALL-CAPS legalese headings ("GENERAL CONDITIONS OF CONTRACT PROVISION OF GOODS…"):
    # require BOTH a high ratio AND many caps words, so a normal brief peppered with a few
    # acronyms (UNOPS, RFQ, PPE, DRC) is NOT flagged — only a genuine shouting heading run.
    words = re.findall(r"[A-Za-z]{3,}", b)
    caps = sum(1 for w in words if w.isupper())
    if words and caps >= 6 and caps / len(words) > 0.6:
        return True
    return False


def clean_brief(brief: Any, raw_text: Any = None) -> str:
    """Return a DISPLAY-safe brief: HTML stripped and a leading "[file.pdf]" attachment tag
    removed. If what remains still reads as RAW legalese/boilerplate (looks_raw_brief), return
    "" so the caller can show a graceful fallback instead of contract clauses. A genuine
    synthesised brief passes through unchanged (minus any stray attachment tag)."""
    s = strip_html(brief) if isinstance(brief, str) else brief
    s = (s or "").strip() if isinstance(s, str) else ""
    if not s:
        return ""
    stripped = _ATTACH_TAG_RE.sub("", s).strip()
    # Re-test the marker-stripped text: if the remainder is still raw legalese, reject it.
    if looks_raw_brief(stripped, raw_text):
        return ""
    return stripped


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


# ---------------------------------------------------------------------------
# Submission weighting
# ---------------------------------------------------------------------------
# An RFP can be submitted to a donor MORE THAN ONCE (rfp_submissions.submissions).
# Counting rows therefore under-reports every submission-derived indicator: an RFP submitted
# twice and now under review is TWO applications under review, not one. Every count over
# submitted RFPs — Total Submitted, Approved, Under Review, Not Approved, win rate — must use
# this weight so the whole app tells the same story.
#
#   weight = submissions (>=1) when the row has actually been SUBMITTED, else 0.
#
# "Submitted" is the app-wide rule (progress_status = Completed, OR a real donor decision —
# a donor can only decide on a proposal it received, OR a recorded submission date), so a
# backdated import whose progress_status was never set still counts. A row that was never
# submitted contributes 0, never a phantom application.
_SUBMITTED_DECISIONS_W = {"approved", "under review", "not approved"}


def submission_weight(row) -> int:
    """Donor-side submissions this row represents: N if submitted, else 0."""
    get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
    ps = str(get("progress_status") or "").strip().lower()
    dd = str(get("donor_decision") or "").strip().lower()
    submitted = (ps == "completed" or dd in _SUBMITTED_DECISIONS_W
                 or bool(str(get("date_completed") or "").strip()))
    if not submitted:
        return 0
    n = get("submissions")
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


def submission_weights(df):
    """Vectorised `submission_weight` → an int Series aligned to `df` (0 for an empty df)."""
    import pandas as _pd
    if df is None or len(df) == 0:
        return _pd.Series(dtype="int64")
    return df.apply(submission_weight, axis=1).astype("int64")


def requested_currency(row):
    """The currency the REQUEST was made in.

    `amount_requested` is what WE asked the donor for; `currency` is the unit the CALL was
    advertised in (the Estimated Value). They are NOT the same thing — a Canadian call can
    be advertised in CAD while the budget submitted is in USD. Converting the request with
    the call's currency silently mis-states it (CAD-rating a USD 715,400 request produced
    "$509,530 USD" on the Grants page).

    The editor captures the submission/award currency once, as `currency_secured` (labelled
    simply "Currency" — it governs BOTH the requested and the secured amount). Falls back to
    `currency` for legacy rows saved before that field was populated."""
    get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
    cur = str(get("currency_secured") or "").strip()
    return cur or (get("currency") or "")
