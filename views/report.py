"""Report view — KPI dashboard of the full RFPIS pipeline.

Story arc, top → bottom:
  1. Search activity     — scanner output (top of funnel)
  2. Insights funnel     — eligibility / decision breakdown
  3. Reviews & decisions — triage outcomes + velocity
  4. Engagements         — meetings + donor touchpoints
  5. Outcomes            — proposals submitted + grants secured

Two global controls drive every section so the story stays consistent:
  * **Period**   — date window (This year / YTD / Last 90d / Last 12m / All time)
  * **View by**  — bucket granularity (Weekly / Monthly / Quarterly /
                   Semestrial / Annually) applied to every time-series chart

Time-series charts are reindexed against the full bucket range inside the
period, so buckets with zero activity render as flat-line zeros rather
than disappearing — important for the search-activity chart which would
otherwise only show the days a scan was triggered.

Export:
  * **Print** button uses window.print() + @media print CSS that hides the
    Streamlit sidebar / toolbar so the printable / save-as-PDF output is
    just the report body.
  * **Excel export** writes every section's underlying data to a single
    .xlsx workbook (one sheet per section + a Summary sheet on top).
"""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core import dropdowns, report_snapshots, settings
from core.records import clean_df
from db.supabase_client import get_client


# ---------------------------------------------------------------------------
# Team-member name normalization
# ---------------------------------------------------------------------------
# Different submitters spell their names inconsistently across the
# Excel workbook and the Submit form. Common cases:
#   "First"             → should resolve to "First Last"
#   "Nickname"          → should resolve to canonical "First Last"
#   "FIRST LAST"        → should normalise to "First Last"  (case)
#   "first last"        → should normalise to "First Last"
# Without normalisation, the "Submissions by team member" chart shows
# the same person two or three times as separate stacked-bar series.
#
# Strategy: build a normaliser keyed on the canonical team_members
# dropdown. For each input name:
#   1. Trim + collapse whitespace + title-case (handles ALL-CAPS).
#   2. If the normalised name is an exact canonical match → return canonical.
#   3. Tokenise both. After applying the nickname map, if the input
#      tokens are a subset of any canonical name's tokens → return that
#      canonical name. Subset rule handles single-name → full-name.
#   4. Tie-break: longest canonical wins (so "First Last" beats "First").
#   5. Fall back to the title-cased input if nothing matches.
#
# The team-is-small assumption (no two members share first OR last name)
# makes the subset rule safe — when two members DO share a token, the
# subset rule could incorrectly merge them. Re-evaluate this strategy
# if the team grows past ~30 members.
# ---------------------------------------------------------------------------
_NICKNAME_TO_FULL = {
    # nickname (lowercase) → full first name (lowercase)
    # Add new mappings here as the team grows. Bidirectional — applies
    # both to incoming nicknames AND to nicknames inside canonical names.
    "ben": "bernard",
    "bernie": "bernard",
    # room to grow: "yauba": "yauba"  (no-op — placeholder)
}

# ---------------------------------------------------------------------------
# Full-name aliases — maps an INPUT string (lowercased, whitespace-normalised)
# to the canonical full name it should resolve to.
#
# Why this exists in addition to _NICKNAME_TO_FULL: the nickname dict is
# keyed on individual TOKENS ("ben" → "bernard"), but some cases need to
# override an exact-match-on-itself. If "Clarence" is also present in the
# team_members dropdown as a standalone entry, the exact-match shortcut
# in `normalize_member_name` returns "Clarence" before the subset rule
# can match it to "Clarence Bongo". This alias dict short-circuits that
# trap so first-name-only mentions roll up to the right person.
#
# Add new entries when two people don't share a first name AND the
# shorter form keeps showing up in the data (Excel imports, old form
# submissions, etc.). Format: lowercased input → exact canonical name.
# ---------------------------------------------------------------------------
_FULLNAME_ALIASES = {
    "clarence": "Clarence Bongo",
}


def _title_case(s: str) -> str:
    """ALL CAPS / lower / Mixed → Sentence Case per word. Keeps hyphens and
    apostrophes intact (so 'O'Brien' stays 'O'Brien' rather than 'O'brien')."""
    if not s:
        return ""
    parts = []
    for word in str(s).strip().split():
        # Preserve apostrophe casing: O'BRIEN → O'Brien
        if "'" in word:
            chunks = word.split("'")
            parts.append("'".join(c.capitalize() for c in chunks))
        else:
            parts.append(word.capitalize())
    return " ".join(parts)


def _tokenize_name(s: str) -> set[str]:
    """Lowercase token set, with nicknames expanded to their full form."""
    out: set[str] = set()
    for tok in str(s or "").lower().replace("-", " ").split():
        out.add(_NICKNAME_TO_FULL.get(tok, tok))
    return out


@st.cache_data(ttl=300)
def _build_name_resolver(canonical_list: tuple[str, ...]) -> dict[str, str]:
    """Pre-compute a lookup so each lookup at chart-render time is O(1)
    rather than O(n_team_members). Cached for 5 minutes."""
    return {c: c for c in canonical_list}  # identity map seed; resolver does the work


def normalize_member_name(raw: str | None) -> str:
    """Map a raw name string to the canonical team-member name.

    None / empty → "(unknown)" so it still buckets cleanly.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "(unknown)"
    s = str(raw).strip()
    if not s:
        return "(unknown)"
    tidy = _title_case(s)
    canonical_list = tuple(dropdowns.get("team_members") or [])
    if not canonical_list:
        return tidy

    # Full-name alias check (runs BEFORE the exact-match shortcut so
    # collisions like "Clarence" → "Clarence Mbanga" win even when
    # "Clarence" is also a dropdown entry on its own).
    alias_target = _FULLNAME_ALIASES.get(tidy.lower())
    if alias_target:
        for c in canonical_list:
            if c.lower() == alias_target.lower():
                return c
        # Alias target isn't in the dropdown — return it anyway so the
        # rollup happens; downstream chart code doesn't require canonical
        # membership.
        return alias_target

    # Exact match on the cleaned-up form
    for c in canonical_list:
        if c.lower() == tidy.lower():
            return c

    # Subset / nickname match
    input_tokens = _tokenize_name(tidy)
    if not input_tokens:
        return tidy
    matches: list[str] = []
    for c in canonical_list:
        c_tokens = _tokenize_name(c)
        if not c_tokens:
            continue
        # Input ⊆ canonical (e.g. single-name ⊆ full-name) OR
        # canonical ⊆ input (rare — when a fuller form is submitted)
        if input_tokens <= c_tokens or c_tokens <= input_tokens:
            matches.append(c)
    if matches:
        # Tie-break: prefer the canonical name with MORE tokens (the
        # fully-specified form). "First Last" beats "First".
        matches.sort(key=lambda x: (-len(_tokenize_name(x)), x))
        return matches[0]

    return tidy


def split_and_normalize_names(value) -> list[str]:
    """Split a comma-separated name (or list-of-strings) into a flat
    list of canonical names.

    Cases handled:
      * None / NaN / empty                  → []
      * "Jane Doe"                       → ["Jane Doe"]
      * "Alex Kim, Jane Doe"        → ["Alex Kim", "Jane Doe"]
      * ["Alex Kim", "Jane Doe"]    → ["Alex Kim", "Jane Doe"]
        (Postgres text[] arrays — contributors column)
      * ["Alex Kim, Jane Doe"]      → ["Alex Kim", "Jane Doe"]
        (one list element that ITSELF contains commas — common when a
        sloppy form submission packed two names into one entry)

    Each split piece is run through `normalize_member_name()` so
    nickname / case / partial variants collapse to the canonical form.
    "(unknown)" results are filtered out — they only appear when the
    input was empty/None.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            out.extend(split_and_normalize_names(v))
        return out
    parts = [p.strip() for p in str(value).split(",")]
    normalized = [normalize_member_name(p) for p in parts if p]
    return [n for n in normalized if n and n != "(unknown)"]


def first_name_display_map(canonical_names) -> dict[str, str]:
    """Return {canonical_name → display_name} where display is the
    FIRST name only when it's unique, or the full name if two
    canonical members share a first name.

    Example: given ["Jane Doe", "Drew Hall", "Alex Kim"]
      → {"Jane Doe": "Bernard", "Drew Hall": "Yauba", "Alex Kim": "Michael"}

    Example with a collision (two Bernards on the team):
      ["Jane Doe", "Bernard Smith"]
      → {"Jane Doe": "Jane Doe", "Bernard Smith": "Bernard Smith"}

    Shorter labels mean narrower chart legends — important for
    print-to-PDF where wide right-side legends get cut off.
    """
    canonical = [n for n in set(canonical_names) if n and n != "(unknown)"]
    by_first: dict[str, list[str]] = {}
    for name in canonical:
        first = name.split()[0]
        by_first.setdefault(first, []).append(name)
    display: dict[str, str] = {}
    for first, names in by_first.items():
        if len(names) == 1:
            display[names[0]] = first       # unique first name → use it
        else:
            for n in names:
                display[n] = n               # collision → fall back to full
    # Pass through "(unknown)" so it still groups
    display["(unknown)"] = "(unknown)"
    return display


# ---------------------------------------------------------------------------
# FX — every monetary aggregate on this page is shown in USD.
# Conversion happens row-by-row using each row's own currency field via
# `dropdowns.usd_rate()`, which reads `core/dropdowns.yaml` (or the
# admin-edited overrides in app_settings.currencies_json). Rows missing a
# currency fall back to rate=1.0 (treated as USD) so the dashboard never
# crashes on bad data — but those rows are counted in `_missing_currency`
# below and surfaced as a transparency badge on Section 5.
# ---------------------------------------------------------------------------
def _to_usd(amount, currency) -> float:
    """Convert (amount, currency) → USD. Tolerant of None / NaN / blank."""
    if amount is None or pd.isna(amount):
        return 0.0
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return 0.0
    if v == 0:
        return 0.0
    rate = dropdowns.usd_rate(currency)
    return v * rate


def _series_to_usd(amount_col: pd.Series, currency_col: pd.Series | None) -> pd.Series:
    """Vectorised pair conversion. Returns a float Series of USD values
    aligned with `amount_col.index`.

    Robust to a missing / shorter / longer currency column — the column
    is reindexed to amount_col.index first, so passing an empty Series
    (e.g. when the underlying DataFrame doesn't even have the column)
    works the same as a column of NaNs → all rows treated as USD via the
    fallback in _to_usd.
    """
    if amount_col is None or len(amount_col) == 0:
        return pd.Series(dtype=float)
    if currency_col is None or len(currency_col) == 0:
        currency_col = pd.Series([None] * len(amount_col), index=amount_col.index)
    else:
        # Align indexes; rows in amount_col without a matching currency get NaN.
        currency_col = currency_col.reindex(amount_col.index)
    return pd.Series(
        [_to_usd(a, c) for a, c in zip(amount_col, currency_col)],
        index=amount_col.index, dtype=float,
    )

sb = get_client()


