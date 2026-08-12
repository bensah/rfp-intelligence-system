"""Report view — KPI dashboard of the full RFPIS pipeline.

Story arc, top → bottom:
  1. Scan activity     — scanner output (top of funnel)
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
  * **Export Report** builds a shareable PDF from the collected Document — cover page, one
    section per page, charts laid out at page width. It does NOT print this page: every
    Streamlit ancestor is a flex container, where Chrome will not honour `break-inside`, so
    charts split across page boundaries no matter what CSS is added. See core.report_pdf.
  * **Export Data** writes every section's underlying data to a single .xlsx workbook (one
    sheet per section + a Summary sheet on top).

There is no Print button. The @media print CSS and the beforeprint hook remain, so Ctrl+P
still fits the charts for anyone who reaches for it, but it cannot produce a shareable file.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

from core import chart_theme as _theme
from core import report_pdf as _report_pdf
from core import dropdowns, partner_names, report_snapshots, settings
# Plotly Express BAKES a colour into the trace, so layout.colorway is ignored and every
# chart without an explicit `color=` came out Plotly default blue — which is why the
# partner charts stayed blue after the palette change. Setting the Express default is the
# one place that fixes all of them.
px.defaults.color_discrete_sequence = _theme.ramp(6)

from core.member_names import (first_name_display_map, normalize_member_name,
                              split_and_normalize_names)
from core.records import clean_df
from db.supabase_client import get_client


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


# ---------------------------------------------------------------------------
# Single source of truth for "submitted to a donor" — MUST match the Applied
# Funding + Summary pages, else the Report won't reconcile with them. A row is
# submitted when Progress = Completed OR it carries a real donor decision
# (Approved / Under Review / Not Approved). Using progress=Completed alone
# undercounts and lets Approved exceed Submitted (win-rate > 100%).
# ---------------------------------------------------------------------------
_SUBMITTED_DECISIONS = {"approved", "under review", "not approved"}


def _submitted_mask(df: pd.DataFrame) -> pd.Series:
    """Boolean Series: True where the RFP has been submitted to a donor."""
    if df is None or df.empty:
        return pd.Series([], dtype=bool)
    ps = df.get("progress_status")
    dd = df.get("donor_decision")
    ps = (ps if ps is not None else pd.Series("", index=df.index)).fillna("").astype(str).str.strip().str.lower()
    dd = (dd if dd is not None else pd.Series("", index=df.index)).fillna("").astype(str).str.strip().str.lower()
    return ps.eq("completed") | dd.isin(_SUBMITTED_DECISIONS)


def _scope_key() -> str:
    """Cache-key discriminator for the @st.cache_data loaders below. Their cache is
    PROCESS-GLOBAL (shared across all sessions), but the rows they load are tenant-scoped
    by the get_client() wrapper — so without the tenant in the key one tenant's report
    would be served to another. Mirror the wrapper's own scope: a tenant id for a scoped
    tenant user, or 'all' for super_user / single-tenant (who see everything).

    THE PARAMETER MUST NOT BE NAMED WITH A LEADING UNDERSCORE. Streamlit excludes
    underscore-prefixed arguments from a cache key, so `def _load_rfps(_scope)` cached ONE
    frame for every tenant and served whichever tenant rendered first in the process to all
    the others. The safeguard this docstring describes was defeated by the parameter's name:
    the report showed another tenant's rows (161 auto-scan rows across two months where the
    tenant's own data spanned seven months and thirteen people). Verified against Streamlit:
    changing an underscore-prefixed argument does not re-execute the function."""
    from core.cache_scope import scope_key as _shared_scope_key
    return _shared_scope_key()


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
        /* PORTRAIT with real margins, so nothing sits in the unprintable edge. Orientation
           only — the paper SIZE is left to the user's print dialog, since forcing A4 would
           be wrong on Letter and vice versa. */
        @page { size: portrait; margin: 12mm; }

        /* Side-by-side columns do not fit a portrait page: two half-width charts each get
           ~340px, and the funnels were being cut off mid-plot. Stacking gives every chart
           the FULL page width, which is what makes the JS scaling below sufficient. */
        [data-testid="stHorizontalBlock"] { display: block !important; }
        [data-testid="stColumn"] {
          width: 100% !important; flex: none !important;
          display: block !important; margin-bottom: 0.4rem !important;
        }
        /* ...except KPI tile rows, which are small and read better left as a row. */
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"]) {
          display: flex !important;
        }
        [data-testid="stHorizontalBlock"]:has([data-testid="stMetric"])
          [data-testid="stColumn"] { width: auto !important; flex: 1 1 0 !important; }

        /* Nothing may exceed the page box. Plotly draws into a fixed-size SVG, so without
           this a wide chart silently extends past the paper edge. */
        .block-container, .stPlotlyChart, [data-testid="stPlotlyChart"],
        .js-plotly-plot, .plot-container, .svg-container, .main-svg,
        [data-testid="stDataFrame"], table {
          max-width: 100% !important;
        }
        [data-testid="stDataFrame"] { overflow: visible !important; }

        /* A heading must not be the last thing on a page, and must never be drawn over the
           chart it labels - the "Lead & Sub Applicant partners" overlap. h4/h5/h6 were
           missing here, which is why subsection headings behaved differently from h1-h3. */
        h1, h2, h3, h4, h5, h6 {
          page-break-after: avoid; break-after: avoid-page;
          page-break-inside: avoid;
        }
        .stPlotlyChart, [data-testid="stPlotlyChart"], [data-testid="stMetric"],
        [data-testid="stDataFrame"], [data-testid="stTable"],
        /* The bordered frame around each chart. This selector was MISSING, so a frame could
           split across a page break — the chart on one page and its value labels on the next,
           which is what the overlapping / stray-numbers pages were. */
        [data-testid="stVerticalBlockBorderWrapper"] {
          page-break-inside: avoid; break-inside: avoid;
        }
        /* Keep a subsection heading with the frame that follows it. */
        h4 + div, h5 + div, h6 + div { page-break-before: avoid; break-before: avoid; }
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
def _load_scan_logs(scope: str, start_iso: str | None, end_iso: str | None) -> pd.DataFrame:
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
        # Multi-tenant: a tenant's Activity Report reflects only ITS OWN runs (eligibility
        # / "Find my matches" screening). The system-wide discovery crawl (cron, tenant_id
        # NULL) is excluded here — it's shared, not this tenant's activity. Super_user and
        # single-tenant see everything. No-op before migration 074 (no tenant_id column).
        try:
            import streamlit as _st
            from auth.tenant_context import multitenant_enabled, current_tenant_id
            from core import permissions as _perms
            _u = _st.session_state.get("app_user") or {}
            _tid = current_tenant_id()
            if (multitenant_enabled() and _tid and not _perms.is_super_user(_u)
                    and "tenant_id" in df.columns):
                df = df[df["tenant_id"].astype(str) == str(_tid)]
        except Exception:
            pass
    return df


@st.cache_data(ttl=120)
def _load_rfps(scope: str) -> pd.DataFrame:
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
        "uid,source,opportunity_title,brief_description,funding_agency,"
        "submitted_at,search_date,"
        "submitted_by,submitted_by_email,"
        "call_submission_deadline,date_completed,decision_date,date_of_approval,"
        "decision,auto_recommendation,donor_decision,progress_status,stage,"
        "alignment_score,call_award_value,currency,"
        "amount_requested,amount_secured,currency_secured,submissions,"
        "call_geographic_scope,call_domain_areas,decision_overridden_by,"
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
def _load_meetings(scope: str, start_iso: str | None, end_iso: str | None) -> pd.DataFrame:
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
def _load_engagements(scope: str, start_iso: str | None, end_iso: str | None) -> pd.DataFrame:
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
def _load_grants(scope: str) -> pd.DataFrame:
    res = sb.table("applied_funding").select("*").limit(10000).execute()
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
# The first element is a STABLE ID, not the display number. Saved reports persist these ids
# (and the `sN_` metric keys), so a shared or refreshed report restores the sections it was
# generated with — renumbering them to match a new running order would silently change what an
# existing report shows. Display order is this list's order; the display number is in the label.
_REPORT_SECTIONS = [
    ("1", "1 · Scan activity", [
        ("s1_discovery",  "Funding discovered by member"),
        ("s1_donor",      "Funding by donor (top 15)"),
        ("s1_keywords",   "Search keyword cloud"),
        ("s1_kw_success", "Keywords driving success"),
        ("s1_sources",    "Top sources by yield"),
        ("s1_cycle",      "Search → Submission cycle time"),
    ]),
    ("4", "2 · Team & partners", [
        ("s4_eng_ts",    "Donor engagements over time"),
        ("s4_topdonors", "Top donors by touchpoints"),
        ("s4_leads",     "Proposal leads"),
        ("s4_contrib",   "Contributors"),
        ("s4_partners",  "Lead & sub applicant partners"),
    ]),
    ("2", "3 · Insights — status & eligibility funnel", [
        ("s2_funnel",   "Conversion funnel"),
        ("s2_progress", "Progress status"),
    ]),
    ("3", "4 · Reviews & decisions", [
        ("s3_decdist",  "Decision distribution"),
        ("s3_dectime",  "Decisions over time"),
        ("s3_autorec",  "Auto-recommendation vs decision"),
        ("s3_donordec", "Donor decisions"),
    ]),
    ("5", "5 · Our results", [
        ("s5_cumusd", "Cumulative USD secured"),
        ("s5_grants", "Applied Funding pipeline"),
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
# A snapshot records the metrics that were ON, not which metrics EXISTED. So every metric added
# to the report after a snapshot was saved was absent from it, and reopening that snapshot came
# back with the checkbox unticked and the chart quietly gone. New snapshots also record their key
# universe, which makes the distinction exact from here on.
_items_saved = _sel("items")
_restored_items = report_snapshots.restore_items(_items_saved, _ALL_KEYS, _sel("all_items"))

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


# ── PDF DOCUMENT COLLECTION ───────────────────────────────────────────────────────────
# The page collects what it renders into a Document, and the PDF is built from THAT rather
# than by printing the page. Printing cannot work: every Streamlit ancestor is a flex
# container, and Chrome will not honour `break-inside` inside flexbox, so charts split across
# page boundaries no matter what CSS is added. See core.report_pdf.
#
# Collecting here also means the aggregations are not duplicated — one source of truth for
# the numbers, two presentations of them.
#
# THE DOCUMENT LIVES IN core.report_pdf, NOT HERE. Streamlit re-executes this page with a
# fresh namespace on every rerun, so a hook installed once cannot close over a document
# defined here: it would keep appending to the FIRST run's object while the page builds and
# renders a new one. That is precisely what happened — the metric hook was installed on the
# first run and every later PDF came out with no KPI cards at all, because the tiles were
# going into a dead Document. The hooks ask `report_pdf.current()` for the live one on every
# call instead.
_PDF_DOC = _report_pdf.new_document()


def _pdf_hook_streamlit() -> None:
    """Record KPI tiles and tables into the live document as well as rendering them.

    Patched at the DeltaGenerator level because both are called on column and container
    handles (`k1.metric(...)`, `expander.dataframe(...)`), not on `st` — so wrapping the `st.`
    functions alone would miss nearly all of them, and adding a call beside forty existing
    ones would be forty chances to miss one.

    Guarded throughout: if Streamlit's internals move, the page still renders and only the
    PDF loses content.
    """
    try:
        from streamlit.delta_generator import DeltaGenerator
    except Exception:
        return
    if getattr(DeltaGenerator, "_rfpis_pdf_hooked", False):
        return

    _orig_metric = DeltaGenerator.metric
    _orig_df = getattr(DeltaGenerator, "dataframe", None)
    _orig_table = getattr(DeltaGenerator, "table", None)

    def _metric(self, label, value=None, *a, **kw):
        try:
            doc = _report_pdf.current()
            if doc is not None:
                doc.metric(label, value)
        except Exception:
            pass
        return _orig_metric(self, label, value, *a, **kw)

    def _wrap_table(orig):
        def _inner(self, data=None, *a, **kw):
            try:
                doc = _report_pdf.current()
                if doc is not None and hasattr(data, "columns"):
                    doc.table(data)
            except Exception:
                pass
            return orig(self, data, *a, **kw)
        return _inner

    DeltaGenerator.metric = _metric
    if _orig_df is not None:
        DeltaGenerator.dataframe = _wrap_table(_orig_df)
    if _orig_table is not None:
        DeltaGenerator.table = _wrap_table(_orig_table)
    DeltaGenerator._rfpis_pdf_hooked = True


_pdf_hook_streamlit()


# Values that are not a programme area. "Unspecified Program Areas" is the extractor's
# placeholder for "we could not tell", and drawing it in a focus-area cloud presents an absence
# of information as a focus.
_AREA_NON_VALUES = frozenset({
    "n/a", "na", "none", "other", "unspecified", "unspecified program areas",
    "unspecified programme areas", "not specified", "tbd",
})


def _canonical_area(label: str) -> str:
    """Snap a stored area onto the taxonomy's own spelling where it unambiguously matches.

    The pipeline stores "Digital Health" and "Digital Health (+AI)" for the same taxonomy
    sub-area, so the cloud drew one area twice at half its weight each. Matching is EXACT first,
    then a prefix match accepted only when exactly one sub-area matches — an ambiguous prefix is
    left alone rather than guessed at, and an area the taxonomy has never heard of still appears
    under its own name.
    """
    try:
        from core import program_area_classifier as _pa
        subs = [s for v in _pa.TAXONOMY.values() for s in v]
    except Exception:
        return label
    low = label.lower()
    for s in subs:
        if s.lower() == low:
            return s
    hits = [s for s in subs if s.lower().startswith(low)]
    return hits[0] if len(hits) == 1 else label


def _programme_area_freq(rows) -> dict[str, int]:
    """{programme area: number of calls} from the `call_domain_areas` a row actually carries.

    NOT from titles and briefs, and NOT from the scan vocabulary. Those describe what we went
    looking for; this describes what the team decided to pursue, which is the only version worth
    putting in front of a reader.

    The stored values carry an internal grouping prefix, and inconsistently — the same area
    appears as "Cross-cutting - Digital Health (+AI)", "Cross-cutting  - Digital Health (+AI)"
    (two spaces) and "Cross-cutting Expert Areas - Digital Health". Left as-is they would draw
    as three separate areas at one count each instead of one area at three. Stripping the prefix
    and collapsing whitespace merges them.
    """
    import collections

    freq: collections.Counter = collections.Counter()
    if rows is None or getattr(rows, "empty", True) or "call_domain_areas" not in rows.columns:
        return {}
    for raw in rows["call_domain_areas"]:
        items = raw
        if isinstance(items, str):
            txt = items.strip()
            if txt.startswith("["):
                try:
                    items = json.loads(txt)
                except (ValueError, TypeError):
                    items = [txt]
            else:
                items = [p for p in txt.split(",")] if txt else []
        if not isinstance(items, (list, tuple, set)):
            items = [items] if items else []
        seen_this_row = set()
        for item in items:
            label = re.sub(r"\s+", " ", str(item or "")).strip()
            if not label:
                continue
            # "<Category> - <Sub-area>" -> "<Sub-area>". Split on the LAST separator so an area
            # whose own name contains a dash survives.
            if " - " in label:
                label = label.rsplit(" - ", 1)[-1].strip()
            if not label or label.lower() in _AREA_NON_VALUES:
                continue
            label = _canonical_area(label)
            if not label:
                continue
            # One call counts once for an area even if it lists it twice.
            if label.lower() in seen_this_row:
                continue
            seen_this_row.add(label.lower())
            freq[label] += 1
    return dict(freq)


def _period_slug() -> str:
    """A short, stable token for the selected period — "ytd", "2026", "last90d", "alltime".

    Goes in the export filename, so two reports downloaded on the same day for different periods
    no longer land as "…(1).xlsx".
    """
    mode = str(period_mode or "").strip().lower()
    if "year to date" in mode or mode == "ytd":
        return "ytd"
    if "last 90" in mode:
        return "last90d"
    if "last 12" in mode:
        return "last12m"
    if "all time" in mode:
        return "alltime"
    if "month" in mode:
        return f"{str(month_override or '').lower()[:3] or 'month'}{int(year_override)}"
    if "year" in mode:
        return str(int(year_override))
    return (mode.replace(" ", "") or "period")


def _slug(text: str, limit: int = 28) -> str:
    """Filename-safe token: keeps letters, digits and dashes, drops everything else."""
    out = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-")
    return (out[:limit].strip("-") or "org")


def _export_filename(ext: str) -> str:
    """RFPIS_<period>_report_<tenant>_<report-id>_<year>.<ext>

    The old name was the same for every tenant and every period, so downloads collided and a
    file on disk could not be traced back to the report that produced it.
    """
    parts = ["RFPIS", _period_slug(), "report",
             _slug(_org.get("org_short") or _org_name),
             _slug(_url_rid or "unsaved", 20), str(int(year_override))]
    return "_".join(p for p in parts if p) + f".{ext}"


def _cadence_word() -> str:
    """"Monthly" / "Quarterly" / … from the View-by selection.

    The report is named after the cadence it was cut at, because that is what makes two exports
    of the same period distinguishable to a reader who did not generate them.
    """
    m = str(bucket_mode or "").strip().lower()
    return {"weekly": "Weekly", "monthly": "Monthly", "quarterly": "Quarterly",
            "semestrial": "Semi-annual", "annually": "Annual"}.get(m, m.title() or "Periodic")


def _report_name() -> str:
    """"<Organisation> · Fund-raising Monthly Activity Report"."""
    return f"{_org_name} · Fund-raising {_cadence_word()} Activity Report"


def _period_phrase() -> str:
    """The period as a reader says it: "Year-to-date 2026", not "YTD 2026".

    The view-by word is deliberately NOT repeated here — it is already in the report's name, and
    having both made the subtitle read like a settings dump.
    """
    label = str(_period_label_str or "").strip()
    low = label.lower()
    if low.startswith("ytd"):
        return "Year-to-date " + label.split()[-1]
    if low.startswith("last 90"):
        return "Last 90 days"
    if low.startswith("last 12"):
        return "Last 12 months"
    if low.startswith("all"):
        return "All time"
    return label


def _h5(text: str) -> None:
    """A subsection heading, on the page AND in the exported document.

    Explicit rather than hooked. The table hook taught the lesson: `st.dataframe` does not route
    through the `DeltaGenerator` attribute we patched, so tables silently never reached the PDF
    while the hook reported itself installed. Nine call sites are cheap; a hook that lies is not.
    """
    # The hashes are built rather than written literally: a bulk rewrite of every
    # `st.markdown("##### …")` call site into `_h5(…)` rewrote the one INSIDE this function too,
    # and it called itself until the stack ran out.
    st.markdown(("#" * 5) + " " + str(text))
    try:
        doc = _report_pdf.current()
        if doc is not None:
            doc.sub(text)
    except Exception:
        pass


def _table(df, title: str = "", **kw) -> None:
    """A dataframe on the page, and the same rows in the exported document.

    The title goes ABOVE the table (a table is labelled above; a figure below), and it is passed
    explicitly because a Streamlit dataframe carries no title of its own to lift out.
    """
    try:
        doc = _report_pdf.current()
        if doc is not None:
            doc.table(df, title=title)
    except Exception:
        pass
    st.dataframe(df, **kw)


def _boxed(fig, **kw):
    """Render a chart inside its own bordered frame.

    Charts used to sit directly on the page background, so with several in a row the eye had
    nothing to separate one from the next — the funnel pair in particular read as one wide
    graphic. The frame also gives print a block it can keep together.

    Every chart also goes through `_theme.style`, which strips Plotly's grey plot area: inside a
    bordered container that reads as a box within a box.
    """
    _theme.style(fig)
    try:
        _doc = _report_pdf.current()
        if _doc is not None:
            _doc.chart(fig)          # same figure object the page shows
    except Exception:
        pass
    with st.container(border=True):
        st.plotly_chart(fig, width="stretch", **kw)


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
            # The metrics that EXISTED when this was saved. Without it, a metric added later
            # cannot be told apart from one deliberately switched off, and restoring has to
            # guess — see report_snapshots.restore_items.
            "all_items": sorted(_ALL_KEYS),
            "all_secs": sorted(_ALL_SEC_IDS),
        },
        updated_by=(st.session_state.get("app_user") or {}).get("email"),
    )
    st.query_params.clear()
    st.query_params["r"] = _new_rid
    st.session_state["report_generated"] = True
    # A new report means the built PDF describes the previous one. Drop it so the action row
    # offers Export Report again rather than a download of something stale.
    st.session_state.pop("_rfpis_pdf_bytes", None)
    st.session_state.pop("_rfpis_pdf_name", None)
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
    _sk = _scope_key()   # tenant discriminator so cached rows never cross tenants
    scans = _load_scan_logs(_sk, _s_iso, _e_iso)
    rfps_all = _load_rfps(_sk)
    meetings = _load_meetings(_sk, _s_iso, _e_iso)
    engagements = _load_engagements(_sk, _s_iso, _e_iso)
    grants = _load_grants(_sk)


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
# The hook component still needs a home, but it draws nothing (height 0), so it goes
# in the tip column rather than taking a slot of its own.
ac_tip, ac_pdf, ac_excel = st.columns([5.4, 1.8, 1.6])


def _safe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    """Sanitise a DataFrame for openpyxl writing.

    Excel can't store:
      * timezone-aware datetimes  — pandas raises ValueError. Postgres
        `timestamptz` round-trips through pandas as tz-aware Timestamps,
        which is most of the failures we see.
      * inf / -inf                — openpyxl raises ValueError.
      * lists / dicts             — comes from Postgres array columns
        (call_geographic_scope, call_domain_areas). JSON-stringify them.
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


