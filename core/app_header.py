"""App branding + global theme.

Three placements, one helper (`render_app_header`):
  * SIDEBAR TOP    — RFPIS logo via st.logo()
  * MAIN CONTENT   — Deploying-org logo + name, left-aligned strip + divider
  * GLOBAL CSS     — theme variables (primary green), card styles,
                     consistent metric / heading typography

A second helper (`render_sidebar_footer`) is called at the end of every
page render to pin the RFPIS product name + version to the BOTTOM of
the sidebar (below the sign-in / logout block). Splitting the two
matters because Streamlit renders sidebar items in the order they're
added — calling everything at the top would push the caption ABOVE the
sign-in info instead of below it.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from core import settings


APP_NAME = "RFP Intelligence System"
APP_SHORT = "RFPIS"
APP_VERSION = "v1.0"

# Primary brand green — used as the global accent across all metric
# tiles, headings, buttons, and tab underlines. Swap this constant +
# the CSS below to rebrand for a different deployment.
THEME_PRIMARY = "#00703C"      # primary brand green
THEME_PRIMARY_DARK = "#005a30"
THEME_PRIMARY_LIGHT = "#e6f2eb"
THEME_NAVY = "#1e3a8a"          # RFPIS brand color (sidebar logo)
THEME_SLATE = "#475569"
THEME_SLATE_LIGHT = "#94a3b8"
THEME_BG_CARD = "#fafcfa"

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_LOGO_PATH = _ASSETS_DIR / "rfpis_logo.svg"
_ICON_PATH = _ASSETS_DIR / "rfpis_icon.svg"


_GLOBAL_CSS = f"""
<style>
  /* ============================================================
     RFPIS global theme — applied once at the top of every page.
     Primary green, clean typography, consistent metric tiles.
     ============================================================ */

  /* Headings — match existing Home.py style and propagate to every page */
  h1, h2, h3, h4 {{
    color: {THEME_PRIMARY};
    letter-spacing: -0.01em;
  }}
  h1 {{ font-weight: 700; }}
  h2, h3 {{ font-weight: 650; }}

  /* Streamlit subheaders — same green */
  [data-testid="stHeading"] h2,
  [data-testid="stHeading"] h3 {{ color: {THEME_PRIMARY}; }}

  /* Metric tiles — give them card-like presence */
  [data-testid="stMetric"] {{
    background: {THEME_BG_CARD};
    border: 1px solid #e3e7e3;
    border-left: 4px solid {THEME_PRIMARY};
    border-radius: 6px;
    padding: 10px 14px;
  }}
  [data-testid="stMetricLabel"] {{
    color: {THEME_SLATE} !important;
    font-weight: 500;
  }}
  [data-testid="stMetricValue"] {{
    color: {THEME_PRIMARY_DARK} !important;
    font-weight: 700;
  }}

  /* Primary action buttons — use the green */
  [data-testid="stBaseButton-primary"] {{
    background-color: {THEME_PRIMARY} !important;
    border-color: {THEME_PRIMARY} !important;
  }}
  [data-testid="stBaseButton-primary"]:hover {{
    background-color: {THEME_PRIMARY_DARK} !important;
    border-color: {THEME_PRIMARY_DARK} !important;
  }}

  /* Tabs underline — green */
  [data-baseweb="tab-highlight"] {{ background-color: {THEME_PRIMARY} !important; }}
  [data-baseweb="tab"][aria-selected="true"] {{ color: {THEME_PRIMARY} !important; }}

  /* Sidebar tweaks — keep nav-link spacing tight */
  [data-testid="stSidebarNavLink"] {{ padding-top: 4px; padding-bottom: 4px; }}

  /* Quick-start cards on Home — equal height so buttons in adjacent
     columns line up. min-height covers the longest description
     ("Org profile, year setting, Excel sync ..." on the Admin card)
     plus the heading; descriptions stretch via flex so all cards
     render at the same height regardless of text length. */
  .quickcard {{
    border: 1px solid #e3e7e3;
    border-left: 4px solid {THEME_PRIMARY};
    border-radius: 6px;
    padding: 14px 16px;
    background: {THEME_BG_CARD};
    min-height: 9.5rem;
    display: flex;
    flex-direction: column;
    margin-bottom: 0.5rem;
  }}
  .quickcard h4 {{
    margin: 0 0 8px 0;
    font-size: 1rem;
    color: {THEME_PRIMARY};
    font-weight: 650;
  }}
  .quickcard p  {{
    margin: 0;
    color: #475569;
    font-size: 0.88rem;
    line-height: 1.4;
    flex: 1;
  }}

  /* RFPIS sidebar footer brand block — pinned styling so it visually
     belongs to the sidebar even when scrolled. */
  .rfpis-footer-brand {{
    margin-top: 1rem;
    padding: 0.6rem 0.5rem;
    border-top: 1px solid #e3e7e3;
    font-size: 0.78rem;
    color: {THEME_SLATE};
    line-height: 1.3;
  }}
  .rfpis-footer-brand strong {{ color: {THEME_NAVY}; }}
  .rfpis-footer-brand .ver  {{ color: {THEME_SLATE_LIGHT}; }}

  /* ============================================================
     PRINT MODE — make the Report (and any other page with charts)
     fit on a standard letter / A4 landscape PDF without overflow.
     The default Streamlit + Plotly combo lays out for wide screens
     and the right-edge legends get clipped at the print page
     boundary. These rules:
       * Constrain block-container to the printable width.
       * Shrink the in-chart typography so labels still read after
         the page-scale-to-fit kicks in.
       * Tell the browser to avoid breaking a chart across pages.
       * Hide the sidebar / toolbar / buttons (already done in
         views/report.py's own print block; duplicated here so any
         page that prints gets clean output, not just the Report).
     ============================================================ */
  @media print {{
    [data-testid="stSidebar"],
    [data-testid="stToolbar"],
    [data-testid="stHeader"],
    [data-testid="stDecoration"],
    section[data-testid="stSidebarNav"],
    button,
    .stDownloadButton,
    details {{ display: none !important; }}

    .block-container {{
      padding: 0.4rem !important;
      max-width: 100% !important;
    }}

    /* Charts: cap to page width + avoid mid-chart page breaks. */
    [data-testid="stPlotlyChart"],
    .stPlotlyChart {{
      max-width: 100% !important;
      page-break-inside: avoid;
      break-inside: avoid;
    }}
    /* Plotly's internal SVG sizes itself; force the container to
       shrink-to-fit so a long legend doesn't push the chart off-page. */
    .js-plotly-plot, .plot-container {{
      max-width: 100% !important;
      width: 100% !important;
    }}
    /* Shrink in-chart text so first-name labels still render when
       the page-scale-to-fit kicks in. */
    .js-plotly-plot text {{ font-size: 9px !important; }}

    /* Headings / metric tiles: keep with following content. */
    h1, h2, h3, h4 {{ page-break-after: avoid; break-after: avoid; }}
    [data-testid="stMetric"] {{ page-break-inside: avoid; break-inside: avoid; }}

    /* Color-print everything (otherwise green / amber / red bars
       come out grey on most browsers' default print settings). */
    body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
  }}