# ===========================================================================
# Print-mode CSS — hides Streamlit chrome when the user prints / saves PDF.
# Loaded once at the top of the page so it's in scope for every section.
# ===========================================================================
st.markdown(
    """
    <style>
      /* Print-only rules so window.print() / Save-as-PDF yields a clean
         report — just the title, sections, KPI tiles, charts and tables.
         Everything that's page-only UI (controls, buttons, the Print tip,
         the saved-report line, advanced filter, and the explanatory caption
         text under each section) is hidden. The org letterhead (top bar) and
         the footer (org + generated date + report id) are kept. */
      @media print {
        [data-testid="stSidebar"],
        [data-testid="stToolbar"],
        [data-testid="stHeader"],
        [data-testid="stDecoration"],
        section[data-testid="stSidebarNav"],
        [data-testid="stExpandSidebarButton"],
        button,
        .stDownloadButton,
        [data-testid="stDownloadButton"],
        [data-testid="stButton"],
        details,
        [data-testid="stExpander"],
        iframe,
        [data-testid="stIFrame"],
        [data-testid="stCustomComponentV1"],
        [data-testid="stSelectbox"],
        [data-testid="stNumberInput"],
        [data-testid="stMultiSelect"],
        [data-testid="stCheckbox"],
        [data-testid="stRadio"],
        [data-testid="stTextInput"],
        [data-testid="stDateInput"],
        [data-testid="stAlert"],
        [data-testid="stAlertContainer"],
        [data-testid="stCaptionContainer"] {
          display: none !important;
        }
        /* …but keep the footer (report id + generated date) on the printout. */
        [class*="st-key-report_footer"],
        [class*="st-key-report_footer"] [data-testid="stCaptionContainer"] {
          display: block !important;
        }
        .block-container { padding: 0.5rem !important; max-width: 100% !important; }
        h1, h2, h3 { page-break-after: avoid; }
        .stPlotlyChart, [data-testid="stMetric"] { page-break-inside: avoid; }
        body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
      }
      /* Slightly compact the metric tiles so the report fits on one printed page. */
      [data-testid="stMetric"] { padding: 4px 6px; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ===========================================================================
# Org context — used by per-section labels + Excel-export metadata. The
# org logo / name are NOT rendered here anymore; the global header strip
# (core/app_header.render_app_header) already places them at the top of
# every page. Re-rendering them here would duplicate the branding.
# ===========================================================================
_org = settings.get_org()
_org_name = _org.get("org_name") or "RFPIS"
_org_country = _org.get("org_country") or ""

# Lightweight context line — what dashboard is this, and for whom.
st.caption(
    f"Activity dashboard · {_org_country}" if _org_country
    else "Activity dashboard"
)


# ===========================================================================
# Global controls — Period (when) + View by (granularity) + actions
# ===========================================================================
current_year = settings.get_year()
today = date.today()

# ── Restore a prior report from the URL (refresh-safe + shareable) ─────────
# Clicking Generate writes the whole selection — period, year, month, view-by,
# sections, metrics — plus a unique report id (`rid`) into the URL query
# string. A refresh, a dropped connection, or a shared link then reproduces
# the EXACT report instead of losing the (painstaking) selection. A new
# Generate mints a fresh `rid`, so each report has its own clean URL.
def _qp(name: str, default: str | None = None) -> str | None:
    v = st.query_params.get(name)
    return v if (v is not None and v != "") else default


# A generated report is identified by a short id in the URL: `?r=YYYYMMDD-NNNNNN`.
# The full selection (period, view-by, sections, metrics) lives server-side in
# core.report_snapshots keyed by that id — so the URL stays neat AND the report
# is actually saved for future reference. Opening a link looks the snapshot up
# and restores the exact selection.
_url_rid = _qp("r")
_snap = report_snapshots.get_snapshot(_url_rid)
_has_url_state = _snap is not None
_snap_missing = _url_rid is not None and _snap is None


def _sel(key: str, default=None):
    """A field from the restored snapshot's selection, or `default`."""
    if _snap and key in _snap:
        return _snap[key]
    return default


def _opt_index(options: list[str], value, fallback: int) -> int:
    """Index of `value` in `options`, or `fallback` if it's missing/invalid."""
    return options.index(value) if value in options else fallback


_PERIOD_OPTS = ["This year", "Year to date", "Specific month",
                "Last 90 days", "Last 12 months", "All time"]
_VIEW_OPTS = ["Weekly", "Monthly", "Quarterly", "Semestrial", "Annually"]
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]
try:
    _year_seed = int(_sel("year", int(current_year)))
except (TypeError, ValueError):
    _year_seed = int(current_year)
_year_seed = min(max(_year_seed, 2020), 2050)

pc1, pc2, pc3, pc4, pc5 = st.columns([2, 1.5, 1.5, 2, 3])
period_mode = pc1.selectbox(
    "Period",
    options=_PERIOD_OPTS,
    index=_opt_index(_PERIOD_OPTS, _sel("period"), 1),  # default YTD
    help="Date window for every KPI on this page. Pick 'Specific month' "
         "to drill into a single month.",
)
year_override = pc2.number_input(
    "Year",
    min_value=2020, max_value=2050,
    value=_year_seed, step=1,
    disabled=(period_mode not in ("This year", "Year to date", "Specific month")),
)
# Month picker — only meaningful when period is "Specific month". Disabled
# (but still rendered) otherwise so the layout doesn't shift.
month_override = pc3.selectbox(
    "Month",
    options=_MONTH_NAMES,
    index=_opt_index(_MONTH_NAMES, _sel("month"), min(today.month - 1, 11)),
    disabled=(period_mode != "Specific month"),
    help="The month to drill into. Only used when Period = 'Specific month'.",
)
bucket_mode = pc4.selectbox(
    "View by",
    options=_VIEW_OPTS,
    index=_opt_index(_VIEW_OPTS, _sel("view"), 1),  # default Monthly
    help="Granularity used for every time-series chart. Weekly is detail-heavy; "
         "Quarterly / Annually are good for board reports.",
)


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _period_bounds() -> tuple[date | None, date | None]:
    if period_mode == "This year":
        return date(int(year_override), 1, 1), date(int(year_override), 12, 31)
    if period_mode == "Year to date":
        return date(int(year_override), 1, 1), today
    if period_mode == "Specific month":
        m = _MONTH_NAMES.index(month_override) + 1
        return date(int(year_override), m, 1), _last_day_of_month(int(year_override), m)
    if period_mode == "Last 90 days":
        return today - timedelta(days=90), today
    if period_mode == "Last 12 months":
        return today - timedelta(days=365), today
    return None, None  # All time


_start, _end = _period_bounds()

# Spell out the active period in plain English so chart titles can echo it.
def _period_label() -> str:
    if period_mode == "Specific month":
        return f"{month_override} {int(year_override)}"
    if period_mode == "This year":
        return f"{int(year_override)} (full year)"
    if period_mode == "Year to date":
        return f"YTD {int(year_override)}"
    if period_mode == "All time":
        return "All time"
    return period_mode


_period_label_str = _period_label()
pc5.caption(
    f"**{_period_label_str}** · view {bucket_mode.lower()}  \n  "
    f"{_start.isoformat() if _start else '∞'} → "
    f"{_end.isoformat() if _end else '∞'}"
)


# ===========================================================================
# Bucketing helpers — used by every time-series chart for the View by toggle
# ===========================================================================
def _bucket_start(series: pd.Series, mode: str) -> pd.Series:
    """Snap each date in `series` to the start of its containing bucket.

    Returns a datetime Series. Buckets are non-overlapping and cover the
    calendar exhaustively, so reindexing with the full bucket range
    fills empty buckets with zeros consistently.

    Semestrial = H1 (Jan-Jun) / H2 (Jul-Dec). Calendar semester, not fiscal.
    """
    s = pd.to_datetime(series, errors="coerce")
    if mode == "Weekly":
        return s.dt.to_period("W-SUN").dt.start_time
    if mode == "Monthly":
        return s.dt.to_period("M").dt.start_time
    if mode == "Quarterly":
        return s.dt.to_period("Q").dt.start_time
    if mode == "Semestrial":
        month = s.dt.month
        return pd.to_datetime(
            s.dt.year.astype(str) + "-"
            + np.where(month <= 6, "01", "07") + "-01"
        )
    if mode == "Annually":
        return s.dt.to_period("Y").dt.start_time
    return s


def _bucket_range(start: date, end: date, mode: str) -> pd.DatetimeIndex:
    """Generate every bucket-start between `start` and `end` (inclusive).
    Used to reindex aggregated frames so empty buckets render as zero."""
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if mode == "Weekly":
        return pd.date_range(start - pd.Timedelta(days=6), end, freq="W-MON")
    if mode == "Monthly":
        return pd.date_range(start, end, freq="MS")
    if mode == "Quarterly":
        return pd.date_range(start, end, freq="QS")
    if mode == "Semestrial":
        # Materialise H1/H2 starts explicitly.
        years = range(start.year, end.year + 1)
        idx = []
        for y in years:
            for m in (1, 7):
                d = pd.Timestamp(y, m, 1)
                if d >= start - pd.Timedelta(days=180) and d <= end:
                    idx.append(d)
        return pd.DatetimeIndex(idx)
    if mode == "Annually":
        return pd.date_range(pd.Timestamp(start.year, 1, 1),
                             pd.Timestamp(end.year, 1, 1), freq="YS")
    return pd.DatetimeIndex([])


def _bucket_label(mode: str) -> str:
    """Human-readable axis label."""
    return {
        "Weekly": "Week", "Monthly": "Month", "Quarterly": "Quarter",
        "Semestrial": "Semester", "Annually": "Year",
    }.get(mode, "Period")


def _fmt_bucket_ticks(mode: str) -> dict:
    """Plotly xaxis kwargs for sensible tick formatting per bucket."""
    if mode == "Weekly":
        return {"tickformat": "%G-W%V", "dtick": 7 * 86400 * 1000}  # weekly
    if mode == "Monthly":
        return {"tickformat": "%Y-%m", "dtick": "M1"}
    if mode == "Quarterly":
        return {"tickformat": "%Y-Q%q", "dtick": "M3"}
    if mode == "Semestrial":
        return {"tickformat": "%Y-%m"}  # H1=01, H2=07 — readable as-is
    if mode == "Annually":
        return {"tickformat": "%Y", "dtick": "M12"}
    return {}


# ===========================================================================
# Data loaders — cached per period so toggling is snappy
# ===========================================================================
# Pandas default `to_datetime` infers ONE format from the first row, then
# silently coerces every later row that doesn't match to NaT. Supabase
# emits ISO 8601 strings but with variable precision (some with microseconds,
# some without — depending on whether the row was inserted by the scanner
# or by Excel migration). Passing `format="ISO8601"` makes pandas parse
# the full spec instead of pattern-matching, so mixed precisions all
# round-trip cleanly. Forgetting this was the bug that made all 50 Excel-
# migration rows invisible on the Report's discovery chart.
_DT_KW = dict(errors="coerce", format="ISO8601")