@st.cache_data(ttl=120, show_spinner=False)
def _excel_bytes(scope: str, start_iso: str | None, end_iso: str | None) -> bytes:
    """`_build_excel_export`, memoised on the same key the data loaders use.

    `st.download_button` needs its bytes up front, so the whole workbook was rebuilt on EVERY
    rerun whether or not anyone downloaded it — about 0.9s of a ~3s warm render. The key is
    (scope, period), which is exactly what determines the frames the workbook is built from, and
    the TTL matches the loaders', so the file cannot be staler than the page showing it.
    """
    return _build_excel_export()


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
            ("Applied Funding",       int(len(grants))),
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
                writer, sheet_name="Applied Funding", index=False)
    buf.seek(0)
    return buf.getvalue()


ac_tip.caption(
    "💡 **Export Report** builds a shareable PDF — cover page, one section per page, and every "
    "chart laid out to fit. **Export Data** ships each section's underlying rows as a separate "
    "sheet in one workbook."
)

ac_excel.download_button(
    "📥 Export Data",
    data=_excel_bytes(_sk, _s_iso, _e_iso),
    file_name=_export_filename("xlsx"),
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    help="Multi-sheet workbook: Summary KPIs + raw data for every section.",
    width='stretch',
)

# NO Print / PDF button. It was the browser's own print dialog, which cannot control page
# breaks in a flexbox layout, cannot size a Plotly chart to paper, and names the file after the
# document title. Export Report replaces it. The @media print CSS and the beforeprint hook below
# stay, so Ctrl+P still produces something sane for anyone who reaches for it out of habit.
#
# "Export Report", not "Build PDF": the user is exporting a report and then downloading it.
# Whether we build it is our concern, not theirs.
#
# ONE SLOT, ONE BUTTON. Export Report and Download PDF are two states of the same control, not
# two controls: once a PDF exists, exporting again does nothing a reader wants, and two buttons
# side by side left the question of which one to press. Generate report clears the PDF, so the
# slot goes back to Export Report — the report has changed, and the old file no longer describes
# it.
#
# The slot also has to be reserved HERE rather than filled at the end of the script: the document
# is only complete once the page has drawn, and rendering the download button at that point put it
# several screens below the button just pressed, which read as "clicking Export Report does
# nothing".
_pdf_slot = ac_pdf.empty()
_pdf_name = _export_filename("pdf")
_pdf_bytes = st.session_state.get("_rfpis_pdf_bytes")