</style>
"""


def render_app_header() -> None:
    """Top-of-page branding.

    Renders the RFPIS lockup at the top of the sidebar (via st.logo)
    and the deploying-org logo + name as a left-aligned strip at the
    top of the main content area. Also injects the global theme CSS — once
    per page render, before any chart / table renders so colors apply
    consistently.

    Call ONCE per page, immediately after the login gate. Pair with
    `render_sidebar_footer()` at the end of the page so the RFPIS name +
    version sits BELOW the sign-in / logout block instead of above it.
    """
    # ────────────────── Global theme CSS ──────────────────────────────
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)

    # ────────────────── SIDEBAR — RFPIS product brand ─────────────────
    if _LOGO_PATH.exists():
        kwargs: dict = {"image": str(_LOGO_PATH), "size": "large"}
        if _ICON_PATH.exists():
            kwargs["icon_image"] = str(_ICON_PATH)
        try:
            st.logo(**kwargs)
        except Exception:
            with st.sidebar:
                st.image(str(_LOGO_PATH), width=200)
    # Caption (full product name + version) moved to the FOOTER —
    # see render_sidebar_footer().

    # ────────────────── MAIN CONTENT — deploying-org branding ─────────
    try:
        org_bytes, _ = settings.get_org_logo()
    except Exception:
        org_bytes = None

    left, _spacer = st.columns([4, 6], gap="small")
    with left:
        l_icon, l_text = st.columns([1, 4], gap="small")
        with l_icon:
            if org_bytes:
                try:
                    st.image(org_bytes, width=55)
                except Exception:
                    pass
        with l_text:
            st.markdown(
                f"<div style='padding-top:0.65rem; font-weight:600; "
                f"font-size:1rem; color:{THEME_NAVY}; line-height:1.2;'>"
                f"{settings.get_org_name()}</div>",
                unsafe_allow_html=True,
            )

    st.divider()

    # ────────────────── SIDEBAR FOOTER — RFPIS name + version ─────────
    # Rendered as part of render_app_header() because Streamlit places
    # sidebar items in the order they're added. Since every page calls
    # `ensure_logged_in()` BEFORE `render_app_header()`, and ensure_
    # logged_in adds the Sign-in / Logout block, this footer renders
    # AFTER those in the sidebar — sitting at the bottom as the user
    # asked.
    st.sidebar.markdown(
        f"<div class='rfpis-footer-brand'>"
        f"<strong>{APP_NAME}</strong><br>"
        f"<span class='ver'>{APP_SHORT} · {APP_VERSION}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