@st.cache_data(ttl=120)
def _load_scan_logs(start_iso: str | None, end_iso: str | None) -> pd.DataFrame:
    q = sb.table("scan_logs").select("*")
    if start_iso:
        q = q.gte("scan_date", start_iso)
    if end_iso:
        q = q.lte("scan_date",
                  (date.fromisoformat(end_iso) + timedelta(days=1)).isoformat())
    res = q.limit(10000).execute()
    df = clean_df(pd.DataFrame(res.data or []))
    if not df.empty:
        df["scan_date"] = pd.to_datetime(df["scan_date"], **_DT_KW)
        for c in ("rfps_found", "rfps_new", "rfps_duplicate", "rfps_rejected"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    return df


@st.cache_data(ttl=120)
def _load_rfps() -> pd.DataFrame:
    # NOTE: every monetary field has its OWN currency column. Two
    # pairings matter on this page:
    #   estimated_value  ↔ currency           (the asked amount)
    #   amount_secured   ↔ currency_secured   (the won amount)
    # An earlier version of this select forgot `currency_secured` — the
    # downstream `.get("currency_secured", default=empty)` then fell
    # through to USD-as-fallback for every row, so a 200,000 GBP grant
    # rendered as $200,000 instead of $266,000. If you add a new
    # monetary field, add its companion currency column here too.
    res = sb.table("rfp_submissions").select(
        "uid,source,opportunity_title,funding_agency,submitted_at,search_date,"
        "submitted_by,submitted_by_email,"
        "submission_deadline,date_completed,decision_date,date_of_approval,"
        "decision,auto_recommendation,donor_decision,progress_status,stage,"
        "alignment_score,estimated_value,currency,"
        "amount_requested,amount_secured,currency_secured,"
        "call_geographic_scope,program_area,decision_overridden_by,"
        "proposal_lead,contributors,reviewers,"
        "lead_applicant,sub_applicant,"
        "is_duplicate,review_week,applicant_role"
    ).limit(50000).execute()
    df = clean_df(pd.DataFrame(res.data or []))
    if df.empty:
        return df
    for c in ("submitted_at", "search_date"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], **_DT_KW)
    for c in ("call_submission_deadline", "date_completed", "decision_date",
              "date_of_approval"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], **_DT_KW).dt.date
    df["is_duplicate"] = df.get("is_duplicate", False).fillna(False)
    return df


@st.cache_data(ttl=120)
def _load_meetings(start_iso: str | None, end_iso: str | None) -> pd.DataFrame:
    q = sb.table("meeting_logs").select("*")
    if start_iso:
        q = q.gte("meeting_date", start_iso)
    if end_iso:
        q = q.lte("meeting_date", end_iso)
    res = q.limit(10000).execute()
    df = clean_df(pd.DataFrame(res.data or []))
    if not df.empty and "meeting_date" in df.columns:
        df["meeting_date"] = pd.to_datetime(df["meeting_date"], **_DT_KW)
        df["is_resolved"] = df.get("is_resolved", False).fillna(False)
    return df


@st.cache_data(ttl=120)
def _load_engagements(start_iso: str | None, end_iso: str | None) -> pd.DataFrame:
    q = sb.table("engagement_logs").select("*")
    if start_iso:
        q = q.gte("engagement_date", start_iso)
    if end_iso:
        q = q.lte("engagement_date", end_iso)
    res = q.limit(10000).execute()
    df = clean_df(pd.DataFrame(res.data or []))
    if not df.empty and "engagement_date" in df.columns:
        df["engagement_date"] = pd.to_datetime(df["engagement_date"], **_DT_KW)
    return df


@st.cache_data(ttl=120)
def _load_grants() -> pd.DataFrame:
    res = sb.table("active_grants").select("*").limit(10000).execute()
    df = clean_df(pd.DataFrame(res.data or []))
    if not df.empty:
        for c in ("award_date", "end_date", "submitted_date", "report_due_date"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c], **_DT_KW).dt.date
    return df


# ===========================================================================
# Advanced filter + Generate gate
# ---------------------------------------------------------------------------
# The report is heavy — 5 sections, ~20 charts, a word cloud, an Excel build.
# Rendering it on every page open made the page feel slow and "half-loaded"
# (a new user couldn't tell whether it had finished). So the entire body
# below is gated behind a Generate button: nothing loads or renders until the
# user clicks. The advanced filter then lets them drop individual charts /
# tables from the run (default = everything selected). All items still honour
# the Period / View-by controls above.
# ===========================================================================
# Report items grouped by section. The advanced filter is a two-level picker:
# choose the SECTIONS you want, then tick the metrics within each. A section
# you don't select is hidden entirely (none of its metrics run). Per-section
# checkboxes show full label text — readable + tappable on a phone, unlike a
# flat 20-item multiselect whose pills truncate to "1 · Search — RFPs d…".
# Everything defaults to on, so Generate with no changes = the full report.
_REPORT_SECTIONS = [
    ("1", "1 · Search activity", [
        ("s1_discovery",  "RFPs discovered by member"),
        ("s1_donor",      "RFPs by donor (top 15)"),
        ("s1_keywords",   "Search keyword cloud"),
        ("s1_kw_success", "Keywords driving success"),
        ("s1_sources",    "Top sources by yield"),
        ("s1_cycle",      "Search → Submission cycle time"),
    ]),
    ("2", "2 · Insights", [
        ("s2_funnel",   "Conversion funnel"),
        ("s2_progress", "Progress status"),
    ]),
    ("3", "3 · Reviews & decisions", [
        ("s3_decdist", "Decision distribution"),
        ("s3_dectime", "Decisions over time"),
        ("s3_autorec", "Auto-recommendation vs decision"),
    ]),
    ("4", "4 · Team & partners", [
        ("s4_eng_ts",    "Donor engagements over time"),
        ("s4_topdonors", "Top donors by touchpoints"),
        ("s4_leads",     "Proposal leads"),
        ("s4_contrib",   "Contributors"),
        ("s4_partners",  "Lead & sub applicant partners"),
    ]),
    ("5", "5 · Our results", [
        ("s5_cumusd", "Cumulative USD secured"),
        ("s5_grants", "Active grants pipeline"),
        ("s5_reqsec", "Requested vs secured"),
        ("s5_conv",   "Conversion rates"),
    ]),
]
_ALL_KEYS = [k for _, _, _items in _REPORT_SECTIONS for k, _ in _items]
_SEC_LABELS = {sid: lbl for sid, lbl, _ in _REPORT_SECTIONS}
_ALL_SEC_IDS = [sid for sid, _, _ in _REPORT_SECTIONS]

# Seed the section / metric pickers from the saved snapshot when present (so a
# refreshed or shared report restores exactly), else default to everything.
_secs_saved = _sel("secs")
_restored_secs = ([s for s in _secs_saved if s in _ALL_SEC_IDS]
                  if isinstance(_secs_saved, list) else list(_ALL_SEC_IDS))
_items_saved = _sel("items")
_restored_items = ({k for k in _items_saved if k in _ALL_KEYS}
                   if isinstance(_items_saved, list) else set(_ALL_KEYS))

# Built every run (even when the expander is collapsed — Streamlit still
# executes the widgets inside it), so the filter applies without re-opening.
_selected_items: set[str] = set()
with st.expander("⚙️ Advanced filter — choose sections & metrics", expanded=False):
    st.caption(
        "Pick the sections you want, then tick the metrics within each. "
        "Leave a section out to hide it entirely. Everything is on by "
        "default, so Generate with no changes gives the full report. All "
        "items honour the **Period** / **View by** controls above."
    )
    _picked_secs = st.multiselect(
        "Sections to include",
        options=_ALL_SEC_IDS,
        default=_restored_secs,
        format_func=lambda s: _SEC_LABELS.get(s, s),
        key="report_secs_ms",
        help="Each section you add reveals its own metric checklist below.",
    )
    for _sid, _slabel, _items in _REPORT_SECTIONS:
        if _sid not in _picked_secs:
            continue
        st.markdown(f"**{_slabel}**")
        for _k, _lbl in _items:
            if st.checkbox(_lbl, value=(_k in _restored_items),
                           key=f"rpt_item_{_k}"):
                _selected_items.add(_k)


def _show(key: str) -> bool:
    """True when a report item is selected (its section is included AND its
    metric checkbox is ticked) in the advanced filter."""
    return key in _selected_items


def _show_sec(sid: str) -> bool:
    """True when an entire section is included in the advanced filter. Gates
    the section header, caption, and KPI tiles (individual charts inside a
    shown section are further gated by `_show`)."""
    return sid in _picked_secs


# Signature of the CURRENT selection — compared against the saved snapshot so
# we can flag unsaved changes (secs/items sorted → order-independent).
_cur_sig = (period_mode, int(year_override), month_override, bucket_mode,
            sorted(_picked_secs), sorted(_selected_items))
_snap_sig = ((_sel("period"), int(_sel("year", 0)), _sel("month"), _sel("view"),
              sorted(_sel("secs") or []), sorted(_sel("items") or []))
             if _snap else None)

_gen_col, _gen_spacer = st.columns([1.4, 5])
if _gen_col.button("▶ Generate report", type="primary",
                   width='stretch',
                   help="Build the report and save it under a short, shareable "
                        "link that survives a refresh or dropped connection. A "
                        "new Generate saves a fresh report."):
    # Persist the whole selection server-side under a short id, then put just
    # that id in the URL (?r=YYYYMMDD-NNNNNN). Neat URL + the report is now
    # actually saved for future reference. clear() drops any stale long params.
    _new_rid = report_snapshots.save_snapshot(
        {
            "period": period_mode,
            "year": int(year_override),
            "month": month_override,
            "view": bucket_mode,
            "secs": sorted(_picked_secs),
            "items": sorted(_selected_items),
        },
        updated_by=(st.session_state.get("app_user") or {}).get("email"),
    )
    st.query_params.clear()
    st.query_params["r"] = _new_rid
    st.session_state["report_generated"] = True
    st.rerun()

# The report is "generated" when the URL carries a valid saved id (refresh-safe)
# or the user just clicked Generate this session.
_generated = _has_url_state or st.session_state.get("report_generated")
if _snap_missing:
    st.warning(
        f"⚠ Report `{_url_rid}` wasn't found — it may have aged out (only the "
        "100 most recent reports are kept). Re-select below and Generate a "
        "fresh one."
    )
if not _generated:
    st.info(
        "Pick your **Period**, **View by**, and (optionally) narrow the "
        "**Advanced filter** above, then click **▶ Generate report**. "
        "Nothing loads until you do — so the page opens instantly, and the "
        "report is saved under a short, shareable link that survives a refresh "
        "or lost connection."
    )
    st.stop()

# Status line: confirm the saved link, or warn about unsaved selection changes.
if _has_url_state and _cur_sig != _snap_sig:
    _gen_spacer.warning(
        "✎ Selections changed — click **▶ Generate report** to save them under "
        "a fresh link. (The current link still shows the last saved report.)"
    )
elif _url_rid:
    _gen_spacer.caption(
        f"🔗 Saved report `{_url_rid}` — shareable; restores on refresh."
    )

st.divider()

_s_iso = _start.isoformat() if _start else None
_e_iso = _end.isoformat() if _end else None

with st.spinner("Generating report — loading data…"):
    scans = _load_scan_logs(_s_iso, _e_iso)
    rfps_all = _load_rfps()
    meetings = _load_meetings(_s_iso, _e_iso)
    engagements = _load_engagements(_s_iso, _e_iso)
    grants = _load_grants()


# Restrict RFPs to the period — used by "discovered in period" type
# metrics. Section 2's pipeline-state view ignores this entirely so Excel-
# imported rows (which often have NULL search_date) still show up.
def _in_period(ts) -> bool:
    if _start is None and _end is None:
        return True  # "All time" — include every row, even those with NULL date
    if ts is None or pd.isna(ts):
        return False  # period filter active + row has no date → exclude
    d = ts.date() if hasattr(ts, "date") else ts
    if _start and d < _start:
        return False
    if _end and d > _end:
        return False
    return True