# Rendered at most ONCE per run: a widget key may not be reused, so the run that BUILDS the PDF
# fills the slot at the end instead (see the export block below).
_pdf_rendered = False
if _pdf_bytes:
    _pdf_slot.download_button(
        "⬇ Download PDF",
        data=_pdf_bytes,
        file_name=st.session_state.get("_rfpis_pdf_name") or _pdf_name,
        mime="application/pdf", width="stretch", key="report_pdf_download",
        help=f"{st.session_state.get('_rfpis_pdf_name') or _pdf_name} · "
             f"{len(_pdf_bytes) / 1024:,.0f} KB · Generate report starts a new export.")
    _pdf_rendered = True
elif _pdf_slot.button("📄 Export Report", width="stretch", key="report_pdf_btn",
                      help="Builds a shareable PDF — cover page, one section per page, charts "
                           "laid out to fit. Takes a few seconds; this button then becomes "
                           "Download PDF."):
    st.session_state["_rfpis_make_pdf"] = True
    st.rerun()

# Tenant- and period-specific document title. Chrome stamps document.title into the printed
# page header, so this is what makes the PDF header identify the report rather than the product.
_doc_title = f"{_report_name()} · {_period_phrase()}"

with ac_tip:
    components.html(
        "<script>window.RFPIS_DOC_TITLE = "
        + json.dumps(_doc_title)
        + ";</script>"
        + r"""
        <script>
        // -- Fit Plotly charts to the printed page ------------------------------------
        // Plotly bakes a PIXEL width into its <svg> at render time, measured from the
        // browser window. Printing narrows the page box but does NOT re-render the
        // chart, so a chart laid out at ~1200px was being cut off by a ~700px page.
        //
        // CSS alone cannot fix it: the SVGs carry width/height attributes and NO
        // viewBox, so `width:100%` resizes the viewport and CLIPS the drawing instead
        // of scaling it. Measured in a standalone repro: container 704px, svg still
        // width="1200", document 1488px wide - cut off at about half.
        //
        // So give each SVG a viewBox derived from its natural size, then set its box to
        // the printable width, which scales the whole drawing, text included. This
        // deliberately does NOT call Plotly's own resize API: Streamlit bundles Plotly
        // as a module, so `window.Plotly` is not reliably reachable from here and a fix
        // depending on it would fail silently.
        //
        // The target width is computed from PAPER, not measured: `beforeprint` fires
        // before print layout, so measuring then returns SCREEN widths. 700px ~ 185mm,
        // which fits both A4 and Letter portrait inside 12mm margins.
        var RFPIS_PRINT_W = 700;

        function rfpisDoc() {
          try { return window.parent.document; } catch (e) { return document; }
        }

        function rfpisFitPlots(maxW) {
          var doc = rfpisDoc();
          var jobs = [];
          doc.querySelectorAll('.js-plotly-plot').forEach(function (plot) {
            var cont = plot.querySelector('.svg-container');
            var svgs = plot.querySelectorAll('svg.main-svg');
            if (!cont || !svgs.length) return;
            // Record the natural size ONCE, so afterprint restores the screen layout and a
            // second print does not re-fit an already-fitted chart.
            if (!plot.dataset.rfpisNatW) {
              plot.dataset.rfpisNatW =
                parseFloat(svgs[0].getAttribute('width')) || cont.offsetWidth || 0;
              plot.dataset.rfpisNatH =
                parseFloat(svgs[0].getAttribute('height')) || cont.offsetHeight || 0;
            }
            var natW = parseFloat(plot.dataset.rfpisNatW);
            var natH = parseFloat(plot.dataset.rfpisNatH);
            if (!natW || !natH || maxW / natW >= 1) return;   // already fits - never enlarge

            // PREFERRED: ask Plotly to re-lay the chart out at the page width. Fonts, ticks and
            // legends keep their real sizes and are recomputed for the narrower box.
            //
            // The alternative - giving the SVG a viewBox and shrinking its box - scales EVERYTHING,
            // so an 11px axis label printed at about 5pt. That is what "blurry" was: not
            // resolution (the PDF is vector throughout) but type shrunk past legibility.
            if (window.Plotly && window.Plotly.relayout) {
              try {
                jobs.push(window.Plotly.relayout(plot, {width: maxW}));
                return;
              } catch (e) { /* fall through to the scaling fallback */ }
            }

            // FALLBACK, only when Plotly is unreachable: scale the SVG. Undersized text beats a
            // chart cut off at the page edge.
            var k = maxW / natW;
            var w = Math.floor(natW * k), h = Math.floor(natH * k);
            cont.style.width = w + 'px';  cont.style.height = h + 'px';
            plot.style.width = w + 'px';  plot.style.height = h + 'px';
            svgs.forEach(function (svg) {
              if (!svg.getAttribute('viewBox')) {
                svg.setAttribute('viewBox', '0 0 ' +
                  (svg.getAttribute('width') || natW) + ' ' +
                  (svg.getAttribute('height') || natH));
              }
              svg.setAttribute('width', w); svg.setAttribute('height', h);
              svg.style.width = w + 'px';  svg.style.height = h + 'px';
            });
          });
          // A promise the caller can await, so printing waits for the re-layout to finish.
          return (window.Promise && jobs.length) ? window.Promise.all(jobs)
                                                 : {then: function (f) { f(); }};
        }

        function rfpisRestorePlots() {
          var doc = rfpisDoc();
          doc.querySelectorAll('.js-plotly-plot').forEach(function (plot) {
            var natW = parseFloat(plot.dataset.rfpisNatW);
            var natH = parseFloat(plot.dataset.rfpisNatH);
            if (!natW || !natH) return;
            var cont = plot.querySelector('.svg-container');
            if (cont) { cont.style.width = ''; cont.style.height = ''; }
            plot.style.width = ''; plot.style.height = '';
            plot.querySelectorAll('svg.main-svg').forEach(function (svg) {
              svg.setAttribute('width', natW); svg.setAttribute('height', natH);
              svg.style.width = ''; svg.style.height = '';
            });
            if (window.Plotly && window.Plotly.relayout) {
              try { window.Plotly.relayout(plot, {width: natW}); } catch (e) {}
            }
          });
        }

        // Hook Ctrl+P / the browser menu, not just our button.
        //
        // The listener is INJECTED INTO THE PARENT as its own script element rather
        // than registered from in here.
        //
        // NOTE, and this cost two rounds of "the button does nothing": never write a
        // literal script tag inside inline script text. This comment used to spell one
        // out, and the HTML parser stopped treating the rest as script, so the whole
        // block never executed. The button then rendered (it is plain HTML), had no
        // hover (it is not a Streamlit button) and did nothing at all, because its
        // onclick handler was never defined. Nothing in the page could report it.
        //
        // Streamlit destroys and recreates this iframe on every
        // rerun, so a listener added by `window.parent.addEventListener` from inside the
        // iframe keeps pointing at functions in a torn-down realm - it survives the guard
        // that stops re-registration and then does nothing when the user prints. Injected
        // code lives in the parent's own realm, so it outlives any rerun.
        // The id carries a VERSION. The previous build guarded with
        // `if (getElementById('rfpis-print-hook')) return;`, so once a page had been
        // loaded with an older build, the parent kept that older hook forever and the
        // newer one never installed — the button then posted a message nothing was
        // listening for, and clicking it did nothing at all. Bump this whenever the
        // injected body changes, and remove any earlier hook on the way in.
        var RFPIS_HOOK_ID = 'rfpis-print-hook-v3';

        // The browser tab title, which is ALSO what the print dialog stamps into the page
        // header. It read "RFP Intelligence System - RFPIS" on every tenant's printout —
        // identical across tenants and saying nothing about which report this is. Set from the
        // page so the PDF header names the tenant and the period.
        try {
          if (window.RFPIS_DOC_TITLE) { window.parent.document.title = window.RFPIS_DOC_TITLE; }
        } catch (e) {}

        (function () {
          var pdoc;
          try { pdoc = window.parent.document; } catch (e) { return; }  // cross-origin
          if (pdoc.getElementById(RFPIS_HOOK_ID)) return;               // this version is in
          // Drop hooks from earlier builds so their stale listeners stop firing.
          var stale = pdoc.querySelectorAll('[id^="rfpis-print-hook"]');
          for (var i = 0; i < stale.length; i++) { stale[i].remove(); }
          var el = pdoc.createElement('script');
          el.id = RFPIS_HOOK_ID;
          // Rebuild the two helpers plus their listeners inside the parent. The function
          // bodies are carried across as source text, so what runs there is parent code.
          el.textContent = [
            'window.RFPIS_PRINT_W = ' + RFPIS_PRINT_W + ';',
            'window.rfpisDoc = function () { return document; };',
            'window.rfpisFitPlots = ' + rfpisFitPlots.toString() + ';',
            'window.rfpisRestorePlots = ' + rfpisRestorePlots.toString() + ';',
            'window.addEventListener("beforeprint", function () {',
            '  window.rfpisFitPlots(window.RFPIS_PRINT_W); });',
            'window.addEventListener("afterprint", function () {',
            '  window.rfpisRestorePlots(); });',
            // The button posts up to here, so the print originates in the parent realm.
            // One entry point the button can CALL, so success is observable rather than
            // fired-and-forgotten into a message channel.
            'window.rfpisPrintNow = function () {',
            // Await the fit: Plotly.relayout is asynchronous, and printing before it settles
            // captures the chart at its old width.
            '  var p = window.rfpisFitPlots(window.RFPIS_PRINT_W);',
            '  if (p && p.then) { p.then(function () { window.print(); }); }',
            '  else { window.print(); }',
            '  return true; };',
            'window.addEventListener("message", function (ev) {',
            '  if (!ev.data || ev.data.rfpis !== "print") return;',
            '  window.rfpisPrintNow();',
            '});'
          ].join('\n');
          pdoc.head.appendChild(el);
        })();

        // Ask the PARENT to print itself, rather than reaching across and calling
        // print() from in here.
        //
        // Why: this button lives in a sandboxed iframe. Every step of the old path -
        // touching window.parent, calling its print() from a sandboxed context - is a
        // place a browser may refuse, and the old code's `catch` then fell through to
        // `window.print()`, which prints the IFRAME: a page containing one button.
        // A blank-looking printout is indistinguishable from a dead button, which is
        // the most likely reason this reads as 'not working'.
        //
        // postMessage has no such failure mode. The handler was installed by the
        // injected parent-realm script above, so the print call originates in the
        // parent, where nothing is sandboxed. If the hook is missing we still try the
        // direct route, and only then fall back - now telling the user rather than
        // printing a button.
        </script>
        """,
        # Zero height: this component now only INSTALLS the hook. Nothing is drawn in it, so
        # it cannot occupy space beside the button or intercept a click meant for it.
        height=0,
    )

# ── AT A GLANCE ───────────────────────────────────────────────────────────────────────
# A report that opens on a KPI grid asks the reader to assemble the story themselves. This is
# the story in a sentence or two, computed from the same rows the sections below chart, so it
# cannot drift from them. It leads the PDF as well as the page.
def _headline_summary() -> str:
    """A plain-language summary of where this organisation's pipeline stands.

    EVERY FIGURE HERE COMES FROM THE SAME HELPER THE SECTIONS BELOW USE. The first version did
    its own arithmetic and disagreed with the report it introduces: it counted rows with any
    submissions value (17) where the agreed rule is Progress = Completed × submissions (14), and
    it summed `amount_secured` over every row where section 5 counts it only on donor-Approved
    ones. A summary that contradicts the tables under it is worse than no summary.
    """
    if rfps_all.empty:
        return (f"No funding calls are stored for {_org_name} yet. Run an eligibility scan or "
                f"import the workbook, and this report will fill in.")

    _u = rfps_all[~rfps_all["is_duplicate"]]
    _dec = _u["decision"].fillna("").str.strip().str.lower()
    n_all = int(len(_u))
    n_proceed = int(_dec.str.startswith("proceed").sum())
    n_park = int((_dec == "park").sum())
    n_decline = int((_dec == "decline").sum())
    n_open = n_all - (n_proceed + n_park + n_decline)

    bits = [f"{_org_name} screened {n_all:,} funding calls and proceeded with "
            f"{n_proceed:,} of them"
            + (f" — {n_proceed / n_all:.0%} of everything screened." if n_all else ".")]
    _tail = []
    if n_park:
        _tail.append(f"{n_park:,} were parked")
    if n_decline:
        _tail.append(f"{n_decline:,} declined")
    if n_open:
        _tail.append(f"{n_open:,} are still open")
    if _tail:
        bits.append(" and ".join([", ".join(_tail[:-1]), _tail[-1]]).strip(", ")
                    if len(_tail) > 1 else _tail[0] + ".")
        if len(_tail) > 1:
            bits[-1] = bits[-1] + "."

    # SUBMITTED — the agreed rule, from the shared helper, so it matches section 5's tile.
    try:
        from core.records import submission_weights as _sw
        n_sub = int(_sw(_u).sum())
        if n_sub:
            bits.append(f"{n_sub:,} applications have gone to funders.")
    except Exception:
        pass

    # SECURED — Approved rows only, exactly as section 5 computes it.
    try:
        _appr = _u[_u["donor_decision"].fillna("").str.strip().str.lower().eq("approved")]
        if not _appr.empty:
            _secured = float(_series_to_usd(_appr.get("amount_secured"),
                                            _appr.get("currency_secured")).sum())
            if _secured > 0:
                bits.append(f"{len(_appr):,} were approved, securing ${_secured:,.0f}.")
    except Exception:
        pass

    _areas = _programme_area_freq(_u[_dec.str.startswith("proceed")])
    if _areas:
        _top = [k for k, _ in sorted(_areas.items(), key=lambda kv: -kv[1])[:3]]
        _joined = (", ".join(_top[:-1]) + " and " + _top[-1]) if len(_top) > 1 else _top[0]
        bits.append(f"The work sits mainly in {_joined}.")
    bits.append(f"Unless a caption says otherwise, the figures below cover "
                f"{_period_phrase().lower()}.")
    return " ".join(bits)


_summary_text = _headline_summary()
st.info(_summary_text)
try:
    _sdoc = _report_pdf.current()
    if _sdoc is not None:
        # Markdown emphasis is for the page; the PDF renders plain prose.
        _sdoc.intro(_summary_text.replace("**", ""))
except Exception:
    pass

st.divider()


# ===========================================================================
# SECTION 1 — Scan activity (top of funnel)
# ===========================================================================
if _show_sec("1"):
    st.subheader("1 · Scan activity")
    (_report_pdf.current() or _PDF_DOC).section("1 · Scan activity")
    st.caption(
        "How the automated scanner is performing. KPI tiles come from "
        "`scan_logs` (one row per source per run). The time-series chart "
        "below tracks **RFPs discovered** using each row's `search_date`, "
        "so the curve covers the full period — not just days a scan ran."
    )

    # System-wide DISCOVERY counter — SUPER_USER ONLY (owner, 2026-08-12).
    #
    # It reports the shared crawl every tenant screens, which is not this tenant's activity and
    # not something a tenant can act on: 25,036 discovered and 22,791 rejected at gate said
    # nothing about their own pipeline while being the largest numbers on the page. A tenant's
    # report should show what that tenant did — their eligibility scans and their migrated rows.
    # Operators still need the crawl's health, so it stays for super_user.
    #
    # Multi-tenant only (in single-tenant the tenant IS the system, so the two would
    # double-count).
    try:
        from auth.tenant_context import multitenant_enabled as _mte
        from core import permissions as _perms_sd
        if _mte() and _perms_sd.is_super_user(st.session_state.get("app_user") or {}):
            from core import analytics as _an

            # Cached: this rollup paginates scan_logs and counts the shared store, and the
            # Report page re-runs it on EVERY widget interaction. It's system-wide (not
            # tenant-scoped), so one shared 60s entry is correct.
            @st.cache_data(ttl=60, show_spinner=False)
            def _system_discovery_cached() -> dict:
                return _an.system_discovery_stats()

            _d = _system_discovery_cached()
            with st.container(border=True):
                st.markdown("🌐 **System-wide discovery** — the shared catalog every "
                            "tenant screens (not your tenant's activity)")
                _dc = st.columns(4)
                _dc[0].metric("Shared catalog", f"{_d['catalog']:,}")
                _dc[1].metric("Discovered", f"{_d['found']:,}")
                _dc[2].metric("Rejected at gate", f"{_d['rejected']:,}")
                _dc[3].metric("Discovery runs", _d["runs"])
                if _d.get("last_run"):
                    st.caption(f"Last discovery run: {str(_d['last_run'])[:16]}. "
                               "Your tenant's own screening activity is below.")
    except Exception:
        pass

    # WHERE THIS TENANT'S ROWS CAME FROM. Moved up from the funnel section (owner,
    # 2026-08-12): it describes intake, which is what section 1 is about, and with the
    # system-wide counter gone it is now the honest headline for a tenant — their eligibility
    # scans, their manual submissions, and the rows migrated from the legacy workbook.
    if not rfps_all.empty and "source" in rfps_all.columns:
        # EVERY RECORD, THE DUPLICATES, AND WHAT'S LEFT (owner, 2026-08-12).
        #
        # The tile showed unique rows only, so "Excel imported 52" could not be reconciled with
        # the 63 records actually imported — the 11 the dedupe caught were invisible, and a
        # reader comparing the report against the workbook finds a shortfall with no explanation.
        # Unique stays the headline, because it is what every other figure here counts; the
        # duplicates are stated beside it so the arithmetic is closed.
        _src_col = rfps_all["source"].fillna("(unknown)").astype(str).str.strip()
        _dup_col = rfps_all["is_duplicate"].astype(bool)
        _labels = {"auto": "System eligibility auto-scan", "migration": "Excel imported",
                   "manual": "Manually submitted via platform"}
        _by_src = (pd.DataFrame({"src": _src_col.str.lower(), "dup": _dup_col})
                   .groupby("src").agg(total=("dup", "size"), dups=("dup", "sum")))
        _by_src["unique"] = _by_src["total"] - _by_src["dups"]
        _by_src = _by_src.sort_values("total", ascending=False)

        if not _by_src.empty:
            _n_all = int(_by_src["total"].sum())
            _n_dup = int(_by_src["dups"].sum())
            _ic = st.columns(min(4, len(_by_src) + 1))
            _ic[0].metric(
                "Records ingested", f"{_n_all:,}",
                delta=(f"{_n_dup:,} duplicate{'s' if _n_dup != 1 else ''}" if _n_dup else None),
                delta_color="off",
                help="Every row stored for this organisation, from all intake routes, including "
                     "the duplicates the dedupe caught.")
            for _i, (_src, _row) in enumerate(_by_src.iterrows(), start=1):
                if _i >= len(_ic):
                    break
                _u, _d, _t = int(_row["unique"]), int(_row["dups"]), int(_row["total"])
                _ic[_i].metric(
                    _labels.get(str(_src), str(_src).title()), f"{_u:,}",
                    delta=(f"{_d:,} duplicate{'s' if _d != 1 else ''}" if _d else None),
                    delta_color="off",
                    help=(f"{_t:,} record(s) arrived by this route; {_d:,} were duplicates of "
                          f"calls already stored, leaving {_u:,} unique."))
            st.caption(
                f"Where this organisation's calls came from — all-time, not period-filtered. "
                f"The large number on each card is the UNIQUE calls kept, which is what every "
                f"other figure in this report counts; the grey number beside it is the "
                f"duplicates the dedupe removed. {_n_all:,} records in total, "
                f"{_n_dup:,} of them duplicates.")

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

    # ── RFP-derived blocks: NOT gated on scan logs ─────────────────────────────
    # These read rfp_submissions, not scan_logs, so they must render whether or not the
    # tenant has scan runs recorded in the period. They used to sit inside the
    # `else:` of `if scans.empty:` — so a period with no scan rows silently took the
    # keyword cloud, the discovery timeline, funding-by-donor, top sources and cycle
    # time with it, even though every one of them had data to show.
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
                    .reset_index(name="Funding calls discovered")
                )
                fig = px.bar(
                    stacked_disc, x="bucket", y="Funding calls discovered",
                    color="submitter", barmode="stack",
                    title=f"Funding calls discovered by member "
                          f"({_period_label_str}, {bucket_mode.lower()})",
                    # One colour per member: these categories are people, not an ordered scale,
                    # so the single-hue ramp made thirteen of them look identical.
                    color_discrete_sequence=_theme.categorical(
                        int(stacked_disc["submitter"].nunique())),
                    labels={"bucket": _bucket_label(bucket_mode), "submitter": "Submitted by"},
                )
                fig.update_layout(
                    height=360, margin=dict(t=40, b=10),
                    xaxis=_fmt_bucket_ticks(bucket_mode),
                    legend=dict(orientation="h", yanchor="top", y=-0.18,
                                xanchor="center", x=0.5, font=dict(size=11)),
                )
                _boxed(fig)

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
                    # Kept on the page, NOT collected into the PDF: it is the same counts as
                    # the chart directly above it, and the export does not need the number twice.
                    # (It was also mislabelled — these are members and discoveries, not keywords.)
                    st.dataframe(leader_series,
                                  width='stretch', hide_index=True)
            else:
                # No submitter data — fall back to a plain bucket count
                disc_df = _bucketed_count(disc["search_date"].dropna(), "RFPs discovered")
                fig = px.bar(
                    disc_df, x="bucket", y="Funding calls discovered",
                    title=f"Funding calls discovered ({_period_label_str}, {bucket_mode.lower()})",
                    labels={"bucket": _bucket_label(bucket_mode)},
                )
                fig.update_layout(height=320, margin=dict(t=40, b=10),
                                  xaxis=_fmt_bucket_ticks(bucket_mode))
                _boxed(fig)
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
                    title=f"Funding calls by donor — top 15 ({_period_label_str})",
                    color_discrete_sequence=[_theme.TURQUOISE],
                )
                fig_dn.update_layout(
                    height=max(280, 28 * len(donor_counts) + 80),
                    margin=dict(t=40, b=10),
                    yaxis={"categoryorder": "total ascending"},
                )
                _boxed(fig_dn)

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
        # PROCEED ONLY, and from the areas the calls actually CARRY (owner, 2026-08-12).
        #
        # It used to match a curated global-health vocabulary against every discovered call's
        # title and brief. That drew the search terms the scanner went looking for — including
        # across Park and Decline rows — so the cloud was largest exactly where the team had
        # decided NOT to bid. In a report a tenant sends to their leadership that is worse than
        # uninformative: it presents rejected subject matter as the tenant's focus.
        #
        # Now it reads `call_domain_areas` off the tenant's PROCEED calls: what was pursued, in
        # the words the pipeline recorded. All-time rather than period-filtered, because a
        # Proceed decision is the durable signal and a short period would leave this near-empty —
        # said plainly in the caption so it is not mistaken for period data.
        _proceed_all = rfps_all[
            (~rfps_all["is_duplicate"])
            & rfps_all["decision"].fillna("").str.strip().str.lower().str.startswith("proceed")
        ] if not rfps_all.empty and "decision" in rfps_all.columns else rfps_all.iloc[0:0]
        kw_freq = _programme_area_freq(_proceed_all) if _show("s1_keywords") else {}
        if _show("s1_keywords"):
            if kw_freq:
                st.markdown("#### Focus areas")
                st.caption(
                    f"Programme areas recorded on the **{len(_proceed_all)} calls this tenant "
                    "chose to pursue** (Proceed) — all-time, not filtered by the period above. "
                    "Word size is the number of those calls carrying the area. Park and Decline "
                    "calls are excluded, and so are the keywords the scanner searches on: this "
                    "shows where the team committed effort, not what it looked at."
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
                    with st.container(border=True):
                        st.pyplot(fig_wc, width='stretch')
                    # Into the export as well. It is the ONE raster thing in the document — a
                    # word cloud is a bitmap by nature — and it was simply absent before, which
                    # left the "Focus areas" heading over nothing in the PDF.
                    try:
                        _wdoc = _report_pdf.current()
                        if _wdoc is not None:
                            _wdoc.image(fig_wc, height=300)
                    except Exception:
                        pass
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
                            columns=["Programme area", "Proceed calls"],
                        ).head(40)
                    )
                    _table(_kw_top, "Focus areas on Proceed calls", width='stretch',
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
                # Shared app-wide "submitted": Completed OR a donor decision recorded.
                is_submitted = (progress_str == "completed"
                                or donor_str in _SUBMITTED_DECISIONS)
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
                    _table(
                        kw_agg.rename(columns={"keyword": "Keyword"}),
                        title="Keywords driving success",
                        width='stretch', hide_index=True,
                    )

    # Top sources by yield — the ONE block here that really is scan-log-derived, so it keeps
    # its own guard rather than borrowing the section-wide one. Without this, de-nesting the
    # RFP blocks made an empty scan-log frame raise KeyError('source') on the groupby.
    src = pd.DataFrame()
    if not scans.empty and "source" in scans.columns:
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
            _table(
                src.rename(columns={
                    "source": "Source", "runs": "Runs", "found": "Found",
                    "new": "New", "rejected": "Rejected",
                    "avg_dur": "Avg duration (s)", "yield_pct": "Yield %",
                }),
                title="Top 15 sources by new-call yield",
                width='stretch', hide_index=True,
            )

    # ───────────── Search → Submission cycle time (relocated from §4) ─────
    # Lives in Section 1 because Search Date is the anchor of the metric —
    # it's the search-to-submission lag, conceptually a search-activity
    # quality signal. (Originally placed in §4 Team Activity which was
    # the wrong narrative beat.)
    if _show("s1_cycle") and not rfps_all.empty:
        _h5("Search → Submission cycle time")
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
                labels={"days": "Days from discovery to submission",
                        "count": "Number of funding calls"},
            )
            fig_cyc.update_layout(height=280, margin=dict(t=40, b=10))
            _boxed(fig_cyc)
        else:
            st.info("No RFPs with both search_date and date_completed yet — "
                    "cycle time chart will populate as proposals get submitted.")

    st.divider()