def _discovery_ts(row) -> pd.Timestamp | None:
    """Best-available discovery timestamp. Falls back to submitted_at when
    search_date is NULL — common for Excel-migration rows where the
    original Excel didn't carry a search_date column."""
    sd = row.get("search_date")
    if sd is not None and not pd.isna(sd):
        return sd
    return row.get("submitted_at")


if not rfps_all.empty:
    rfps_all["_disc_ts"] = rfps_all.apply(_discovery_ts, axis=1)
    rfps_all["_discovered_in_period"] = rfps_all["_disc_ts"].apply(_in_period)
    rfps_all["_decided_in_period"] = rfps_all["decision_date"].apply(_in_period)
    rfps_all["_completed_in_period"] = rfps_all["date_completed"].apply(_in_period)
    rfps_all["_approved_in_period"] = rfps_all["date_of_approval"].apply(_in_period)


# ===========================================================================
# Generic helper: aggregate a date column to bucketed counts/sums, then
# reindex against the full bucket range so empty periods render as zero.
# ===========================================================================
def _bucketed_count(date_series: pd.Series, label: str = "count") -> pd.DataFrame:
    if date_series.empty or _start is None or _end is None:
        # For All time, just use the data's own range
        if date_series.empty:
            return pd.DataFrame({"bucket": [], label: []})
        local_start = date_series.min().date()
        local_end = date_series.max().date()
    else:
        local_start = _start
        local_end = _end

    buckets = _bucket_start(date_series, bucket_mode)
    counts = buckets.value_counts().sort_index()
    full = _bucket_range(local_start, local_end, bucket_mode)
    if len(full):
        counts = counts.reindex(full, fill_value=0)
    counts.index.name = "bucket"
    return counts.rename(label).reset_index()


def _bucketed_sum(date_series: pd.Series, value_series: pd.Series,
                  label: str = "value") -> pd.DataFrame:
    if date_series.empty or _start is None or _end is None:
        if date_series.empty:
            return pd.DataFrame({"bucket": [], label: []})
        local_start = date_series.min().date()
        local_end = date_series.max().date()
    else:
        local_start = _start
        local_end = _end
    bucketed = pd.DataFrame({
        "bucket": _bucket_start(date_series, bucket_mode),
        "v": pd.to_numeric(value_series, errors="coerce").fillna(0),
    })
    agg = bucketed.groupby("bucket")["v"].sum()
    full = _bucket_range(local_start, local_end, bucket_mode)
    if len(full):
        agg = agg.reindex(full, fill_value=0)
    agg.index.name = "bucket"
    return agg.rename(label).reset_index()


# ===========================================================================
# Page actions — tip on the left, Excel export + Print on the right
# (right-aligned so they sit near the natural eye-line for "actions"
# in a left-to-right reading layout).
# ===========================================================================
ac_tip, ac_excel, ac_print = st.columns([6, 1.4, 1.4])