# ===========================================================================
# SECTION 4 (displayed 2nd) — Team & Partnership Activity
# Single umbrella covering everything about WHO does the work and WHO we
# do it with: internal meetings, donor engagements, individual member
# activity, proposal leads / contributors, lead & sub applicant partners,
# status mix, requested-vs-secured economics, conversion + cycle time.
# ===========================================================================
if _show_sec("4"):
    st.subheader("2 · Team & Partnership Activity")
    (_report_pdf.current() or _PDF_DOC).section("2 · Team & Partnership Activity")
    st.caption(
        "Everything about WHO does the work and WHO we do it with — internal "
        "triage meetings (**Team Touchpoints**), donor-facing engagements "
        "(**Donor Touchpoints**), member-level submission activity, proposal "
        "leadership, partner trends, status mix, funding economics, and the "
        "Proceed-to-Submitted conversion."
    )

    # ─── Team Touchpoints (internal team meetings) ─────────────────────────────
    _h5("Team Touchpoints")
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
    _h5("Donor Engagements")
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
        _boxed(fig)

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
            _table(
                top_donors.rename(columns={
                    "donor": "Donor", "touchpoints": "Touchpoints",
                    "types": "Engagement types",
                }),
                title="Top 10 donors by touchpoints",
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
        # Section 4 RFP distributions reflect the PROCEED pipeline only (the actionable
        # RFPs found) — consistent with the convention that everything after the Section-2
        # eligibility/conversion funnels is Proceed-scoped.
        _ded_all = rfps_all[~rfps_all["is_duplicate"]]
        activity_rows = _ded_all[
            _ded_all["decision"].fillna("").str.lower().str.startswith("proceed")].copy()

        # ───────────── Submissions by team member ─────────────────────────────
        # REMOVED 2026-06-05: chart + leaderboard moved to Section 1, directly
        # under "RFPs discovered by member" — same data presented twice was
        # redundant. The leaderboard expander now sits inline with the
        # discovery chart, and Section 4 jumps straight from KPIs to the
        # stacked Submitted/Unsubmitted views.

        # ───────────── Helpers for stacked Submitted / Unsubmitted bars ───────
        # "Submitted" uses the shared app-wide definition (Completed OR a donor decision) —
        # computed per RFP via _submitted_mask before exploding names, so it matches every
        # other page. Everything else goes into "Unsubmitted".
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
        # One hue at two opacities: submitted is the emphatic half of the same bar.
        _STACK_COLORS = _theme.sequence_for(list(_theme.SUBMITTED_ORDER),
                                            order=_theme.SUBMITTED_ORDER)

        # ───────── Proposal Leads + Contributors, SIDE BY SIDE ────────────────
        # Two charts of the same shape — people on the y-axis, submitted vs unsubmitted
        # stacked — so they belong in one row where the same person's two bars can be
        # compared at a glance. Stacked vertically they were a scroll apart.
        #
        # Each stays INDEPENDENTLY toggleable, so when only one section is enabled it takes
        # the full width instead of rendering half a row with a gap beside it. That is why
        # the figures are BUILT first and rendered afterwards: the layout can only be chosen
        # once we know how many charts there are.
        #
        # `proposal_lead` is free text that may hold one name or a comma-separated list
        # ("Alice, Bob"); `contributors` is a Postgres text[] whose elements can themselves
        # carry comma-separated names from sloppy form submissions.
        # split_and_normalize_names handles both shapes, and the per-row set() dedupe means
        # one RFP never counts the same person twice.
        def _people_stack(col: str, y_title: str, x_title: str, chart_title: str,
                          empty_msg: str):
            """(figure|None, message|None, row_count) for one people-vs-status chart."""
            if col not in activity_rows.columns:
                return None, empty_msg, 0
            rows = activity_rows[[col, "progress_status", "donor_decision"]].copy()
            rows["is_submitted"] = _submitted_mask(rows).to_numpy()
            rows["_members"] = rows[col].apply(
                lambda v: sorted(set(split_and_normalize_names(v))))
            rows = rows.explode("_members").dropna(subset=["_members"])
            rows = rows[rows["_members"] != ""]
            if rows.empty:
                return None, empty_msg, 0
            disp = first_name_display_map(rows["_members"])
            rows["display"] = rows["_members"].map(disp).fillna(rows["_members"])
            stacked = _stacked_chart_df(rows[["display", "is_submitted"]], "display")
            if stacked.empty:
                return None, empty_msg, 0
            fig = px.bar(
                stacked, x="RFPs", y="display",
                color="Status", orientation="h",
                color_discrete_map=_STACK_COLORS,
                title=chart_title, text="RFPs",
                category_orders={"Status": ["Submitted", "Unsubmitted"]},
            )
            return fig, None, int(stacked["display"].nunique())

        _pl_fig = _ct_fig = None
        _pl_msg = _ct_msg = None
        _pl_n = _ct_n = 0
        if _show("s4_leads"):
            _pl_fig, _pl_msg, _pl_n = _people_stack(
                "proposal_lead", "Proposal lead", "RFPs",
                "Top 15 by RFP count (Submitted vs Unsubmitted)",
                "No proposal_lead values recorded yet.")
        if _show("s4_contrib"):
            _ct_fig, _ct_msg, _ct_n = _people_stack(
                "contributors", "Contributor", "RFP contributions",
                "Top 15 by RFPs supported (Submitted vs Unsubmitted)",
                "No contributors recorded yet.")

        # ONE height for both, from whichever has more people, so the two panels line up
        # instead of one floating short beside the other.
        _people_h = max(280, 30 * max(_pl_n, _ct_n) + 100)

        def _finish(fig, y_title: str, x_title: str):
            fig.update_layout(
                height=_people_h, margin=dict(t=54, b=10), barmode="stack",
                yaxis={"categoryorder": "total ascending", "title": y_title},
                xaxis={"title": x_title},
                title={"font": dict(size=13)},
                legend={"orientation": "h", "yanchor": "bottom", "y": 1.02,
                        "xanchor": "right", "x": 1, "font": dict(size=11)},
            )
            return fig

        def _panel(heading: str, fig, msg, y_title: str, x_title: str):
            _h5(heading)
            if fig is not None:
                _boxed(_finish(fig, y_title, x_title))
            elif msg:
                st.info(msg)

        if _show("s4_leads") and _show("s4_contrib"):
            _pc1, _pc2 = st.columns(2, gap="medium")
            with _pc1:
                _panel("Proposal Leads", _pl_fig, _pl_msg, "Proposal lead", "RFPs")
            with _pc2:
                _panel("Contributors", _ct_fig, _ct_msg, "Contributor",
                       "RFP contributions")
        elif _show("s4_leads"):
            _panel("Proposal Leads", _pl_fig, _pl_msg, "Proposal lead", "RFPs")
        elif _show("s4_contrib"):
            _panel("Contributors", _ct_fig, _ct_msg, "Contributor", "RFP contributions")

        # ───────────── Lead & Sub Applicant partners ──────────────────────────
        # Over the PROCEED RFPs (activity_rows), each applicant cell can list MULTIPLE partners
        # who applied jointly on the SAME grant, separated by ";" or "," (e.g. "Org North;
        # Org South"). We split them → one count each, canonicalise the deploying org's own name
        # (its short form / hyphenated variant → its full canonical name, while a distinct
        # sibling org is left alone), and DE-DUP per RFP → the number of distinct lead/sub applicants
        # per Proceed RFP. BLANK cells and the literal "N/A" (Not Applicable — a real, distinct
        # value) are BOTH dropped: neither is an applicant, so neither belongs on the chart.
        # A separator INSIDE one org's own name (a legal suffix after a comma, or an
        # abbreviation after a ";") does not make a second applicant — see core.partner_names.
        if _show("s4_partners"):
            _h5("Lead & Sub Applicant partners")
        _NA_VALUES = partner_names.NA_VALUES
        _org_full = (settings.get_org_name() or "").strip()
        _org_short = (settings.get_org_short() or "").strip()
        _org_full_key = re.sub(r"\s+", " ", _org_full.replace("-", " ")).strip().lower()
        # The bare acronym = the org's first word (the full name's leading token). A bare
        # acronym rolls up to the full name; a qualified sibling (two tokens) does NOT.
        _org_first = _org_full.split()[0].lower() if _org_full else ""

        def _norm_org(raw) -> str:
            s = re.sub(r"\s+", " ", str(raw or "").replace("-", " ")).strip()
            if not s or s.strip().lower() in _NA_VALUES:
                return ""                          # blank OR "N/A"/"Not Applicable" → drop
            # Canonicalise on the name WITHOUT its own trailing abbreviation, so the deploying
            # org still resolves to one bar when its cell was written "Full Name; (FN)" and the
            # splitter re-attached the abbreviation.
            low = partner_names.strip_trailing_acronym(s).lower()
            if _org_short and low == _org_short.lower():
                return _org_full or s              # bare short name → canonical full name
            if _org_full and low == _org_full_key:
                return _org_full                   # variant/hyphen/case → canonical casing
            if _org_first and low == _org_first:
                return _org_full                   # bare acronym → canonical full name
            return s                               # distinct sibling org untouched

        def _split_orgs(v) -> list[str]:
            # Separator splitting + re-attachment lives in core.partner_names so it can be
            # tested; this page is script-scope and cannot be imported.
            return sorted({o for o in (_norm_org(p) for p in partner_names.split_pieces(v)) if o})

        ap_l, ap_r = st.columns(2)
        for col_name, col_label, container in [
            ("lead_applicant", "Lead applicant", ap_l),
            ("sub_applicant",  "Sub applicant",  ap_r),
        ]:
            if _show("s4_partners") and col_name in activity_rows.columns:
                _rows = activity_rows[[col_name]].copy()
                _rows["_orgs"] = _rows[col_name].apply(_split_orgs)   # blank/N/A already dropped
                _rows = _rows.explode("_orgs").dropna(subset=["_orgs"])
                _rows = _rows[_rows["_orgs"].astype(str).str.strip() != ""]
                counts = (_rows["_orgs"].value_counts().head(10)
                          .rename_axis(col_label).reset_index(name="RFPs"))
                with container:
                    if not counts.empty:
                        fig = px.bar(
                            counts, x="RFPs", y=col_label, orientation="h",
                            title=f"{col_label} — top 10 (Proceed calls)", text="RFPs",
                        )
                        # Shaded by RANK: the busiest partner takes the primary, the tail fades
                        # toward the light end. `counts` is already sorted descending, and the
                        # ramp runs emphatic-first, so the two line up.
                        fig.update_traces(marker_color=_theme.ramp(len(counts)))
                        fig.update_layout(
                            height=max(250, 30 * len(counts) + 60),
                            margin=dict(t=40, b=10),
                            yaxis={"categoryorder": "total ascending"},
                        )
                        _boxed(fig)
                    else:
                        st.info(f"No {col_label.lower()} values recorded yet.")

        # Progress status chart relocated to Section 2 (Insights) — same
        # narrative beat as the eligibility funnel. Not re-rendered here.

        # The Requested-vs-Secured scatter + Conversion rates blocks
        # relocated to Section 5 (Our Results) — that's the natural home for
        # outcome-shaped metrics. The Search → Submission cycle time block
        # relocated to Section 1 (Scan Activity) — Search Date is the
        # anchor, so it fits the search-narrative beat.

    st.divider()

# ===========================================================================
# SECTION 2 (displayed 3rd) — Status & eligibility funnel (insights from triage)
# ===========================================================================
if _show_sec("2"):
    st.subheader("3 · Insights — Status & Eligibility Funnel")
    (_report_pdf.current() or _PDF_DOC).section("3 · Insights — Status & Eligibility Funnel")
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

        # All-time current pipeline state — what the user actually wants.
        # Funnel stages are NESTED subsets so they can never invert:
        #   Total ⊇ Proceed ⊇ Submitted ⊇ Approved.
        total_all = int(len(unique_all))
        _dec_all = unique_all["decision"].fillna("").str.lower()
        proceed_df_all = unique_all[_dec_all.str.startswith("proceed")]
        proceed_all = int(len(proceed_df_all))
        park_all = int((_dec_all == "park").sum())
        decline_all = int((_dec_all == "decline").sum())
        no_decision_all = total_all - (proceed_all + park_all + decline_all)
        # Submitted = submitted-per-app-def WITHIN the Proceed track; Approved = the
        # approved subset OF those submitted — guarantees Approved ≤ Submitted ≤ Proceed.
        _sub_proc = proceed_df_all[_submitted_mask(proceed_df_all)]
        submitted_all = int(len(_sub_proc))
        approved_all = int((_sub_proc["donor_decision"].fillna("").str.lower()
                            == "approved").sum())

        # Period-restricted: how many RFPs were *discovered* in this window
        discovered_in_period = int(len(in_period))

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total unique RFPs", total_all,
                  help=f"All non-duplicate rows across every source (auto-scanned, "
                       f"manually submitted, Excel migration). {discovered_in_period} "
                       f"discovered in the active period.")
        k2.metric("Proceed", proceed_all,
                  help="Decision = Proceed. The actionable pipeline.")
        k3.metric("Park", park_all,
                  help="Held for later review — uncertain fit or no extractable deadline.")
        k4.metric("Decline", decline_all,
                  help="Filtered by the team or the decision tree.")

        _STAGES = ["Discovered (all-time)", "Proceed-track", "Submitted", "Approved"]
        _FUNNEL_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#22c55e"]

        # COUNT funnel — how many RFPs make it through each stage.
        fig_funnel = go.Figure(go.Funnel(
            y=_STAGES, x=[total_all, proceed_all, submitted_all, approved_all],
            textinfo="value+percent initial", marker={"color": _FUNNEL_COLORS},
        ))
        fig_funnel.update_layout(height=340, margin=dict(t=40, b=10),
                                 title="Conversion funnel — counts")

        # VALUE funnel — the $ at each stage. Each stage uses the most meaningful amount:
        #   Discovered  → Σ call_award_value across ALL unique RFPs (Proceed + Park + Decline)
        #   Proceed     → Σ call_award_value across the Proceed track
        #   Submitted   → Σ amount_requested (Estimated Value) across the submitted set
        #   Approved    → Σ amount_secured across the approved subset (what we actually won)
        _appr = _sub_proc[_sub_proc["donor_decision"].fillna("").str.lower() == "approved"]
        _disc_v = float(_series_to_usd(
            unique_all["call_award_value"], unique_all.get("currency", pd.Series(dtype=str))).sum())
        _proc_v = float(_series_to_usd(
            proceed_df_all["call_award_value"], proceed_df_all.get("currency", pd.Series(dtype=str))).sum())
        _sub_v = float(_series_to_usd(
            _sub_proc["amount_requested"], _sub_proc.get("currency", pd.Series(dtype=str))).sum())
        _appr_v = float(_series_to_usd(
            _appr["amount_secured"], _appr.get("currency_secured", pd.Series(dtype=str))).sum())
        fig_funnel_v = go.Figure(go.Funnel(
            y=_STAGES, x=[_disc_v, _proc_v, _sub_v, _appr_v],
            texttemplate="$%{x:,.0f}", marker={"color": _FUNNEL_COLORS},
        ))
        fig_funnel_v.update_layout(height=340, margin=dict(t=40, b=10),
                                   title="Conversion funnel — value (USD)")
        if _show("s2_funnel"):
            _fc1, _fc2 = st.columns(2)
            # Framed individually — side by side and unframed, the pair read as one graphic.
            with _fc1:
                _boxed(fig_funnel)
            with _fc2:
                _boxed(fig_funnel_v)
            st.caption("Value funnel: Discovered / Proceed = estimated award value; Submitted "
                       "= amount requested; Approved = amount secured.")
        # Decision-distribution chart used to live here; moved to Section 3
        # since "where decisions land" belongs with the Reviews & Decisions
        # narrative, not the funnel.

        # ───────────── Progress status (relocated from Section 4) ─────────
        # Belongs in Insights because it's another "where is each RFP in the
        # pipeline?" view — the funnel above shows attrition, this shows the
        # current workflow distribution. Same narrative beat.
        #
        # PROCEED-ONLY: progress_status is a lifecycle only Proceed RFPs have — a Park/Decline
        # row has no proposal to progress, so including them produced a meaningless "(unset)"
        # bar (which was really just one blank-progress Decline row, not a data gap). Scoping
        # to Proceed removes that noise and matches the Summary reconciliation funnel. Any
        # blank progress on a Proceed row defaults to "Not Started" (its correct default), so
        # every Proceed RFP is accounted for and there is no "(unset)".
        if _show("s2_progress") and "progress_status" in unique_all.columns:
            _proc_ps = unique_all[
                unique_all["decision"].fillna("").str.lower().str.startswith("proceed")
            ].copy()
            _PS_CANON = {
                "not started": "Not Started", "in progress": "In Progress",
                "completed": "Completed", "discontinued": "Discontinued",
                "missed": "Missed", "missing": "Missed",
            }

            def _canon_progress(v) -> str:
                s = str(v or "").strip().lower()
                return _PS_CANON.get(s, "Not Started")  # blank/unknown → Not Started

            if _proc_ps.empty:
                st.caption("_No Proceed RFPs in scope._")
            else:
                _order = ["Not Started", "In Progress", "Completed",
                          "Discontinued", "Missed"]
                ps = (
                    _proc_ps["progress_status"].apply(_canon_progress)
                    .value_counts().reindex(_order, fill_value=0)
                    .rename_axis("Status").reset_index(name="RFPs")
                )
                # Traffic-light semantics: Not Started (red) → In Progress (amber) →
                # Completed (green); Discontinued / Missed are muted greys.
                # Shaded along pipeline progression (Completed most emphatic), so the ink
                # carries the ordering the old red/amber/green only implied.
                _PS_COLORS = _theme.sequence_for(list(ps["Status"]),
                                                 order=_theme.PROGRESS_ORDER)
                fig_ps = px.bar(ps, x="Status", y="RFPs", text="RFPs",
                                title="Progress status — Proceed calls", color="Status",
                                category_orders={"Status": _order},
                                color_discrete_map=_PS_COLORS)
                fig_ps.update_layout(height=300, showlegend=False,
                                     margin=dict(t=40, b=10), xaxis_title=None)
                _boxed(fig_ps)
                st.caption(f"_{len(_proc_ps)} Proceed RFPs (blank progress counts as "
                           f"'Not Started'). Completed = submitted to donor._")

    st.divider()


# ===========================================================================
# SECTION 3 (displayed 4th) — Reviews & Decisions
# ===========================================================================
if _show_sec("3"):
    st.subheader("4 · Reviews & Decisions")
    (_report_pdf.current() or _PDF_DOC).section("4 · Reviews & Decisions")
    st.caption(
        "Triage outcomes — velocity from RFP discovery to decision, where "
        "the auto-recommendation needed manual override, and the funder's own "
        "decision on what we submitted."
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

        # Proceed-only for the lower charts: §3 drills into the RFPs the team chose to pursue
        # (consistent with §4+). The KPI row and Decision Distribution stay full-triage — they
        # describe the whole review process, and the distribution chart IS the
        # Proceed/Park/Decline split.
        _proc_dec = decided[
            decided["decision"].fillna("").str.lower().str.startswith("proceed")
        ]

        # ── Decision Distribution + Proceed decisions over time, ONE ROW ──────────────
        # They answer two halves of the same question — where decisions land, and when they
        # happened — so they belong side by side rather than a scroll apart.
        #
        # The split is 1:3. The time series is a monthly run across the period and needs the
        # width or its buckets crowd; the distribution is four bars and does not. That also makes
        # VERTICAL bars right for the distribution: in a narrow column horizontal bars waste the
        # height and squeeze the value labels, and four categories never collide on the x-axis.
        fig_dec = fig_time = None

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
            fig_dec = px.bar(
                dec_df, x="decision", y="count", text="count",
                title="Team decisions: our own Proceed / Park / Decline",
                color="decision",
                # Fixed semantic order, not frequency order: the shade then means the same thing
                # on every report instead of tracking whichever decision happens to dominate.
                category_orders={"decision": _theme.DECISION_ORDER},
                color_discrete_map=_theme.sequence_for(
                    list(dec_df["decision"]), order=_theme.DECISION_ORDER),
            )
            fig_dec.update_layout(height=300, showlegend=False,
                                  xaxis_title=None, yaxis_title="Funding calls")

        if _show("s3_dectime") and not _proc_dec.empty:
            dec_dates = pd.to_datetime(_proc_dec["decision_date"], errors="coerce").dropna()
            if not dec_dates.empty:
                ts_df = _bucketed_count(dec_dates, "decisions")
                fig_time = px.bar(ts_df, x="bucket", y="decisions",
                                  title=f"Team Proceed decisions over time "
                                        f"({_period_label_str}, {bucket_mode.lower()})",
                                  labels={"bucket": _bucket_label(bucket_mode),
                                          "decisions": "Proceed decisions"},
                                  color_discrete_sequence=[_theme.TURQUOISE])
                fig_time.update_layout(height=300, xaxis=_fmt_bucket_ticks(bucket_mode))

        if fig_dec is not None and fig_time is not None:
            _dc1, _dc2 = st.columns([1, 3], gap="medium")
            with _dc1:
                _boxed(fig_dec)
            with _dc2:
                _boxed(fig_time)
        elif fig_dec is not None:
            # Alone it gets the full width, rather than a quarter-row with a gap beside it.
            _boxed(fig_dec)
        elif fig_time is not None:
            _boxed(fig_time)

        if _show("s3_autorec") and not decided.empty:
            # SYSTEM vs HUMAN, which is the only reason to show this at all.
            #
            # It used to list the auto-scorer's recommendation for TEAM-PROCEED rows only, under a
            # title promising a comparison: one column of recommendations and one count, with the
            # team's decision nowhere in it. Every row was a Proceed, so there was nothing to
            # compare against.
            #
            # Now it is a cross-tab — the scorer's recommendation down the side, the team's
            # decision across the top — so agreement sits on the diagonal and every override is
            # visible as an off-diagonal count.
            _ar_src = decided.copy()
            _ar_src["_auto"] = (_ar_src["auto_recommendation"].fillna("").astype(str)
                                .str.strip().str.title().replace("", "No recommendation"))
            _ar_src["_team"] = (_ar_src["decision"].fillna("").astype(str)
                                .str.strip().str.title().replace("", "No decision"))
            _order = ["Proceed", "Park", "Decline", "No recommendation", "No decision"]
            _xtab = pd.crosstab(_ar_src["_auto"], _ar_src["_team"])
            _xtab = _xtab.reindex(index=[r for r in _order if r in _xtab.index],
                                  columns=[c for c in _order if c in _xtab.columns])
            _agree = int(sum(_xtab.at[k, k] for k in _xtab.index if k in _xtab.columns))
            _total = int(_xtab.to_numpy().sum())
            with st.expander(
                f"Auto-scorer vs the team — agreed on {_agree} of {_total} decided calls",
                expanded=False,
            ):
                st.caption(
                    "Rows are what the auto-scorer recommended; columns are what the team "
                    "decided. The diagonal is agreement; anything off it is a call where the "
                    "team overrode the scorer."
                )
                _table(_xtab.reset_index().rename(columns={"_auto": "Auto-scorer said"}),
                       title="Auto-scorer recommendation vs the team's decision",
                       width='stretch', hide_index=True)

        # ───────────── Donor Decisions ─────────────────────────────────────────
        # The charts above are OUR decisions (Proceed / Park / Decline — whether to bid).
        # `donor_decision` is the FUNDER's decision on what we submitted, which is the other
        # half of the same review story and belongs in this section rather than under results.
        #
        # Proceed-scoped, like the rest of §3's lower charts. "Not submitted" is excluded: it is
        # the default for everything still in flight, so leaving it in produces one bar that
        # dwarfs every real outcome and says nothing about donor decisions. The count of those
        # awaiting a decision is stated as a caption instead.
        # ALL Proceed calls, not only those decided inside the period. Period-filtering dropped
        # Not Approved and Submitted from the chart entirely — 13 real donor decisions exist and
        # only 6 were drawn, so a funder's rejection simply did not appear. A donor decision is a
        # durable outcome, like the Proceed decision itself; the caption says the scope.
        _proc_dd = rfps_all[
            (~rfps_all["is_duplicate"])
            & rfps_all["decision"].fillna("").str.strip().str.lower().str.startswith("proceed")
        ] if not rfps_all.empty and "decision" in rfps_all.columns else _proc_dec
        if _show("s3_donordec") and "donor_decision" in _proc_dd.columns:
            _h5("Donor decisions: the funder's response to what we submitted")
            _dd = _proc_dd["donor_decision"].fillna("").astype(str).str.strip()
            _pending = int((_dd.str.lower().isin(["", "not submitted"])).sum())
            _dd = _dd[~_dd.str.lower().isin(["", "not submitted"])]
            if _dd.empty:
                st.info("No donor decisions recorded yet.")
            else:
                _ddc = (_dd.str.title().value_counts()
                        .rename_axis("Donor decision").reset_index(name="RFPs"))
                fig_dd = px.bar(
                    _ddc, x="RFPs", y="Donor decision", orientation="h",
                    title="Donor decisions on submitted proposals", text="RFPs",
                    color="Donor decision",
                    color_discrete_map=_theme.sequence_for(
                        list(_ddc["Donor decision"]), order=_theme.DONOR_DECISION_ORDER),
                )
                fig_dd.update_layout(height=max(220, 34 * len(_ddc) + 70), showlegend=False,
                                     margin=dict(t=40, b=10), yaxis_title=None,
                                     yaxis={"categoryorder": "total ascending"})
                _boxed(fig_dd)
            if _pending:
                st.caption(
                    f"All-time across this organisation's Proceed calls, not filtered by the "
                    f"period above. {_pending} have no donor decision yet — not submitted, or "
                    "submitted and awaiting an outcome — and are excluded from the chart."
                )

    st.divider()



# ===========================================================================
# SECTION 5 — Our Results
# ===========================================================================
if _show_sec("5"):
    st.subheader("5 · Our Results — Proposals Submitted & Grants Secured")
    (_report_pdf.current() or _PDF_DOC).section("5 · Our Results")
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
        _dec_l = unique["decision"].fillna("").str.lower()
        proceed_track = unique[_dec_l.str.startswith("proceed")]

        # SUBMITTED GRANTS = sum of donor-side SUBMISSION EVENTS over Proceed rows whose
        # Progress = Completed (an RFP's `submissions` column can be >1 when it was submitted
        # to a donor more than once). So 8 completed RFPs where one carried 2 submissions = 9.
        _pc = proceed_track[
            proceed_track["progress_status"].fillna("").str.lower().eq("completed")].copy()
        _subs = (_pc["submissions"].fillna(1).astype(int) if "submissions" in _pc.columns
                 else pd.Series(1, index=_pc.index, dtype=int))
        n_submitted = int(_subs.sum())
        approved_period = _pc[_pc["donor_decision"].fillna("").str.lower() == "approved"]
        n_approved = int(len(approved_period))
        win_rate = (n_approved / n_submitted * 100) if n_submitted > 0 else 0.0

        # --- FX conversion to USD (per-field currency) ------------------------
        # Outcome amounts over the SUBMITTED-with-a-donor-decision set (matches the
        # Requested-vs-Secured chart below): Secured = amount_secured on Approved; Unsecured =
        # amount_requested on Not-Approved; Requested = amount_requested across the set.
        usd_view = unique.copy()
        # The REQUEST converts with the currency WE submitted in (currency_secured), not
        # the call's advertised currency — see core.records.requested_currency.
        usd_view["req_usd"] = _series_to_usd(
            usd_view["amount_requested"],
            usd_view.get("currency_secured", pd.Series(dtype=str)).fillna("")
            .replace("", pd.NA).fillna(usd_view.get("currency", pd.Series(dtype=str))))
        usd_view["sec_usd"] = _series_to_usd(
            usd_view["amount_secured"], usd_view.get("currency_secured", pd.Series(dtype=str)))
        _ddl = usd_view["donor_decision"].fillna("").str.strip().str.lower()
        outcome_df = usd_view[_ddl.isin({"approved", "under review", "not approved"})].copy()
        _od = outcome_df["donor_decision"].str.strip().str.lower()
        total_req = float(outcome_df["req_usd"].sum())
        amt_secured = float(outcome_df.loc[_od.eq("approved"), "sec_usd"].sum())
        total_unsec = float(outcome_df.loc[_od.eq("not approved"), "req_usd"].sum())
        sec_ratio = (amt_secured / total_req * 100) if total_req > 0 else 0.0
        # rows with an amount but no currency (silently treated as USD) — transparency badge.
        _sec_nocur = outcome_df[(_od.eq("approved")) & (outcome_df["amount_secured"].fillna(0) > 0)]
        n_missing_secured_cur = int(
            _sec_nocur.get("currency_secured", pd.Series(dtype=str)).fillna("").str.strip().eq("").sum())

        # Pipeline value (Proceed track, call_award_value)
        if not proceed_track.empty:
            amt_pipeline = float(_series_to_usd(
                proceed_track["call_award_value"],
                proceed_track.get("currency", pd.Series(dtype=str))).sum())
            n_missing_pipe_cur = int(proceed_track[
                proceed_track["call_award_value"].fillna(0) > 0
            ].get("currency", pd.Series(dtype=str)).fillna("").str.strip().eq("").sum())
        else:
            amt_pipeline = 0.0
            n_missing_pipe_cur = 0

        # Consolidated KPI cards — counts on top, amounts below (no duplicate secured tile).
        # Card order: counts and the ratio they produce on the first row, money on the second.
        # "Secured ÷ Requested" sat alone at the end of a four-tile row of amounts, reading as an
        # afterthought when it is the summary of them.
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Applications submitted", n_submitted,
                  help="Sum of donor-side submissions on Proceed RFPs whose Progress = "
                       "Completed (an RFP can be submitted to a donor more than once).")
        k2.metric("Approved", n_approved,
                  help="Submitted (Proceed + Completed) RFPs with donor_decision = Approved.")
        k3.metric("Win rate", f"{win_rate:.1f}%" if n_submitted else "—",
                  help="Approved ÷ Applications submitted. '—' with no submissions.")
        k4.metric("Secured ÷ Requested", f"{sec_ratio:.1f}%",
                  help="Total secured ÷ total requested, in USD.")
        # Keep the two rows apart in the export as well; without this they merge into one run
        # of seven tiles and wrap wherever the width falls.
        try:
            _rb = _report_pdf.current()
            if _rb is not None:
                _rb.row_break()
        except Exception:
            pass
        m1, m2, m3 = st.columns(3)
        m1.metric("Total Requested (USD)", f"${total_req:,.0f}")
        m2.metric("Total Secured (USD)", f"${amt_secured:,.0f}",
                  help="amount_secured on Approved calls, row-by-row to USD via the FX rates "
                       "in Admin → Settings.")
        m3.metric("Total Unsecured (USD)", f"${total_unsec:,.0f}",
                  help="amount_requested on Not-Approved (declined) calls.")

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
                _boxed(fig)

        # (The "Submitted Grants" table was moved to the END of this section — see below.)

        st.markdown(
            f"**Pipeline value (Proceed track):** ${amt_pipeline:,.0f} USD across "
            f"{int((unique['decision'].fillna('').str.lower().str.startswith('proceed')).sum())} "
            f"RFPs — sum of `call_award_value` converted row-by-row from each "
            f"RFP's native currency."
        )

        # ───────────── Amount Requested vs Amount Secured (relocated from §4) ─
        # The signed outcome view — the aggregate amounts are in the KPI cards above; this
        # chart shows the per-RFP secured (▲) vs unsecured/declined (▼) picture.
        if _show("s5_reqsec"):
            _h5("Amount Requested vs Amount Secured (USD)")
        if _show("s5_reqsec") and {"amount_requested", "amount_secured"} <= set(unique.columns):
            out_df = outcome_df[(outcome_df["req_usd"] > 0)
                                | (outcome_df["sec_usd"] > 0)].copy()
            if not out_df.empty:
                _d = out_df["donor_decision"].str.strip().str.lower()
                # SIGNED outcome so the axis itself tells the story:
                #   Approved      → +secured (won; falls back to requested if secured blank)
                #   Under Review  → +requested (pending — potential, awaiting decision)
                #   Not Approved  → −requested (UNSECURED / lost — plotted BELOW zero, red)
                def _signed(row):
                    d = str(row.get("donor_decision") or "").strip().lower()
                    if d == "approved":
                        return row["sec_usd"] or row["req_usd"]
                    if d == "not approved":
                        return -row["req_usd"]
                    return row["req_usd"]           # under review → pending (positive)
                out_df["signed_usd"] = out_df.apply(_signed, axis=1)
                _STATE = {"approved": "Secured", "under review": "Requested (pending)",
                          "not approved": "Unsecured (declined)"}
                out_df["Outcome"] = _d.map(_STATE)
                out_df["label"] = (out_df["opportunity_title"].fillna("(untitled)")
                                   .astype(str).str.slice(0, 42))
                out_df = out_df.sort_values("signed_usd", ascending=False)
                _COLORS = {"Secured": "#1e8e3e",
                           "Requested (pending)": "#eab308",
                           "Unsecured (declined)": "#d1343b"}
                fig_amt = px.bar(
                    out_df, x="label", y="signed_usd", color="Outcome",
                    color_discrete_map=_COLORS,
                    title="Requested vs Secured — secured (▲) vs unsecured / declined (▼)",
                    labels={"signed_usd": "USD  ·  secured ▲ / unsecured ▼", "label": ""},
                    hover_data={"funding_agency": True, "req_usd": ":,.0f",
                                "sec_usd": ":,.0f", "donor_decision": True,
                                "signed_usd": False, "label": False},
                )
                fig_amt.add_hline(y=0, line_color="#334155", line_width=1)
                # Legend TOP-RIGHT (inside the plot) so it doesn't collide with the angled
                # x-axis labels at the bottom.
                fig_amt.update_layout(
                    height=430, margin=dict(t=40, b=120), xaxis_tickangle=-40,
                    legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                xanchor="right", x=1, title_text=""))
                _boxed(fig_amt)
            else:
                st.info("No submitted RFPs with monetary amounts recorded yet.")

        # ───────────── Conversion rates (relocated from §4) ───────────────────
        # The actual rates that quantify the funnel. Belongs with Outcomes
        # because the headline question — "of the RFPs we said we'd pursue,
        # how many did we actually submit, and how many won?" — IS the
        # outcomes question, not a team-activity question.
        if _show("s5_conv"):
            _h5("Conversion rates")
        proceeded = unique[
            unique["decision"].fillna("").str.lower().str.startswith("proceed")
        ]
        # Submitted = the shared app-wide definition (Completed OR a donor decision),
        # so Proceeded→Submitted reconciles with the funnel and Applied Funding page.
        submitted_conv = proceeded[_submitted_mask(proceeded).to_numpy()]
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

        # ───────────── Submitted Grants (was "Applied Funding pipeline") ────────
        # The proposals we've actually submitted — Proceed RFPs whose Progress = Completed —
        # with what we requested, what's secured, and the donor's decision. Replaces the old
        # applied_funding table (whose reporting "Status" was stale/invalid, and whose Next
        # report / Owner columns aren't needed here). Not collapsible; the closing table of
        # the results story.
        if _show("s5_grants"):
            _h5("Applied Funding Opportunities")
            st.caption("Every grant we've submitted (Proceed RFPs with Progress = "
                       "Completed) — requested amount and the donor's decision.")
            if _pc.empty:
                st.info("No applied grants yet.")
            else:
                _sg = _pc.copy()
                _sg["_req"] = _series_to_usd(
                    _sg["amount_requested"], _sg.get("currency", pd.Series(dtype=str)))
                _sg["_sec"] = _series_to_usd(
                    _sg["amount_secured"], _sg.get("currency_secured", pd.Series(dtype=str)))
                _sg["_subs"] = (_sg["submissions"].fillna(1).astype(int)
                                if "submissions" in _sg.columns else 1)
                _tbl = pd.DataFrame({
                    "Grant": _sg["opportunity_title"].fillna("(untitled)").astype(str).str.slice(0, 70),
                    "Funder": _sg["funding_agency"].fillna("—"),
                    "Requested (USD)": _sg["_req"],
                    "Secured (USD)": _sg["_sec"],
                    "Donor decision": (_sg["donor_decision"].fillna("").astype(str).str.strip()
                                       .replace("", "Under Review")),
                    "Submissions": _sg["_subs"],
                    "Submitted": pd.to_datetime(_sg["date_completed"], errors="coerce").dt.date,
                }).sort_values("Requested (USD)", ascending=False)
                _table(
                    # No title here: the subsection heading above already names it, and
                    # passing both printed the label twice in the PDF.
                    _tbl, width='stretch', hide_index=True,
                    column_config={
                        "Grant": st.column_config.TextColumn("Grant", width="large"),
                        "Requested (USD)": st.column_config.NumberColumn(
                            "Requested (USD)", format="$%,.0f"),
                        "Secured (USD)": st.column_config.NumberColumn(
                            "Secured (USD)", format="$%,.0f"),
                        "Donor decision": st.column_config.TextColumn("Donor decision"),
                        "Submissions": st.column_config.NumberColumn(
                            "Submissions", format="%d",
                            help="Number of donor-side submissions for this RFP "
                                 "(an RFP can be submitted more than once)."),
                    },
                )

    st.divider()