def _safe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    """Sanitise a DataFrame for openpyxl writing.

    Excel can't store:
      * timezone-aware datetimes  — pandas raises ValueError. Postgres
        `timestamptz` round-trips through pandas as tz-aware Timestamps,
        which is most of the failures we see.
      * inf / -inf                — openpyxl raises ValueError.
      * lists / dicts             — comes from Postgres array columns
        (call_geographic_scope, program_area). JSON-stringify them.
      * other non-primitive types — fall back to str().
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    # 1. Strip timezone from every datetime column.
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_datetime64_any_dtype(s) and getattr(s.dt, "tz", None) is not None:
            out[col] = s.dt.tz_localize(None)
    # 2. Replace inf / -inf with NaN in numeric columns.
    num_cols = out.select_dtypes(include=["float", "int"]).columns
    if len(num_cols):
        out[num_cols] = out[num_cols].replace([np.inf, -np.inf], np.nan)
    # 3. Coerce lists / dicts / other complex objects in object columns.
    for col in out.select_dtypes(include="object").columns:
        out[col] = out[col].apply(_excel_cell_value)
    return out


def _excel_cell_value(v):
    """Single-cell coercion: lists/dicts → JSON, primitives passed through."""
    if v is None:
        return None
    if isinstance(v, (list, tuple, set)):
        try:
            return json.dumps(list(v), default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        try:
            return json.dumps(v, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(v)
    return v


def _build_excel_export() -> bytes:
    """Multi-sheet workbook of the underlying data for the active period."""
    buf = io.BytesIO()
    rfps_period = (rfps_all[rfps_all["_discovered_in_period"]]
                   if (_start and not rfps_all.empty) else rfps_all)
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Summary sheet — top-line KPIs
        summary_rows = [
            ("Organization",        _org_name),
            ("Country",             _org_country or ""),
            ("Period",              f"{period_mode} ({_s_iso or '∞'} → {_e_iso or '∞'})"),
            ("View by",             bucket_mode),
            ("Report generated",    datetime.now().strftime("%Y-%m-%d %H:%M")),
            ("",                    ""),
            ("Scan runs",           int(scans["scan_date"].dt.normalize().nunique()) if not scans.empty else 0),
            ("RFPs found (scans)",  int(scans["rfps_found"].sum()) if not scans.empty else 0),
            ("New (admitted)",      int(scans["rfps_new"].sum()) if not scans.empty else 0),
            ("Duplicates filtered", int(scans["rfps_duplicate"].sum()) if not scans.empty else 0),
            ("Rejected at gate",    int(scans.get("rfps_rejected", pd.Series(dtype=int)).sum())),
            ("Unique RFPs (period)", int(len(rfps_period[~rfps_period["is_duplicate"]])) if not rfps_period.empty else 0),
            ("Meeting items",       int(len(meetings))),
            ("Engagement entries",  int(len(engagements))),
            ("Active grants",       int(len(grants))),
            ("",                    ""),
            ("FX policy",
             "Monetary values converted row-by-row to USD via FX rates in "
             "Admin → Settings (fallback: config/dropdowns.yaml). Rows "
             "missing a currency are treated as USD."),
        ]
        pd.DataFrame(summary_rows, columns=["Metric", "Value"]).to_excel(
            writer, sheet_name="Summary", index=False,
        )
        # Per-section raw data — every frame goes through _safe_for_excel
        # which strips timezone, replaces inf, and JSON-stringifies lists.
        if not scans.empty:
            _safe_for_excel(scans).to_excel(
                writer, sheet_name="Scan logs", index=False)
        if not rfps_period.empty:
            rfps_clean = rfps_period.drop(
                columns=[c for c in rfps_period.columns if c.startswith("_")],
                errors="ignore",
            )
            _safe_for_excel(rfps_clean).to_excel(
                writer, sheet_name="RFPs", index=False)
        if not meetings.empty:
            _safe_for_excel(meetings).to_excel(
                writer, sheet_name="Meeting logs", index=False)
        if not engagements.empty:
            _safe_for_excel(engagements).to_excel(
                writer, sheet_name="Engagement logs", index=False)
        if not grants.empty:
            _safe_for_excel(grants).to_excel(
                writer, sheet_name="Active grants", index=False)
    buf.seek(0)
    return buf.getvalue()


ac_tip.caption(
    "💡 Print works best with **landscape** orientation. Save as PDF via "
    "your browser's print dialog → 'Save as PDF' destination. The Excel "
    "export ships every section's raw data as separate sheets."
)

ac_excel.download_button(
    "📥 Excel",
    data=_build_excel_export(),
    file_name=(
        f"rfpis-report-{_org.get('org_short') or 'org'}-"
        f"{(_s_iso or 'alltime')}-to-{(_e_iso or 'now')}.xlsx"
    ).replace(" ", "_"),
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    help="Multi-sheet workbook: Summary KPIs + raw data for every section.",
    width='stretch',
)

# Print button — rendered via st.components.v1.html so the inline
# onclick handler isn't stripped (Streamlit's markdown sanitiser drops
# event handlers even with unsafe_allow_html=True). The button lives in
# its own iframe, so window.print() inside the iframe would print just
# the iframe — call window.parent.print() to print the main Streamlit
# page where the @media print CSS rules at the top of this file apply.
with ac_print:
    components.html(
        """
        <script>
        function rfpisPrint() {
          try {
            // Print the parent window (main Streamlit app, with our print CSS)
            window.parent.print();
          } catch (e) {
            // Cross-origin block (unlikely on Streamlit Cloud since both
            // are same-origin, but possible in some embed scenarios) —
            // fall back to printing the iframe so something happens.
            window.print();
          }
        }
        </script>
        <button onclick="rfpisPrint()" style="
          background:#003366; color:#fff; border:none;
          padding:0.31rem 0.75rem; border-radius:0.5rem; cursor:pointer;
          font-size:0.875rem; width:100%; line-height:1.6;
          font-weight:400;
          font-family: 'Source Sans Pro', 'Segoe UI', sans-serif;">
          🖨 Print / PDF
        </button>
        """,
        height=40,
    )

st.divider()


# ===========================================================================
# SECTION 1 — Search activity (top of funnel)
# ===========================================================================
if _show_sec("1"):
    st.subheader("1 · Search activity")
    st.caption(
        "How the automated scanner is performing. KPI tiles come from "
        "`scan_logs` (one row per source per run). The time-series chart "
        "below tracks **RFPs discovered** using each row's `search_date`, "
        "so the curve covers the full period — not just days a scan ran."
    )

    if scans.empty:
        st.info("No scans recorded in this period yet. Trigger one from "
                "Admin → Manual Scan or wait for the Friday cron.")
    else:
        n_runs = scans["scan_date"].dt.normalize().nunique()
        n_found = int(scans["rfps_found"].sum())
        n_new = int(scans["rfps_new"].sum())
        n_dup = int(scans["rfps_duplicate"].sum())
        n_rej = int(scans.get("rfps_rejected", pd.Series(dtype=int)).sum())
        err_runs = int((scans.get("errors", pd.Series(dtype=str)).fillna("") != "").sum())

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("Scan runs", n_runs,
                  help="Distinct days a scan was triggered (cron or manual).")
        k2.metric("RFPs found", n_found,
                  help="Total candidates returned by donor sources.")
        k3.metric("New (admitted)", n_new,
                  help="Passed eligibility gate + inserted into the DB.")
        k4.metric("Duplicates", n_dup,
                  help="Caught by dedup (URL / opportunity_id / title / triple).")
        k5.metric("Rejected at gate", n_rej,
                  help="Filtered out at scan-time by the eligibility gate "
                       "(deadline / country / theme / closure / language). "
                       "These NEVER enter the DB.")
        k6.metric("Source errors", err_runs,
                  delta_color="inverse" if err_runs else "off",
                  help="Scan attempts that crashed mid-run (network timeout, "
                       "donor portal HTML changed, parser exception). Each "
                       "counts one source-run. Open Admin → Manual Scan "
                       "history for the actual error messages.")

        # Discovery timeline — driven by rfp_submissions.search_date so the
        # curve covers the entire period (not just days scans ran). Each
        # bar is STACKED by submitted_by so you can see who's contributing
        # to the funnel month-over-month.
        if not rfps_all.empty:
            disc = rfps_all[~rfps_all["is_duplicate"]].copy()
            if _start:
                disc = disc[disc["_discovered_in_period"]]
            if _show("s1_discovery") and not disc.empty and disc["search_date"].notna().any():
                # Stack-by-submitter — split comma-separated names, normalize,
                # and use first-name display when unique (same logic as
                # Section 4's Submissions chart).
                disc_with_members = disc.assign(
                    _members=disc["submitted_by"].apply(split_and_normalize_names),
                    bucket=_bucket_start(disc["search_date"], bucket_mode),
                )
                exp_disc = disc_with_members.explode("_members").dropna(subset=["_members"])
                exp_disc = exp_disc[exp_disc["_members"] != ""]
                if not exp_disc.empty:
                    disp_map = first_name_display_map(exp_disc["_members"])
                    exp_disc["submitter"] = exp_disc["_members"].map(disp_map).fillna(exp_disc["_members"])
                    stacked_disc = (
                        exp_disc.groupby(["bucket", "submitter"]).size()
                        .reset_index(name="RFPs discovered")
                    )
                    fig = px.bar(
                        stacked_disc, x="bucket", y="RFPs discovered",
                        color="submitter", barmode="stack",
                        title=f"RFPs discovered by member ({_period_label_str}, {bucket_mode.lower()})",
                        labels={"bucket": _bucket_label(bucket_mode), "submitter": "Submitted by"},
                    )
                    fig.update_layout(
                        height=360, margin=dict(t=40, b=10),
                        xaxis=_fmt_bucket_ticks(bucket_mode),
                        legend=dict(orientation="h", yanchor="top", y=-0.18,
                                    xanchor="center", x=0.5, font=dict(size=11)),
                    )
                    st.plotly_chart(fig, width='stretch')

                    # ─── Submission leaderboard ─────────────────────────────
                    # Moved here from Section 4 (Team & Partnership Activity)
                    # so the totals-per-member sit directly under the chart
                    # showing the per-bucket breakdown — same data, two views.
                    # Uses `exp_disc["submitter"]` which already has the
                    # first-name display map applied (collision-safe).
                    leader_series = (
                        exp_disc["submitter"].value_counts().reset_index()
                    )
                    leader_series.columns = ["Member", "RFPs discovered"]
                    with st.expander("Submission leaderboard", expanded=False):
                        st.dataframe(leader_series,
                                      width='stretch', hide_index=True)
                else:
                    # No submitter data — fall back to a plain bucket count
                    disc_df = _bucketed_count(disc["search_date"].dropna(), "RFPs discovered")
                    fig = px.bar(
                        disc_df, x="bucket", y="RFPs discovered",
                        title=f"RFPs discovered ({_period_label_str}, {bucket_mode.lower()})",
                        labels={"bucket": _bucket_label(bucket_mode)},
                    )
                    fig.update_layout(height=320, margin=dict(t=40, b=10),
                                      xaxis=_fmt_bucket_ticks(bucket_mode))
                    st.plotly_chart(fig, width='stretch')
            elif _show("s1_discovery"):
                st.info(f"No RFPs discovered in this {period_mode.lower()} period yet.")

            # ───── RFPs by donor (non-time-series) ──────────────────────────
            # Snapshot of which donors contribute the most RFPs to the
            # pipeline. Top 15, horizontal so long donor names fit.
            if _show("s1_donor") and not disc.empty:
                donor_counts = (
                    disc["funding_agency"].fillna("(unspecified)")
                    .replace("", "(unspecified)")
                    .value_counts().head(15).reset_index()
                )
                donor_counts.columns = ["Donor", "RFPs"]
                if not donor_counts.empty:
                    fig_dn = px.bar(
                        donor_counts, x="RFPs", y="Donor", orientation="h",
                        text="RFPs",
                        title=f"RFPs by donor — top 15 ({_period_label_str})",
                        color_discrete_sequence=["#10b981"],
                    )
                    fig_dn.update_layout(
                        height=max(280, 28 * len(donor_counts) + 80),
                        margin=dict(t=40, b=10),
                        yaxis={"categoryorder": "total ascending"},
                    )
                    st.plotly_chart(fig_dn, width='stretch')

            # ───── Keyword cloud — niche-vocabulary, frequency-sized ────────
            # Replaces the old "by program area" bar chart. Source is the
            # RFP title + description (NOT the program-area classifier
            # labels). Words go through `core.keyword_cloud` which:
            #   * tokenizes single words (HIV/AIDS → hiv, aids)
            #   * stems related forms together (finance/financing/financed
            #     → "Financing"; vaccine/vaccination → "Vaccine")
            #   * keeps only words in a curated ~80-stem global-health
            #     niche vocabulary — surfaces the topics we should be
            #     searching more aggressively, not a sprawling cloud of
            #     top-200 English words.
            # Font size scales to frequency via WordCloud.generate_from_
            # frequencies — that IS the "size grows with count" effect.
            if _show("s1_keywords") and not disc.empty:
                from core.keyword_cloud import extract_keyword_frequencies

                _titles_and_briefs = [
                    " ".join([
                        str(r.get("opportunity_title") or ""),
                        str(r.get("brief_description") or ""),
                    ])
                    for _, r in disc.iterrows()
                ]
                kw_freq = extract_keyword_frequencies(_titles_and_briefs)
                if kw_freq:
                    st.markdown(
                        f"#### Search Keywords ({_period_label_str})"
                    )
                    st.caption(
                        "Word size scales to how often the keyword (or any of "
                        "its variants — e.g. *Financing* covers finance / "
                        "financed / financial) appears across RFP titles + "
                        "briefs. Vocabulary is a curated global-health niche."
                    )
                    try:
                        from wordcloud import WordCloud
                        import matplotlib.pyplot as plt

                        wc = WordCloud(
                            width=1600, height=600,
                            background_color="white",
                            colormap="viridis",
                            prefer_horizontal=0.9,
                            collocations=False,
                            relative_scaling=0.6,    # font scales ~linearly with count
                            min_font_size=12,
                            max_font_size=180,
                        ).generate_from_frequencies(kw_freq)
                        fig_wc, ax = plt.subplots(figsize=(14, 5))
                        ax.imshow(wc, interpolation="bilinear")
                        ax.axis("off")
                        fig_wc.tight_layout(pad=0)
                        st.pyplot(fig_wc, width='stretch')
                        plt.close(fig_wc)
                    except ImportError:
                        # Graceful fallback when WordCloud / matplotlib not
                        # installed (e.g. Streamlit Cloud pre-deploy). Show a
                        # ranked list so the user still gets the signal.
                        st.info(
                            "Word cloud requires `wordcloud` + `matplotlib`. "
                            "Run `pip install wordcloud matplotlib` to enable "
                            "the visual cloud. Top keywords shown below."
                        )
                        _kw_top = (
                            pd.DataFrame(
                                sorted(kw_freq.items(), key=lambda kv: -kv[1]),
                                columns=["Keyword", "Hits"],
                            ).head(40)
                        )
                        st.dataframe(_kw_top, width='stretch',
                                     hide_index=True)

            # ───── Keyword cloud + success table ─────────────────────────────
            # Frequency of program-area keywords across RFP titles +
            # descriptions. The cloud surfaces what topics dominate the
            # incoming pipeline; the success table ranks keywords by how
            # often the RFPs containing them progressed to Proceed /
            # Submitted / Approved — actionable signal for what to search
            # for more aggressively.
            if _show("s1_kw_success") and not disc.empty:
                # Use the SAME curated `keyword_cloud` vocabulary as the
                # visual cloud above (not the legacy program_area_classifier
                # keywords). This makes the table and the cloud consistent
                # — and crucially picks up bare acronyms like "AI" that the
                # classifier only knew as "artificial intelligence". Per
                # user feedback: AI-titled RFPs were being missed because
                # the classifier vocabulary required the spelled-out form.
                from core.keyword_cloud import extract_keyword_frequencies

                _kw_rows = []
                for _, r in disc.iterrows():
                    text = " ".join([
                        str(r.get("opportunity_title") or ""),
                        str(r.get("brief_description") or ""),
                    ])
                    if not text.strip():
                        continue
                    # Presence per RFP (set of stems), NOT token occurrence
                    # count — so an RFP saying "AI" twice still counts as
                    # one AI hit on the conversion side.
                    stems_in_row = set(extract_keyword_frequencies([text]).keys())
                    if not stems_in_row:
                        continue
                    decision_str = str(r.get("decision") or "").lower()
                    progress_str = str(r.get("progress_status") or "").lower()
                    donor_str = str(r.get("donor_decision") or "").lower()
                    is_proceed = decision_str.startswith("proceed")
                    is_submitted = progress_str == "completed"
                    is_approved = donor_str == "approved"
                    for kw in stems_in_row:
                        _kw_rows.append({
                            "keyword": kw,
                            "is_proceed": is_proceed,
                            "is_submitted": is_submitted,
                            "is_approved": is_approved,
                        })
                if _kw_rows:
                    kw_df = pd.DataFrame(_kw_rows)
                    kw_agg = (
                        kw_df.groupby("keyword").agg(
                            Hits=("keyword", "count"),
                            Proceed=("is_proceed", "sum"),
                            Submitted=("is_submitted", "sum"),
                            Approved=("is_approved", "sum"),
                        ).reset_index()
                        .sort_values(
                            ["Approved", "Submitted", "Proceed", "Hits"],
                            ascending=[False, False, False, False],
                        )
                        .head(50)
                    )

                    # ─── Keyword success table ───
                    # The visual cloud above answers "what topics dominate";
                    # this table answers "which topics convert" — same
                    # vocabulary, broken out by Proceed / Submitted / Approved.
                    with st.expander(
                        "Keywords driving success — top by Approved / Submitted / Proceed",
                        expanded=False,
                    ):
                        st.caption(
                            "One row per niche keyword. Hits = number of RFPs "
                            "whose title or brief mentions that keyword (or any "
                            "of its stemmed variants). Use this to focus future "
                            "donor searches on the topics that consistently win. "
                            "Sorted by Approved → Submitted → Proceed → Hits."
                        )
                        st.dataframe(
                            kw_agg.rename(columns={"keyword": "Keyword"}),
                            width='stretch', hide_index=True,
                        )

        # Top sources by yield
        src = (
            scans.groupby("source")
            .agg(runs=("scan_date", "count"),
                 found=("rfps_found", "sum"),
                 new=("rfps_new", "sum"),
                 rejected=("rfps_rejected", "sum"),
                 avg_dur=("duration_sec", "mean"))
            .reset_index()
            .sort_values("new", ascending=False)
            .head(15)
        )
        if _show("s1_sources") and not src.empty:
            src["avg_dur"] = src["avg_dur"].round(1)
            src["yield_pct"] = ((src["new"] / src["found"].replace(0, 1)) * 100).round(1)
            with st.expander("Top 15 sources by new-RFP yield", expanded=False):
                st.dataframe(
                    src.rename(columns={
                        "source": "Source", "runs": "Runs", "found": "Found",
                        "new": "New", "rejected": "Rejected",
                        "avg_dur": "Avg duration (s)", "yield_pct": "Yield %",
                    }),
                    width='stretch', hide_index=True,
                )

        # ───────────── Search → Submission cycle time (relocated from §4) ─────
        # Lives in Section 1 because Search Date is the anchor of the metric —
        # it's the search-to-submission lag, conceptually a search-activity
        # quality signal. (Originally placed in §4 Team Activity which was
        # the wrong narrative beat.)
        if _show("s1_cycle") and not rfps_all.empty:
            st.markdown("##### Search → Submission cycle time")
            cyc_src = rfps_all[~rfps_all["is_duplicate"]].copy()
            cyc = cyc_src.dropna(subset=["search_date", "date_completed"]).copy()
            if not cyc.empty:
                cyc["days"] = cyc.apply(
                    lambda r: (r["date_completed"] - r["search_date"].date()).days,
                    axis=1,
                )
                cyc = cyc[cyc["days"] >= 0]
            if not cyc.empty:
                ct1, ct2, ct3, ct4 = st.columns(4)
                ct1.metric("Median days", f"{cyc['days'].median():.0f}")
                ct2.metric("Mean days",   f"{cyc['days'].mean():.0f}")
                ct3.metric("Min days",    f"{int(cyc['days'].min())}")
                ct4.metric("Max days",    f"{int(cyc['days'].max())}")
                fig_cyc = px.histogram(
                    cyc, x="days", nbins=20,
                    title="Distribution of days from Search Date → Date Completed",
                    labels={"days": "Days", "count": "RFPs"},
                )
                fig_cyc.update_layout(height=280, margin=dict(t=40, b=10))
                st.plotly_chart(fig_cyc, width='stretch')
            else:
                st.info("No RFPs with both search_date and date_completed yet — "
                        "cycle time chart will populate as proposals get submitted.")

    st.divider()


# ===========================================================================
# SECTION 2 — Pipeline funnel (insights from triage)
# ===========================================================================
if _show_sec("2"):
    st.subheader("2 · Insights — Eligibility Funnel")
    st.caption(
        "**Current pipeline state across ALL stored RFPs** — auto-scanned, "
        "manually submitted, AND imported from the legacy Excel workbook. "
        "These KPIs ignore the period filter so the funnel always reflects "
        "the full picture. Use the **'Discovered in period'** badge below to "
        "see how many of these were added during the active period."
    )

    if rfps_all.empty:
        st.info("No RFPs in the database yet.")
    else:
        unique_all = rfps_all[~rfps_all["is_duplicate"]].copy()
        in_period = unique_all[unique_all["_discovered_in_period"]]

        # All-time current pipeline state — what the user actually wants
        total_all = int(len(unique_all))
        proceed_all = int(unique_all["decision"].fillna("").str.lower().str.startswith("proceed").sum())
        park_all = int((unique_all["decision"].fillna("").str.lower() == "park").sum())
        decline_all = int((unique_all["decision"].fillna("").str.lower() == "decline").sum())
        no_decision_all = total_all - (proceed_all + park_all + decline_all)
        submitted_all = int((unique_all["progress_status"].fillna("").str.lower() == "completed").sum())
        approved_all = int((unique_all["donor_decision"].fillna("").str.lower() == "approved").sum())

        # Period-restricted: how many RFPs were *discovered* in this window
        discovered_in_period = int(len(in_period))

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total unique RFPs", total_all,
                  delta=f"{discovered_in_period} discovered in period",
                  delta_color="off",
                  help="All non-duplicate rows across every source "
                       "(auto-scanned, manually submitted, Excel migration).")
        k2.metric("Proceed", proceed_all,
                  help="Decision = Proceed. The actionable pipeline.")
        k3.metric("Park", park_all,
                  help="Held for later review — uncertain fit or no extractable deadline.")
        k4.metric("Decline", decline_all,
                  help="Filtered by the team or the decision tree.")

        # By-source breakdown so the user can see how much came from where
        if "source" in unique_all.columns:
            src_counts = unique_all["source"].fillna("(unknown)").value_counts()
            st.caption(
                "**By source:**  "
                + "  ·  ".join(f"`{k}` {v}" for k, v in src_counts.items())
            )

        funnel_data = pd.DataFrame({
            "stage": ["Discovered (all-time)", "Proceed-track", "Submitted", "Approved"],
            "count": [total_all, proceed_all, submitted_all, approved_all],
        })
        fig_funnel = go.Figure(go.Funnel(
            y=funnel_data["stage"], x=funnel_data["count"],
            textinfo="value+percent initial",
            marker={"color": ["#3b82f6", "#10b981", "#f59e0b", "#22c55e"]},
        ))
        fig_funnel.update_layout(height=320, margin=dict(t=20, b=10),
                                 title="Conversion funnel — all stored RFPs")
        if _show("s2_funnel"):
            st.plotly_chart(fig_funnel, width='stretch')
        # Decision-distribution chart used to live here; moved to Section 3
        # since "where decisions land" belongs with the Reviews & Decisions
        # narrative, not the funnel.

        # ───────────── Progress status (relocated from Section 4) ─────────
        # Belongs in Insights because it's another "where is each RFP in the
        # pipeline?" view — the funnel above shows attrition, this shows the
        # current workflow distribution. Same narrative beat.
        if _show("s2_progress") and "progress_status" in unique_all.columns:
            ps = (
                unique_all["progress_status"].fillna("(unset)")
                .replace("", "(unset)")
                .value_counts().reset_index()
                .rename(columns={"progress_status": "Status", "count": "RFPs"})
            )
            fig_ps = px.bar(ps, x="Status", y="RFPs", text="RFPs",
                            title="Progress status — all RFPs", color="Status")
            fig_ps.update_layout(height=300, showlegend=False,
                                 margin=dict(t=40, b=10), xaxis_title=None)
            st.plotly_chart(fig_ps, width='stretch')

    st.divider()


# ===========================================================================
# SECTION 3 — Reviews & Decisions
# ===========================================================================
if _show_sec("3"):
    st.subheader("3 · Reviews & Decisions")
    st.caption(
        "Triage outcomes — velocity from RFP discovery to decision, and where "
        "the auto-recommendation needed manual override."
    )

    if rfps_all.empty:
        st.info("No RFPs to review yet.")
    else:
        decided = rfps_all[~rfps_all["is_duplicate"]].copy()
        if _start:
            decided = decided[decided["_decided_in_period"]]
        overridden = decided[decided["decision_overridden_by"].notna()]

        if not decided.empty and "search_date" in decided.columns:
            dec_with_subm = decided.dropna(subset=["search_date", "decision_date"]).copy()
            if not dec_with_subm.empty:
                dec_with_subm["days_to_decide"] = dec_with_subm.apply(
                    lambda r: (r["decision_date"] - r["search_date"].date()).days,
                    axis=1,
                )
                avg_days = dec_with_subm["days_to_decide"].mean()
                med_days = dec_with_subm["days_to_decide"].median()
            else:
                avg_days = med_days = None
        else:
            avg_days = med_days = None

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Decisions made", int(len(decided)))
        k2.metric("Avg days to decide",
                  f"{avg_days:.1f}" if avg_days is not None and not pd.isna(avg_days) else "—",
                  help="Mean days from RFP discovery (search_date) to decision_date.")
        k3.metric("Median days to decide",
                  f"{med_days:.1f}" if med_days is not None and not pd.isna(med_days) else "—")
        k4.metric("Overridden", int(len(overridden)),
                  help="Decisions where the reviewer disagreed with the auto-recommendation.")

        # Decision Distribution — across ALL stored RFPs (not period-restricted).
        # Placed BEFORE the time-series so the eye sees the static "where we
        # land overall" snapshot first, then descends into the time-bucketed
        # cadence of "when decisions happened in this period".
        if _show("s3_decdist") and not rfps_all.empty:
            _unique_all = rfps_all[~rfps_all["is_duplicate"]]
            _proceed = int(_unique_all["decision"].fillna("").str.lower().str.startswith("proceed").sum())
            _park = int((_unique_all["decision"].fillna("").str.lower() == "park").sum())
            _decline = int((_unique_all["decision"].fillna("").str.lower() == "decline").sum())
            _no_dec = int(len(_unique_all)) - (_proceed + _park + _decline)
            dec_df = pd.DataFrame({
                "decision": ["Proceed", "Park", "Decline", "No decision"],
                "count": [_proceed, _park, _decline, _no_dec],
            })
            # Horizontal bars — easier to scan + labels never collide on
            # narrow widths. Order by count descending so the dominant
            # decision sits at the top.
            fig_dec = px.bar(
                dec_df, x="count", y="decision", text="count",
                orientation="h",
                title="Decision Distribution",
                color="decision",
                color_discrete_map={
                    "Proceed": "#10b981", "Park": "#f59e0b",
                    "Decline": "#ef4444", "No decision": "#9ca3af",
                },
            )
            fig_dec.update_layout(height=280, showlegend=False,
                                  margin=dict(t=40, b=10),
                                  xaxis_title="RFPs", yaxis_title=None,
                                  yaxis={"categoryorder": "total ascending"})
            st.plotly_chart(fig_dec, width='stretch')

        if _show("s3_dectime") and not decided.empty:
            dec_dates = pd.to_datetime(decided["decision_date"], errors="coerce").dropna()
            if not dec_dates.empty:
                ts_df = _bucketed_count(dec_dates, "decisions")
                fig = px.bar(ts_df, x="bucket", y="decisions",
                             title=f"Decisions ({_period_label_str}, {bucket_mode.lower()})",
                             labels={"bucket": _bucket_label(bucket_mode)})
                fig.update_layout(height=280, margin=dict(t=40, b=10),
                                  xaxis=_fmt_bucket_ticks(bucket_mode))
                st.plotly_chart(fig, width='stretch')

        if _show("s3_autorec") and not decided.empty:
            comp = (
                decided.assign(
                    ar=decided["auto_recommendation"].fillna("—"),
                    dec=decided["decision"].fillna("—"),
                )
                .groupby(["ar", "dec"]).size().reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            with st.expander("Auto-recommendation vs final decision", expanded=False):
                st.dataframe(
                    comp.rename(columns={"ar": "Auto-recommendation", "dec": "Final decision"}),
                    width='stretch', hide_index=True,
                )

    st.divider()


# ===========================================================================
# SECTION 4 — Team & Partnership Activity
# Single umbrella covering everything about WHO does the work and WHO we
# do it with: internal meetings, donor engagements, individual member
# activity, proposal leads / contributors, lead & sub applicant partners,
# status mix, requested-vs-secured economics, conversion + cycle time.
# ===========================================================================
if _show_sec("4"):
    st.subheader("4 · Team & Partnership Activity")
    st.caption(
        "Everything about WHO does the work and WHO we do it with — internal "
        "triage meetings (**Team Touchpoints**), donor-facing engagements "
        "(**Donor Touchpoints**), member-level submission activity, proposal "
        "leadership, partner trends, status mix, funding economics, and the "
        "Proceed-to-Submitted conversion."
    )

    # ─── Team Touchpoints (internal team meetings) ─────────────────────────────
    st.markdown("##### Team Touchpoints")
    n_meetings_total = int(len(meetings))
    n_resolved = int(meetings["is_resolved"].sum()) if not meetings.empty else 0
    n_open = n_meetings_total - n_resolved
    pct_unresolved = (n_open / n_meetings_total * 100) if n_meetings_total > 0 else 0.0

    bk1, bk2, bk3, bk4 = st.columns(4)
    bk1.metric("Meeting items logged", n_meetings_total,
               help="Total action items captured across weekly team-call notes.")
    bk2.metric("Open action items", n_open,
               help="Items where `is_resolved = False`. Still awaiting closure.")
    bk3.metric("Resolved action items", n_resolved,
               help="Items where `is_resolved = True`. Closed out.")
    bk4.metric("% Unresolved",
               f"{pct_unresolved:.1f}%" if n_meetings_total > 0 else "—",
               delta_color="inverse" if pct_unresolved > 50 else "off",
               help="Open ÷ Total. High values = follow-up debt building up.")

    st.markdown("")  # vertical spacer between the two sub-sections

    # ─── Donor Touchpoints (external donor engagements) ───────────────────────
    st.markdown("##### Donor Touchpoints")
    n_engagements = int(len(engagements))
    n_donors = (int(engagements["donor"].nunique())
                if (not engagements.empty and "donor" in engagements.columns) else 0)

    dk1, dk2, dk3, dk4 = st.columns(4)
    dk1.metric("Donor engagements", n_engagements,
               help="Total engagement entries logged in the period.")
    dk2.metric("Distinct donors engaged", n_donors,
               help="Unique donor names across all engagement entries.")
    # Avg touchpoints per donor — useful "depth" signal alongside breadth
    avg_per_donor = (n_engagements / n_donors) if n_donors > 0 else 0.0
    dk3.metric("Avg touchpoints / donor",
               f"{avg_per_donor:.1f}" if n_donors else "—",
               help="Engagements ÷ Distinct donors. >2 suggests durable relationships.")
    # Engagements linked to specific RFPs vs general
    n_linked = int(engagements["linked_rfp_uid"].notna().sum()) if not engagements.empty and "linked_rfp_uid" in engagements.columns else 0
    dk4.metric("Tied to an RFP", n_linked,
               help="Engagement entries that linked to a specific opportunity_uid.")

    if _show("s4_eng_ts") and not engagements.empty and engagements["engagement_date"].notna().any():
        eng_df = _bucketed_count(engagements["engagement_date"].dropna(), "engagements")
        fig = px.bar(eng_df, x="bucket", y="engagements",
                     title=f"Donor engagements ({_period_label_str}, {bucket_mode.lower()})",
                     labels={"bucket": _bucket_label(bucket_mode)})
        fig.update_layout(height=280, margin=dict(t=40, b=10),
                          xaxis=_fmt_bucket_ticks(bucket_mode))
        st.plotly_chart(fig, width='stretch')

    if _show("s4_topdonors") and not engagements.empty and "donor" in engagements.columns:
        top_donors = (
            engagements.groupby("donor")
            .agg(touchpoints=("engagement_date", "count"),
                 types=("engagement_type",
                        lambda x: ", ".join(sorted(set(filter(None, x))))[:80]))
            .reset_index()
            .sort_values("touchpoints", ascending=False)
            .head(10)
        )
        with st.expander("Top 10 donors by touchpoints", expanded=False):
            st.dataframe(
                top_donors.rename(columns={
                    "donor": "Donor", "touchpoints": "Touchpoints",
                    "types": "Engagement types",
                }),
                width='stretch', hide_index=True,
            )

    # ─── Team activity, partners, financial mix, conversion (consolidated) ────
    # These used to live in a standalone "Section 6"; consolidated under
    # Section 4 since they all share the umbrella theme of WHO does the work
    # and WHO we do it with. Counts include manual + Excel-migration rows
    # since auto-scanned rows have most of these fields blank.
    if rfps_all.empty:
        st.info("No RFPs in the database yet — team / partner charts will populate "
                "as data lands.")
    else:
        activity_rows = rfps_all[~rfps_all["is_duplicate"]].copy()

        # ───────────── Submissions by team member ─────────────────────────────
        # REMOVED 2026-06-05: chart + leaderboard moved to Section 1, directly
        # under "RFPs discovered by member" — same data presented twice was
        # redundant. The leaderboard expander now sits inline with the
        # discovery chart, and Section 4 jumps straight from KPIs to the
        # stacked Submitted/Unsubmitted views.

        # ───────────── Helpers for stacked Submitted / Unsubmitted bars ───────
        # An RFP counts as "Submitted" when progress_status = Completed
        # (matches the same definition used by Section 5's submission KPI).
        # Anything else (Not Started / In Progress / Discontinued / Missed /
        # blank) goes into "Unsubmitted".
        def _is_submitted(ps_val) -> bool:
            return str(ps_val or "").strip().lower() == "completed"

        def _stacked_chart_df(name_value_pairs: pd.DataFrame,
                              name_col: str) -> pd.DataFrame:
            """Build a tidy DataFrame ready for px.bar(barmode='stack').

            Input: a 2-column frame [name_col, is_submitted (bool)] —
            one row per RFP-person mention.
            Output: long-form [name, Status, count] with two rows per
            person (Submitted + Unsubmitted), suitable for stacked plotting.
            """
            if name_value_pairs.empty:
                return pd.DataFrame(columns=[name_col, "Status", "RFPs"])
            agg = (
                name_value_pairs
                .assign(Status=name_value_pairs["is_submitted"]
                        .map({True: "Submitted", False: "Unsubmitted"}))
                .groupby([name_col, "Status"]).size().reset_index(name="RFPs")
            )
            # Pre-compute per-person total so we can rank top-15 by total.
            totals = agg.groupby(name_col)["RFPs"].sum().sort_values(ascending=False)
            top_names = totals.head(15).index.tolist()
            agg = agg[agg[name_col].isin(top_names)]
            # Force both stack components to exist for every person — keeps
            # the legend stable and the bars visually consistent.
            all_rows = []
            for n in top_names:
                for s in ("Submitted", "Unsubmitted"):
                    match = agg[(agg[name_col] == n) & (agg["Status"] == s)]
                    all_rows.append({
                        name_col: n, "Status": s,
                        "RFPs": int(match["RFPs"].iloc[0]) if not match.empty else 0,
                    })
            return clean_df(pd.DataFrame(all_rows))
        _STACK_COLORS = {"Submitted": "#10b981", "Unsubmitted": "#cbd5e1"}

        # ───────────── Proposal Leads (stacked) ───────────────────────────────
        # `proposal_lead` is a free-text field that can hold a single name
        # OR a comma-separated list ("Alice, Bob"). Split + explode so each
        # named person gets their own bar; first-name display when unique.
        if _show("s4_leads"):
            st.markdown("##### Proposal Leads")
        if _show("s4_leads") and "proposal_lead" in activity_rows.columns:
            lead_rows = activity_rows[["proposal_lead", "progress_status"]].copy()
            lead_rows["_members"] = lead_rows["proposal_lead"].apply(split_and_normalize_names)
            lead_rows = lead_rows.explode("_members").dropna(subset=["_members"])
            lead_rows = lead_rows[lead_rows["_members"] != ""]
            if not lead_rows.empty:
                disp = first_name_display_map(lead_rows["_members"])
                lead_rows["display"] = lead_rows["_members"].map(disp).fillna(lead_rows["_members"])
                lead_rows["is_submitted"] = lead_rows["progress_status"].apply(_is_submitted)
                stacked = _stacked_chart_df(
                    lead_rows[["display", "is_submitted"]],
                    "display",
                )
                if not stacked.empty:
                    fig_pl = px.bar(
                        stacked, x="RFPs", y="display",
                        color="Status", orientation="h",
                        color_discrete_map=_STACK_COLORS,
                        title="Proposal lead — top 15 by RFP count "
                              "(stacked: Submitted vs Unsubmitted)",
                        text="RFPs",
                        category_orders={"Status": ["Submitted", "Unsubmitted"]},
                    )
                    fig_pl.update_layout(
                        height=max(280, 30 * stacked["display"].nunique() + 100),
                        margin=dict(t=50, b=10), barmode="stack",
                        yaxis={"categoryorder": "total ascending",
                               "title": "Proposal lead"},
                        xaxis={"title": "RFPs"},
                        legend={"orientation": "h", "yanchor": "bottom",
                                "y": 1.02, "xanchor": "right", "x": 1,
                                "font": dict(size=11)},
                    )
                    st.plotly_chart(fig_pl, width='stretch')
                else:
                    st.info("No proposal_lead values recorded yet.")
            else:
                st.info("No proposal_lead values recorded yet.")

        # ───────────── Contributors (stacked) ─────────────────────────────────
        # `contributors` is a Postgres text[] array, but individual list
        # elements can themselves carry comma-separated names from sloppy
        # form submissions. split_and_normalize_names handles both shapes.
        if _show("s4_contrib"):
            st.markdown("##### Contributors")
        if _show("s4_contrib") and "contributors" in activity_rows.columns:
            contribs = activity_rows[["contributors", "progress_status"]].copy()
            contribs["_members"] = contribs["contributors"].apply(split_and_normalize_names)
            contribs = contribs.explode("_members").dropna(subset=["_members"])
            contribs = contribs[contribs["_members"] != ""]
            if not contribs.empty:
                disp = first_name_display_map(contribs["_members"])
                contribs["display"] = contribs["_members"].map(disp).fillna(contribs["_members"])
                contribs["is_submitted"] = contribs["progress_status"].apply(_is_submitted)
                stacked = _stacked_chart_df(
                    contribs[["display", "is_submitted"]],
                    "display",
                )
                if not stacked.empty:
                    fig_ct = px.bar(
                        stacked, x="RFPs", y="display",
                        color="Status", orientation="h",
                        color_discrete_map=_STACK_COLORS,
                        title="Contributor — top 15 by RFPs supported "
                              "(stacked: Submitted vs Unsubmitted)",
                        text="RFPs",
                        category_orders={"Status": ["Submitted", "Unsubmitted"]},
                    )
                    fig_ct.update_layout(
                        height=max(280, 30 * stacked["display"].nunique() + 100),
                        margin=dict(t=50, b=10), barmode="stack",
                        yaxis={"categoryorder": "total ascending",
                               "title": "Contributor"},
                        xaxis={"title": "RFP contributions"},
                        legend={"orientation": "h", "yanchor": "bottom",
                                "y": 1.02, "xanchor": "right", "x": 1,
                                "font": dict(size=11)},
                    )
                    st.plotly_chart(fig_ct, width='stretch')
                else:
                    st.info("No contributors recorded yet.")
            else:
                st.info("No contributors recorded yet.")

        # ───────────── Lead & Sub Applicant partners ──────────────────────────
        if _show("s4_partners"):
            st.markdown("##### Lead & Sub Applicant partners")
        ap_l, ap_r = st.columns(2)
        for col_name, col_label, container in [
            ("lead_applicant", "Lead applicant", ap_l),
            ("sub_applicant",  "Sub applicant",  ap_r),
        ]:
            if _show("s4_partners") and col_name in activity_rows.columns:
                counts = (
                    activity_rows[col_name].dropna()
                    .pipe(lambda s: s[s.astype(str).str.strip() != ""])
                    .value_counts().head(10)
                    .reset_index()
                    .rename(columns={col_name: col_label, "count": "RFPs"})
                )
                with container:
                    if not counts.empty:
                        fig = px.bar(
                            counts, x="RFPs", y=col_label, orientation="h",
                            title=f"{col_label} — top 10", text="RFPs",
                        )
                        fig.update_layout(
                            height=max(250, 30 * len(counts) + 60),
                            margin=dict(t=40, b=10),
                            yaxis={"categoryorder": "total ascending"},
                        )
                        st.plotly_chart(fig, width='stretch')
                    else:
                        st.info(f"No {col_label.lower()} values recorded yet.")

        # Progress status chart relocated to Section 2 (Insights) — same
        # narrative beat as the eligibility funnel. Not re-rendered here.

        # The Requested-vs-Secured scatter + Conversion rates blocks
        # relocated to Section 5 (Our Results) — that's the natural home for
        # outcome-shaped metrics. The Search → Submission cycle time block
        # relocated to Section 1 (Search Activity) — Search Date is the
        # anchor, so it fits the search-narrative beat.

    st.divider()


# ===========================================================================
# SECTION 5 — Our Results
# ===========================================================================
if _show_sec("5"):
    st.subheader("5 · Our Results — Proposals Submitted & Grants Secured")
    st.caption(
        "Bottom line across ALL stored RFPs — auto-scanned + manually "
        "submitted + Excel migration rows. Counts here ignore the period "
        "filter for the same reason as Section 2 (Excel rows often have NULL "
        "search_date and would otherwise be invisible). The cumulative chart "
        "below DOES respect the period + 'View by' selectors so you can "
        "narrate the trend."
    )

    if rfps_all.empty:
        st.info("No RFPs in the database yet.")
    else:
        unique = rfps_all[~rfps_all["is_duplicate"]]
        # All-time submitted / approved — Section 5 KPIs reflect current
        # pipeline state across every stored RFP (regardless of when discovered).
        submitted_period = unique[unique["progress_status"].fillna("").str.lower() == "completed"]
        approved_period = unique[unique["donor_decision"].fillna("").str.lower() == "approved"]

        n_submitted = int(len(submitted_period))
        n_approved = int(len(approved_period))
        win_rate = (n_approved / n_submitted * 100) if n_submitted > 0 else 0.0

        # --- FX conversion to USD ---
        # `amount_secured` pairs with `currency_secured`; `estimated_value`
        # pairs with `currency`. Each row converts at its own rate via
        # dropdowns.usd_rate(), so a mixed-currency pipeline rolls up cleanly.
        if "amount_secured" in approved_period.columns and not approved_period.empty:
            approved_usd = _series_to_usd(
                approved_period["amount_secured"],
                approved_period.get("currency_secured", pd.Series(dtype=str)),
            )
            amt_secured = float(approved_usd.sum())
            # Count rows that have a non-zero amount but no usable currency
            # — these silently default to USD and skew the rollup.
            if not approved_period.empty:
                no_cur = approved_period[
                    approved_period["amount_secured"].fillna(0) > 0
                ].get("currency_secured", pd.Series(dtype=str)).fillna("").str.strip()
                n_missing_secured_cur = int((no_cur == "").sum())
            else:
                n_missing_secured_cur = 0
        else:
            approved_usd = pd.Series(dtype=float)
            amt_secured = 0.0
            n_missing_secured_cur = 0

        proceed_track = unique[unique["decision"].fillna("").str.lower()
                               .str.startswith("proceed")]
        if not proceed_track.empty:
            pipeline_usd = _series_to_usd(
                proceed_track["call_award_value"],
                proceed_track.get("currency", pd.Series(dtype=str)),
            )
            amt_pipeline = float(pipeline_usd.sum())
            no_pipe_cur = proceed_track[
                proceed_track["call_award_value"].fillna(0) > 0
            ].get("currency", pd.Series(dtype=str)).fillna("").str.strip()
            n_missing_pipe_cur = int((no_pipe_cur == "").sum())
        else:
            amt_pipeline = 0.0
            n_missing_pipe_cur = 0

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Submitted", n_submitted,
                  help="RFPs where progress_status = Completed within the period.")
        k2.metric("Approved", n_approved,
                  help="RFPs where donor_decision = Approved within the period.")
        k3.metric("Win rate", f"{win_rate:.1f}%" if n_submitted else "—",
                  help="Approved ÷ Submitted (period). Shows '—' with no submissions.")
        k4.metric("USD secured", f"${amt_secured:,.0f}",
                  help="Sum of amount_secured for approved RFPs in period, "
                       "converted row-by-row to USD via the FX rates in "
                       "Admin → Settings (or config/dropdowns.yaml fallback). "
                       "Rows missing a currency are treated as USD.")

        if n_missing_secured_cur or n_missing_pipe_cur:
            st.warning(
                f"⚠ {n_missing_secured_cur} approved row(s) and "
                f"{n_missing_pipe_cur} pipeline row(s) have an amount but no "
                "currency specified — they were treated as USD for this rollup. "
                "Fix in Admin → Data → RFPs or update the FX table in "
                "Admin → Settings.",
                icon="💱",
            )

        # Cumulative USD-secured — bucketed and reindexed so empty buckets stay flat
        if _show("s5_cumusd") and not approved_period.empty and "amount_secured" in approved_period.columns:
            ts = approved_period.dropna(subset=["date_of_approval", "amount_secured"]).copy()
            if not ts.empty:
                ts["date_of_approval"] = pd.to_datetime(ts["date_of_approval"])
                ts["_usd"] = _series_to_usd(
                    ts["amount_secured"],
                    ts.get("currency_secured", pd.Series(dtype=str)),
                )
                agg_df = _bucketed_sum(ts["date_of_approval"], ts["_usd"], "secured")
                agg_df["cumulative"] = agg_df["secured"].cumsum()
                fig = px.area(agg_df, x="bucket", y="cumulative",
                              title=f"Cumulative USD secured ({_period_label_str}, {bucket_mode.lower()})",
                              labels={"bucket": _bucket_label(bucket_mode),
                                      "cumulative": "USD (cumulative)"})
                fig.update_layout(height=280, margin=dict(t=40, b=10),
                                  xaxis=_fmt_bucket_ticks(bucket_mode))
                st.plotly_chart(fig, width='stretch')

        if _show("s5_grants") and not grants.empty:
            with st.expander("Active grants pipeline (all-time)", expanded=False):
                gcols = [c for c in
                         ["grant_id", "donor_title", "status", "award_date",
                          "end_date", "report_due_date", "owner"]
                         if c in grants.columns]
                st.dataframe(
                    grants[gcols].rename(columns={
                        "grant_id": "Grant ID", "donor_title": "Donor / title",
                        "status": "Status", "award_date": "Awarded",
                        "end_date": "Ends", "report_due_date": "Next report",
                        "owner": "Owner",
                    }),
                    width='stretch', hide_index=True,
                )

        st.markdown(
            f"**Pipeline value (Proceed track):** ${amt_pipeline:,.0f} USD across "
            f"{int((unique['decision'].fillna('').str.lower().str.startswith('proceed')).sum())} "
            f"RFPs — sum of `estimated_value` converted row-by-row from each "
            f"RFP's native currency."
        )

        # ───────────── Amount Requested vs Amount Secured (relocated from §4) ─
        # Lives in Section 5 because Requested vs Secured IS the outcome
        # story — what we asked for vs what we got. Scatter shows per-RFP
        # detail; the three KPI tiles roll up the same data in aggregate.
        if _show("s5_reqsec"):
            st.markdown("##### Amount Requested vs Amount Secured (USD)")
        if _show("s5_reqsec") and {"amount_requested", "amount_secured"} <= set(unique.columns):
            usd_view = unique.copy()
            usd_view["req_usd"] = _series_to_usd(
                usd_view["amount_requested"],
                usd_view.get("currency", pd.Series(dtype=str)),
            )
            usd_view["sec_usd"] = _series_to_usd(
                usd_view["amount_secured"],
                usd_view.get("currency_secured", pd.Series(dtype=str)),
            )
            scatter_df = usd_view[(usd_view["req_usd"] > 0) | (usd_view["sec_usd"] > 0)][
                ["opportunity_title", "funding_agency", "req_usd", "sec_usd",
                 "progress_status", "donor_decision"]
            ].copy()
            if not scatter_df.empty:
                fig_amt = px.scatter(
                    scatter_df, x="req_usd", y="sec_usd",
                    hover_data=["opportunity_title", "funding_agency",
                                "progress_status", "donor_decision"],
                    title="Requested vs Secured — every RFP with a value",
                    labels={"req_usd": "Amount requested (USD)",
                            "sec_usd": "Amount secured (USD)"},
                )
                # Add y = x dashed reference line so "full ask received" is visible.
                mx = max(scatter_df["req_usd"].max(), scatter_df["sec_usd"].max())
                fig_amt.add_shape(type="line", line=dict(dash="dash", color="#94a3b8"),
                                  x0=0, y0=0, x1=mx, y1=mx)
                fig_amt.update_layout(height=400, margin=dict(t=40, b=10))
                st.plotly_chart(fig_amt, width='stretch')

                t1, t2, t3 = st.columns(3)
                t1.metric("Total Requested (USD)", f"${scatter_df['req_usd'].sum():,.0f}")
                t2.metric("Total Secured (USD)",  f"${scatter_df['sec_usd'].sum():,.0f}")
                pct = (scatter_df["sec_usd"].sum() / scatter_df["req_usd"].sum() * 100
                       if scatter_df["req_usd"].sum() > 0 else 0)
                t3.metric("Secured ÷ Requested", f"{pct:.1f}%")
            else:
                st.info("No RFPs with monetary amounts recorded yet.")

        # ───────────── Conversion rates (relocated from §4) ───────────────────
        # The actual rates that quantify the funnel. Belongs with Outcomes
        # because the headline question — "of the RFPs we said we'd pursue,
        # how many did we actually submit, and how many won?" — IS the
        # outcomes question, not a team-activity question.
        if _show("s5_conv"):
            st.markdown("##### Conversion rates")
        proceeded = unique[
            unique["decision"].fillna("").str.lower().str.startswith("proceed")
        ]
        submitted_conv = proceeded[
            proceeded["progress_status"].fillna("").str.lower() == "completed"
        ]
        approved_conv = submitted_conv[
            submitted_conv["donor_decision"].fillna("").str.lower() == "approved"
        ]
        conv_rate = (len(submitted_conv) / len(proceeded) * 100) if len(proceeded) > 0 else 0
        win_rate_conv = (len(approved_conv) / len(submitted_conv) * 100) if len(submitted_conv) > 0 else 0
        if _show("s5_conv"):
            cv1, cv2, cv3 = st.columns(3)
            cv1.metric("Proceeded → Submitted", f"{conv_rate:.1f}%",
                       help="Of RFPs we decided to Proceed on, how many got a proposal out the door.")
            cv2.metric("Submitted → Approved", f"{win_rate_conv:.1f}%",
                       help="Win rate among submitted proposals.")
            cv3.metric("End-to-end (Proceed → Approved)",
                       f"{(len(approved_conv)/len(proceeded)*100):.1f}%" if len(proceeded) else "—")

    st.divider()


# NOTE: A standalone "Section 6 · Team & Partnership Activity" used to
# live here. It has been folded into Section 4 (which was renamed from
# "Meetings & Engagements" to "Team & Partnership Activity"). Nothing
# renders here anymore — moving on to the Footer.

st.divider()


st.success(
    f"✅ **End of report** — {len(_selected_items)} of {len(_ALL_KEYS)} "
    f"metric blocks shown for **{_period_label_str}**. Export with "
    f"**Print / PDF** or **Excel** at the top of the page."
)


# ===========================================================================
# Footer — org context + generated timestamp
# ===========================================================================
_website = _org.get("org_website")
foot_bits = [f"**{_org_name}**"]
if _org_country:
    foot_bits.append(_org_country)
if _website:
    foot_bits.append(f"[{_website}]({_website})")
with st.container(key="report_footer"):
    st.caption(
        " · ".join(foot_bits)
        + f" · Report generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        + (f" · Report id `{_url_rid}`" if _url_rid else "")
    )