# NOTE: A standalone "Section 6 · Team & Partnership Activity" used to
# live here. It has been folded into Section 4 (which was renamed from
# "Meetings & Engagements" to "Team & Partnership Activity"). Nothing
# renders here anymore — moving on to the Footer.


# ===========================================================================
# SHAREABLE PDF
# ===========================================================================
# Built from the collected Document, not by printing the page — see core.report_pdf for why
# printing cannot work here. It runs on demand rather than on every rerun: a headless Chromium
# render is a few seconds, and `st.download_button` needs its bytes up front, so generating
# eagerly would put that on every widget interaction.
# WHO generated this report. A printed PDF circulates on its own, so it has to carry a
# signature back to a person — name and email — or a figure in it cannot be questioned by
# anyone who receives it. Read from the session, never from a form field, so it cannot be typed
# to say somebody else.
_gen_user = st.session_state.get("app_user") or {}
_gen_name = str(_gen_user.get("name") or "").strip()
_gen_email = str(_gen_user.get("email") or "").strip()
if _gen_name and _gen_email:
    _gen_by = f"{_gen_name} <{_gen_email}>"
else:
    _gen_by = _gen_name or _gen_email or "unknown user"

_pdf_doc = (_report_pdf.current() or _PDF_DOC).finish()

if st.session_state.pop("_rfpis_make_pdf", False):
    try:
        with st.spinner("Building the PDF — laying out charts at page width…"):
            _html_doc = _report_pdf.build_html(
                _pdf_doc,
                title=_report_name(),
                subtitle=_period_phrase(),
                meta={
                    # "Organization", not "Tenant": tenant is our word for the account, and a
                    # reader of the PDF has no reason to know it.
                    "Organization": _org_name,
                    "Period": (f"{_s_iso} → {_e_iso}" if _s_iso else "All time"),
                    "Report id": (_url_rid or "unsaved"),
                    "Generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "Generated by": _gen_by,
                },
            )
            st.session_state["_rfpis_pdf_bytes"] = _report_pdf.render_pdf(
                _html_doc,
                chart_count=_pdf_doc.chart_count,
                header_text=f"{_report_name()} · {_period_phrase()}",
                footer_text=f"Generated by {_gen_by} · report {_url_rid or 'unsaved'}",
            )
            st.session_state["_rfpis_pdf_name"] = _pdf_name
    except Exception as _pdf_exc:
        st.session_state["_rfpis_pdf_bytes"] = None
        st.error(f"Couldn't build the PDF: {_pdf_exc}")

# On the run that just BUILT the file, the slot is still holding the Export Report button, so
# fill it now. On any later run the download button was already rendered at the top of the page
# and `_pdf_rendered` is True — filling it twice would reuse a widget key.
if st.session_state.get("_rfpis_pdf_bytes") and not _pdf_rendered:
    _pdf_slot.download_button(
        "⬇ Download PDF",
        data=st.session_state["_rfpis_pdf_bytes"],
        file_name=st.session_state.get("_rfpis_pdf_name") or _pdf_name,
        mime="application/pdf", width="stretch", key="report_pdf_download",
        help=(f"{st.session_state.get('_rfpis_pdf_name') or _pdf_name} · "
              f"{len(st.session_state['_rfpis_pdf_bytes']) / 1024:,.0f} KB · "
              f"{_pdf_doc.chart_count} charts · Generate report starts a new export."),
    )

st.divider()


st.success(
    f"✅ **End of report** — {len(_selected_items)} of {len(_ALL_KEYS)} "
    f"metric blocks shown for **{_period_label_str}**. Use "
    f"**Export Report** for a shareable PDF, or **Export Data** for the underlying rows — "
    f"both at the top of the page."
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
        + f" · Generated by **{_gen_by}**"
        + (f" · Report id `{_url_rid}`" if _url_rid else "")
    )
