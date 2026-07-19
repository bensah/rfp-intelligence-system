"""App branding + global theme.

Three placements, one helper (`render_app_header`):
  * SIDEBAR TOP    — "RFPIS" wordmark (CSS ::before on the sidebar nav)
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
APP_SLOGAN = "Seeks funding seamlessly"   # tagline shown under the app name in the top bar
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
THEME_BG_CARD = "#fafcfa"       # near-white card (metric tiles / quick-cards)
THEME_BORDER = "#e3e7e3"        # hairline border on cards/tiles
# Top app bar (owner 2026-06-29): a full-width DARK GREEN bar — the page itself
# stays white. Tune THEME_HEADER_BG / THEME_HEADER_H to restyle the whole bar.
THEME_HEADER_BG = "#014729"     # deep CHAI green — the top app bar
THEME_HEADER_TEXT = "#ffffff"   # primary org line + icons (on green)
THEME_HEADER_SUB = "#bfe0cf"    # muted light-green — secondary org line
THEME_HEADER_H = "4.6rem"       # bar height (also the content top-offset)

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_LOGO_PATH = _ASSETS_DIR / "rfpis_logo.svg"
_ICON_PATH = _ASSETS_DIR / "rfpis_icon.svg"

# App-logo mark inlined into the top bar's far left (st.image can't render SVG).
# Placeholder — swap assets/rfpis_icon.svg for the real brand asset later.
try:
    _APP_LOGO_SVG = _ICON_PATH.read_text(encoding="utf-8")
except Exception:
    _APP_LOGO_SVG = ""


_GLOBAL_CSS = f"""
<style>
  /* ============================================================
     RFPIS global theme — applied once at the top of every page.
     Primary green, clean typography, consistent metric tiles.
     ============================================================ */

  /* ============================================================
     PAGE-TOP TRIM — single source of truth
     ============================================================
     The vertical real estate above the org-logo strip kept growing
     as new fragments were added across releases. Three culprits
     accumulate from a stock Streamlit page:
       1. `[data-testid="stHeader"]`     — top decoration strip (~3.5rem)
       2. `[data-testid="stDecoration"]` — a thin coloured line below it
       3. `.block-container` padding-top — defaults to ~6rem
     The deploy/menu toolbar (`stToolbar`) lives INSIDE stHeader, so
     collapsing stHeader hides it too — fine for end-users, who never
     need the deploy button. Devs running `streamlit run` locally can
     still access the menu via the keyboard shortcut.

     This block sets every one to a tight, predictable value so the
     header strip starts ~0.4rem under the viewport top and never
     drifts when new code is added elsewhere. */
  [data-testid="stHeader"] {{
    height: 1.5rem !important;
    min-height: 1.5rem !important;
    background: transparent !important;
  }}
  /* NOTE: we no longer hide stToolbar — the st.logo (stHeaderLogo) shares
     that toolbar region, so hiding it removed the logo. The header now has
     height (above) which gives the logo + collapse arrows room. */
  [data-testid="stDecoration"] {{
    display: none !important;
  }}
  .block-container,
  [data-testid="stMainBlockContainer"] {{
    /* Clear the FIXED top app bar (THEME_HEADER_H) so content starts below it. */
    padding-top: calc({THEME_HEADER_H} + 0.5rem) !important;
    padding-bottom: 1rem !important;
    padding-left: 2.5rem !important;
    padding-right: 2.5rem !important;
  }}
  /* (The sidebar's below-the-bar offset is applied once on section[stSidebar]
     further down — keep this inner padding small so it isn't double-counted.) */
  [data-testid="stSidebar"] > div:first-child {{
    padding-top: 0.4rem !important;
  }}
  /* Tighten the divider that separates the header strip from
     content — the default st.divider has hefty top/bottom margins. */
  hr {{
    margin-top: 0.5rem !important;
    margin-bottom: 0.75rem !important;
  }}
  /* Zero out the implicit top margin Streamlit adds to the very
     first element in the main column (often a column wrapper for
     the org-logo strip). */
  [data-testid="stMainBlockContainer"] > div:first-child > div:first-child {{
    margin-top: 0 !important;
    padding-top: 0 !important;
  }}

  /* ── Sidebar top clearance ────────────────────────────────────────
     We zero stHeader above, which also tucks the sidebar's top control
     row under the viewport edge. Pad the sidebar header so the logo +
     collapse « clear the top, and nudge the floating » expand button
     (shown when the sidebar is hidden) down + right out of the corner. */
  [data-testid="stSidebarHeader"] {{
    padding-top: 0.2rem !important;
  }}
  /* Floating » control (mobile hamburger; also any version that renders it on
     desktop): keep it above the fixed top bar and clickable. */
  [data-testid="stExpandSidebarButton"] {{
    top: calc({THEME_HEADER_H} + 0.4rem) !important;   /* below the fixed app bar */
    left: 0.6rem !important;
    z-index: 1000001 !important;                        /* above the fixed top bar */
    pointer-events: auto !important;
  }}

  /* ── Click-sticky icon rail ───────────────────────────────────────
     The native collapse button is the toggle, and Streamlit persists
     the open/closed choice in localStorage — so it stays where you
     click it (no hover). We restyle the COLLAPSED state (which normally
     slides the whole sidebar off-screen) into a visible narrow rail of
     page icons; EXPANDED is the full sidebar with labels. App.py sets
     initial_sidebar_state="collapsed" so a fresh launch shows the rail. */
  section[data-testid="stSidebar"][aria-expanded="false"] {{
    transform: none !important;
    margin-left: 0 !important;
    visibility: visible !important;
    width: 4.2rem !important;
    min-width: 4.2rem !important;
  }}
  /* Rail: clip nav labels; hide the user block (signed-in / logout /
     footer) and the wide logo — all return in the expanded state. */
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarContent"] {{
    overflow-x: hidden !important;
  }}
  /* Rail nav links: center the icon, hide the label text (the icon is an
     stIconEmoji, the label is markdown — hide only the markdown), and
     render each link as a padded rounded block so the active highlight
     wraps the icon neatly. */
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNavLink"] {{
    display: flex !important;
    box-sizing: border-box !important;
    justify-content: center !important;
    white-space: nowrap !important;
    margin: 0.15rem 0 !important;
    padding: 0.55rem 0 !important;
    border-radius: 8px !important;
  }}
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNavLink"] [data-testid="stMarkdownContainer"] {{
    display: none !important;
  }}
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarUserContent"] {{
    display: none !important;
  }}
  /* Bigger page icons (rail + expanded). */
  section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"] [data-testid="stIconEmoji"] {{
    font-size: 1.5rem !important;
    line-height: 1 !important;
  }}
  /* Keep nav links full-width so the highlight + hit-area span the whole
     row (margin to margin), not just behind the icon/label. */
  section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"] {{
    width: 100% !important;
    box-sizing: border-box !important;
  }}
  /* Active-page highlight: fill the entire nav row in both states. Paint
     the container (full row) AND the link, then clear Streamlit's small
     default grey box on the inner icon/label so only the brand block shows. */
  section[data-testid="stSidebar"] [data-testid="stSidebarNavLinkContainer"]:has([aria-current="page"]),
  section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"][aria-current="page"] {{
    background: rgba(0, 112, 60, 0.16) !important;
    border-radius: 8px !important;
  }}
  section[data-testid="stSidebar"] [data-testid="stSidebarNavLink"][aria-current="page"] > * {{
    background: transparent !important;
  }}
  /* Make the nav-item container full-width and give the items list a small
     symmetric inset, so the active highlight spans the whole row (margin to
     margin) rather than hugging the icon/label. */
  section[data-testid="stSidebar"] [data-testid="stSidebarNavLinkContainer"] {{
    width: 100% !important;
  }}
  section[data-testid="stSidebar"] [data-testid="stSidebarNavItems"] {{
    padding-left: 0.4rem !important;
    padding-right: 0.4rem !important;
  }}
  /* (The "RFPIS" sidebar wordmark was removed — app branding now lives in the
     top bar. Pull the nav up tight, right under the bar.) */
  [data-testid="stSidebarNav"] {{
    margin-top: 0 !important;
    padding-top: 0 !important;
  }}
  /* Collapse/expand arrows follow the rule WIDE → « (collapse) / NARROW →
     » (expand) — which is already Streamlit's native direction, so we flip
     NOTHING. We only hide the redundant in-sidebar collapse « while in the
     rail; the floating » expand control is the one that belongs there, so
     the narrow view shows a single, correctly-pointing (») arrow. */
  /* Collapsed rail: KEEP the sidebar's own collapse/expand toggle visible. It lives
     in the sidebar header, just below the top bar — the SAME control used to collapse.
     It was previously hidden here in favour of Streamlit's floating » button, but that
     button only renders when the sidebar is slid fully OFF-screen; our rail keeps the
     sidebar ON-screen, so no floating button ever appeared and a collapsed sidebar
     could not be re-opened. Keeping this toggle visible restores the expand affordance
     right where the user collapsed it. Lift it above the fixed top bar so it stays
     clickable, and centre it in the narrow rail. */
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"] {{
    display: flex !important;
    visibility: visible !important;
    opacity: 1 !important;
    pointer-events: auto !important;
    z-index: 1000001 !important;
    margin-left: auto !important;
    margin-right: auto !important;
  }}
  /* …but the icon still points « (collapse). Because we reuse the collapse toggle
     rather than Streamlit's native expand button, its glyph never flips. In the
     collapsed rail the button EXPANDS, so mirror the chevron to point » (outward). */
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"] [data-testid="stIconMaterial"],
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"] svg {{
    transform: scaleX(-1) !important;
  }}

  /* ── Hover-to-peek labels on the collapsed rail ───────────────────
     Mousing over a rail icon reveals its page name as a flyout to the right
     (no reflow — the layout doesn't move). The label markdown is hidden by
     default (rule above); on hover we float it out of the rail. Let the flyout
     escape the rail's clipping (the rail has few items, so dropping the scroll
     overflow is harmless). */
  section[data-testid="stSidebar"][aria-expanded="false"],
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarContent"],
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNav"],
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNavItems"] {{
    overflow: visible !important;
  }}
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNavLink"] {{
    position: relative !important;
  }}
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNavLink"]:hover [data-testid="stMarkdownContainer"] {{
    display: block !important;
    position: absolute !important;
    left: calc(100% + 0.35rem) !important;
    top: 50% !important;
    transform: translateY(-50%) !important;
    white-space: nowrap !important;
    background: {THEME_HEADER_BG} !important;
    color: {THEME_HEADER_TEXT} !important;
    padding: 0.3rem 0.65rem !important;
    border-radius: 6px !important;
    box-shadow: 0 3px 12px rgba(0, 0, 0, 0.28) !important;
    z-index: 1000002 !important;         /* above the rail AND the top bar */
    pointer-events: none !important;
    font-size: 0.85rem !important;
    font-weight: 600 !important;
    line-height: 1.2 !important;
  }}

  /* Headings — match existing Home-page style and propagate to every page */
  h1, h2, h3, h4 {{
    color: {THEME_PRIMARY};
    letter-spacing: -0.01em;
  }}
  h1 {{ font-weight: 700; }}
  h2, h3 {{ font-weight: 650; }}

  /* Streamlit subheaders — same green */
  [data-testid="stHeading"] h2,
  [data-testid="stHeading"] h3 {{ color: {THEME_PRIMARY}; }}

  /* ── Full-viewport dark-green top app bar ─────────────────────────
     position:FIXED (not sticky) pins it flush to the very top of the
     VIEWPORT and spans the whole width (left:0;right:0) — so there is no
     white strip above it (it covers Streamlit's stHeader) and no gap at
     the right edge. The app content + sidebar are pushed down by the bar
     height (see .block-container / stSidebar padding-top above). Tune via
     THEME_HEADER_BG / THEME_HEADER_H. */
  [class*="st-key-rfpis_topbar"] {{
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    right: 0 !important;
    z-index: 1000000 !important;          /* ABOVE the sidebar so the bar sits in front of it */
    background: {THEME_HEADER_BG} !important;
    min-height: {THEME_HEADER_H} !important;
    display: flex !important;
    align-items: center !important;
    margin: 0 !important;
    padding: 0.25rem 1.6rem !important;
    box-shadow: 0 2px 8px -2px rgba(0, 0, 0, 0.40) !important;
  }}
  /* Vertically CENTRE the bar's content (icons + brand) within the taller bar.
     Streamlit's inner vertical block stretches to the full bar height and would
     otherwise top-align its row; justify-content:center pulls the row to the
     middle, and align-items:center lines the columns up on that midline. */
  [class*="st-key-rfpis_topbar"] [data-testid="stVerticalBlock"] {{
    justify-content: center !important;
    height: 100% !important;
  }}
  [class*="st-key-rfpis_topbar"] [data-testid="stHorizontalBlock"] {{
    width: 100% !important;
    align-items: center !important;
    flex-wrap: nowrap !important;
  }}
  /* ── Brand cluster (far-left): app-logo badge + tenant name ──────── */
  [class*="st-key-rfpis_topbar"] .rfpis-brand {{
    display: flex !important;
    align-items: center !important;
    gap: 0.9rem !important;
    padding-left: 0.4rem !important;
  }}
  /* App logo in a small white badge so the (navy/amber) mark reads on green. */
  [class*="st-key-rfpis_topbar"] .rfpis-applogo {{
    background: #ffffff !important;
    border-radius: 9px !important;
    padding: 4px !important;
    line-height: 0 !important;
    flex: 0 0 auto !important;
  }}
  [class*="st-key-rfpis_topbar"] .rfpis-applogo svg {{
    height: 2.5rem !important;
    width: 2.5rem !important;
    display: block !important;
  }}
  /* Tenant name — primary white, secondary muted; nudged right off the edge. */
  [class*="st-key-rfpis_topbar"] .rfpis-org-name {{
    display: flex !important;
    flex-direction: column !important;
    line-height: 1.2 !important;
  }}
  [class*="st-key-rfpis_topbar"] .rfpis-org-primary {{ color: {THEME_HEADER_TEXT} !important; }}
  [class*="st-key-rfpis_topbar"] .rfpis-org-sub {{ color: {THEME_HEADER_SUB} !important; }}
  /* Search / bell / user popover triggers — NO chip: transparent button, white
     Material glyphs that read cleanly on the dark green. */
  [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button {{
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0.25rem 0.35rem !important;   /* tight — bring the icons close together */
    margin-top: 0.7rem !important;         /* step the icons down to sit lower in the bar */
  }}
  [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button,
  [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button * {{
    color: #ffffff !important;
    fill: #ffffff !important;
  }}
  /* Remove the dropdown chevron. The label is now a plain EMOJI (text), so the
     ONLY Material-icon / svg in the button is Streamlit's chevron — hide both
     forms to be safe. */
  [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button [data-testid="stIconMaterial"],
  [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button svg {{
    display: none !important;
  }}
  /* Enlarge the emoji glyph AND render it MONOCHROME WHITE: brightness(0) turns
     the colour emoji solid black, invert(1) flips it to white — a uniform, high-
     contrast, colour-blind-safe icon (fixes the low-contrast purple 👤). The bell's
     red count badge is a ::after pseudo-element (not inside <p>), so the filter
     doesn't touch it. */
  [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button p,
  [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button [data-testid="stMarkdownContainer"] {{
    font-size: 1.7rem !important;
    line-height: 1 !important;
    margin: 0 !important;
    filter: brightness(0) invert(1) !important;
  }}
  /* Cluster the icons tight on the right: the brand column grows, the three icon
     columns shrink to their content so they sit next to each other (not spread one
     per wide cell). */
  [class*="st-key-rfpis_topbar"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child {{
    flex: 1 1 auto !important;
  }}
  [class*="st-key-rfpis_topbar"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:not(:first-child) {{
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: 0 !important;
  }}
  /* SEARCH icon (2nd column) — replace the solid white magnifier blob with an OUTLINE
     magnifier whose lens is hollow (the dark green shows through the centre). Hide the
     emoji glyph and paint a white-stroke / no-fill SVG via ::before. */
  [class*="st-key-rfpis_topbar"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2) [data-testid="stPopover"] button p {{
    font-size: 0 !important;
    filter: none !important;
  }}
  [class*="st-key-rfpis_topbar"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2) [data-testid="stPopover"] button p::before {{
    content: "";
    display: inline-block;
    width: 1.6rem;
    height: 1.6rem;
    vertical-align: middle;
    background: url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20fill='none'%20stroke='white'%20stroke-width='2.4'%20stroke-linecap='round'%3E%3Ccircle%20cx='10.5'%20cy='10.5'%20r='6.5'/%3E%3Cline%20x1='15.2'%20y1='15.2'%20x2='21'%20y2='21'/%3E%3C/svg%3E") center / contain no-repeat;
  }}
  [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button:hover {{
    background: rgba(255, 255, 255, 0.16) !important;
    border-radius: 8px !important;
  }}
  /* The bar now sits IN FRONT of the sidebar (z-index above), so push the whole
     sidebar's contents (collapse toggle + nav) down below the bar — otherwise the
     bar would cover them. Matches the main content offset (block-container). */
  section[data-testid="stSidebar"] {{
    padding-top: {THEME_HEADER_H} !important;
  }}

  /* ── Tenant (org) identity — top-right, just below the app bar ────── */
  .rfpis-orgid {{
    display: flex !important;
    align-items: center !important;
    justify-content: flex-end !important;
    gap: 0.55rem !important;
    margin: -0.3rem 0 0.5rem 0 !important;
  }}
  .rfpis-orgid-logo {{
    height: 38px !important;
    width: auto !important;
    border-radius: 6px !important;
  }}
  .rfpis-orgid-text {{ text-align: right !important; line-height: 1.18 !important; }}
  .rfpis-orgid-name {{ font-weight: 700 !important; font-size: 0.92rem !important;
                      color: {THEME_NAVY} !important; }}
  .rfpis-orgid-sub {{ font-size: 0.76rem !important; color: {THEME_SLATE} !important; }}

  /* Metric tiles — give them card-like presence */
  [data-testid="stMetric"] {{
    background: {THEME_BG_CARD};
    border: 1px solid {THEME_BORDER};
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
    border: 1px solid {THEME_BORDER};
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
    border-top: 1px solid {THEME_BORDER};
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

  /* ============================================================
     MOBILE — responsive pass for narrow screens / phones.
     Safe, additive rules (only apply <= 640px) so the desktop
     layout is untouched. Targets the worst small-screen issues:
     page padding, wide tables, overflowing tab bars, tap targets,
     oversized titles, and stacking cramped column rows.
     ============================================================ */
  @media (max-width: 640px) {{
    .block-container,
    [data-testid="stMainBlockContainer"] {{
      padding-left: 0.7rem !important;
      padding-right: 0.7rem !important;
      max-width: 100% !important;
    }}
    [data-testid="stAppViewContainer"] {{ overflow-x: hidden; }}

    /* ── Sidebar: drop the desktop icon-rail on phones ──────────────────
       The collapsed sidebar slides FULLY off-screen (Streamlit's native
       mobile behaviour) so it never sits on top of the content; the ☰/»
       control re-opens it as an overlay. Overrides the desktop rail rules
       above (same selector, later + !important → wins at this width). */
    section[data-testid="stSidebar"][aria-expanded="false"] {{
      transform: translateX(-100%) !important;
      width: 0 !important;
      min-width: 0 !important;
      margin-left: 0 !important;
      visibility: hidden !important;
    }}
    [data-testid="stExpandSidebarButton"] {{
      visibility: visible !important;
      top: 0.4rem !important;
      left: 0.4rem !important;
    }}

    /* Wide tables scroll inside their own box. */
    [data-testid="stDataFrame"],
    [data-testid="stDataFrameResizable"],
    [data-testid="stTable"] {{ overflow-x: auto !important; }}

    /* Tab bars scroll horizontally instead of clipping. */
    [data-baseweb="tab-list"] {{
      overflow-x: auto !important;
      flex-wrap: nowrap !important;
    }}

    /* Stack cramped side-by-side columns into one readable column. */
    [data-testid="stHorizontalBlock"] {{ flex-wrap: wrap !important; }}
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"],
    [data-testid="stHorizontalBlock"] > [data-testid="column"] {{
      min-width: 11rem !important;
      flex: 1 1 11rem !important;
    }}

    /* ── Top bar stays horizontal + borderless icons ───────────────────
       The generic column-stack above would wrap the 🔍🔔👤 icons into
       full-width boxes; these rules (later → win) keep the top bar in one
       row and strip the box around each popover trigger. */
    [class*="st-key-rfpis_topbar"] [data-testid="stHorizontalBlock"] {{
      flex-wrap: nowrap !important;
      align-items: center !important;
      gap: 0.1rem !important;
    }}
    [class*="st-key-rfpis_topbar"] [data-testid="stColumn"],
    [class*="st-key-rfpis_topbar"] [data-testid="column"] {{
      min-width: 0 !important;
      flex: 0 1 auto !important;
    }}
    [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button {{
      border: none !important;
      background: transparent !important;
      box-shadow: none !important;
      min-width: 0 !important;
      padding: 0.2rem 0.3rem !important;
      white-space: nowrap !important;
    }}
    /* Smaller glyph on phones (the desktop 1.7rem would overlap), still white. */
    [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button p {{
      white-space: nowrap !important;
      font-size: 1.45rem !important;
      filter: brightness(0) invert(1) !important;
    }}
    /* Drop the popover dropdown carets in the top bar (declutter the icons). */
    [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button [data-testid="stIconMaterial"] {{
      display: none !important;
    }}
    /* Compact org name so it doesn't wrap to three lines next to the logo. */
    [class*="st-key-rfpis_topbar"] [data-testid="stMarkdownContainer"] > div {{
      font-size: 0.82rem !important;
      line-height: 1.15 !important;
      padding-top: 0 !important;
    }}

    /* ── Sidebar toggle shown as a hamburger (☰) on phones ─────────────
       Streamlit renders the expand control as the <button
       data-testid="stExpandSidebarButton"> ITSELF — there is no nested
       <button>. Its »/chevron is a Material Symbols glyph in a child
       [data-testid="stIconMaterial"] span. Earlier rules scoped to
       "...Button button ..." matched nothing (no inner button), so the »
       survived. Target the testid element directly: hide the glyph span,
       zero its font, and draw ☰ via ::after. */
    [data-testid="stExpandSidebarButton"] [data-testid="stIconMaterial"],
    [data-testid="stExpandSidebarButton"] span,
    [data-testid="stExpandSidebarButton"] svg {{ display: none !important; }}
    [data-testid="stExpandSidebarButton"] {{ font-size: 0 !important; }}
    [data-testid="stExpandSidebarButton"]::after {{
      content: "☰";
      font-size: 1.6rem !important;
      line-height: 1;
      color: {THEME_NAVY};
    }}

    /* "Where to start" cards stack one per row (no lopsided 2+2+1 wrap). */
    [data-testid="stColumn"]:has(.quickcard) {{
      min-width: 100% !important;
      flex: 1 1 100% !important;
    }}

    /* Comfortable tap targets. */
    [data-testid="stBaseButton-primary"],
    [data-testid="stBaseButton-secondary"] {{ min-height: 2.4rem; }}

    /* ── Headings scale with the viewport ──────────────────────────────
       h1 = page title, h3 = in-tab st.subheader, h4 = mini-labels.
       Without the h3/h4 rules, rows like "Notes for Week 24 (8 Jun –
       14 Jun) · 0 record(s)" rendered at desktop size and overflowed
       the phone width. */
    h1, [data-testid="stHeadingWithActionElements"] h1,
    [data-testid="stHeading"] h1 {{
      font-size: 1.25rem !important; line-height: 1.2 !important;
    }}
    h2, [data-testid="stHeading"] h2 {{
      font-size: 1.1rem !important; line-height: 1.2 !important;
    }}
    h3, [data-testid="stHeading"] h3 {{
      font-size: 1.0rem !important; line-height: 1.25 !important;
    }}
    h4, [data-testid="stHeading"] h4 {{ font-size: 0.92rem !important; }}

    /* ── Metric tiles shrink to fit ─────────────────────────────────────
       Values like "Mon 08 Jun" / "Pokam Ornella" were truncating to
       "Mon 08 Ju…" / "Pokam Or…". Drop the value font, tighten padding,
       and let the value wrap instead of clipping with an ellipsis. */
    [data-testid="stMetric"] {{ padding: 8px 10px !important; }}
    [data-testid="stMetricLabel"] {{ font-size: 0.72rem !important; }}
    [data-testid="stMetricValue"] {{
      font-size: 1.05rem !important;
      line-height: 1.2 !important;
      white-space: normal !important;
      overflow-wrap: anywhere !important;
    }}
    [data-testid="stMetricValue"] > div {{
      white-space: normal !important;
      overflow: visible !important;
      text-overflow: clip !important;
    }}

    /* Button label text scales too (the "+ Add a note" CTA shouldn't
       dominate a narrow row). */
    [data-testid="stBaseButton-primary"] p,
    [data-testid="stBaseButton-secondary"] p {{ font-size: 0.9rem !important; }}
  }}
</style>
"""


def _render_user_menu() -> None:
    """Top-right person-icon dropdown — Help, Profile, Settings (admins),
    Sign Out. Introduced in the 2026-06-07 redesign; replaces the sidebar
    Logout button. Rendered on every page via render_app_header().

    Sign Out uses streamlit-authenticator's logout (deletes the auth cookie
    + clears its auth state). A `callback` ALSO clears our own `app_user`
    session cache — without it the cookie would be gone but ensure_logged_in()
    would still short-circuit on the cached user and appear logged in.
    """
    from core import permissions as _perms  # local import — avoid cycle
    from auth.authenticator import get_authenticator  # local — avoid cycle

    u = st.session_state.get("app_user") or {}
    if not u:
        return

    with st.popover("👤", width='stretch'):
        st.markdown(
            f"<div style='font-weight:650;color:{THEME_NAVY};"
            f"font-size:0.95rem;'>{u.get('name') or u.get('email')}</div>"
            f"<div style='font-size:0.78rem;color:{THEME_SLATE};'>"
            f"{u.get('email','')}</div>",
            unsafe_allow_html=True,
        )
        st.divider()
        st.page_link("app_pages/help.py", label="Help", icon="❓")
        st.page_link("app_pages/organization.py", label="Organization", icon="🏢")
        st.page_link("app_pages/profile.py", label="Profile", icon="👤")
        if _perms.is_admin(u):
            st.page_link("app_pages/admin.py", label="Settings", icon="⚙️")
        st.divider()
        if st.button("🚪 Sign Out", key="topbar_signout",
                     width='stretch'):
            # The cookie reader (CookieModel.get_cookie) restores a session
            # from st.context.cookies UNLESS st.session_state['logout'] is
            # True. That request-snapshot doesn't update mid-session, so the
            # earlier "delete the cookie + clear keys" approach left the gate
            # free to log us straight back in — which is the bug you saw.
            # Run the library's unrendered logout (which sets logout=True,
            # clears its auth keys, and deletes the browser cookie via JS),
            # set the flag explicitly as belt-and-suspenders, clear our own
            # session cache, then rerun into the login gate.
            try:
                get_authenticator().logout(location="unrendered")
            except Exception:
                pass
            st.session_state["logout"] = True
            for k in ("app_user", "_post_login_nav_synced",
                      "_auth_cookie_settled", "authentication_status",
                      "name", "username"):
                st.session_state.pop(k, None)
            st.rerun()


def _render_search(user: dict) -> None:
    """Top-right 🔍 search box. Captures the query and navigates to the
    dedicated results page (app_pages/search.py) — a search-engine-style
    flow: type a keyword, land on a results list, click through. Rendered on
    every page via render_app_header().

    Navigation (st.switch_page) is called AFTER the `with st.popover()` block —
    calling it inside the popover/form didn't navigate reliably. A plain
    text_input + button is used instead of st.form (forms inside popovers were
    flaky). The query is also written to the URL (?q=) so the results page
    reproduces it, and a page_link is kept as a guaranteed fallback."""
    submitted_q = None
    with st.popover("🔍", width='stretch'):
        q = st.text_input(
            "Search", key="hdr_search_q",
            placeholder="Search the site…", label_visibility="collapsed")
        go = st.button("Search", key="hdr_search_go", type="primary",
                       width='stretch')
        st.caption("Searches pages, tabs, opportunities, donors + the web.")
        # Guaranteed-working fallback (page_link always navigates; the results
        # page has its own search box).
        st.page_link("app_pages/search.py", label="Open full search page",
                     icon="🔍")
    if go and (q or "").strip():
        submitted_q = q.strip()
    if submitted_q:
        st.session_state["site_search_query"] = submitted_q
        st.query_params["q"] = submitted_q
        st.switch_page("app_pages/search.py")


def _render_notifications(user: dict) -> None:
    """Top-right 🔔 notification bell — org-wide activity feed (scans + new
    opportunities) with a per-user unread badge. Rendered on every page."""
    from core import notifications as _notif

    email = (user or {}).get("email") or ""
    try:
        feed = _notif.recent_feed()
    except Exception:
        feed = []
    seen = _notif.last_seen(email) if email else None
    unread = _notif.unread_count(feed, seen) if email else 0
    # Just the bell here — the unread-count badge is injected by render_app_header
    # OUTSIDE the icon columns (a stable nth-child selector), so it adds no element to
    # this column and the bell stays vertically aligned with the search/profile icons.
    _render_notifications_popover(_notif, feed, seen, email, unread)
    return unread


def _render_notifications_popover(_notif, feed, seen, email, unread) -> None:
    with st.popover("🔔", width='stretch'):
        st.markdown("**Notifications**")
        nxt = _notif.next_scheduled_scan()
        st.caption(
            f"⏰ Next auto-scan · {nxt.strftime('%a %d %b, %H:%M UTC')} "
            f"({_notif.relative_time(nxt)})")
        # Next Monday check-in call (persistent, under the scan line).
        try:
            from datetime import date as _date
            from core import schedule as _sched
            _mtg = _sched.next_meeting()
        except Exception:
            _mtg = None
        if _mtg:
            _md = _date.fromisoformat(_mtg["date"])
            _roles = "; ".join(
                f"{lbl}: {(_mtg.get(k) or '—')}"
                for lbl, k in (("Note-taker", "note_taker"),
                               ("Presenter", "presenter"), ("Chair", "chair")))
            st.caption(f"📅 Next call · {_md.strftime('%a %d %b')} · {_roles}")
        st.divider()
        if not feed:
            st.caption("No recent activity yet.")
        else:
            for it in feed[:15]:
                is_new = (seen is None) or (it["ts"] and it["ts"] > seen)
                dot = "🟢 " if is_new else ""
                st.markdown(
                    f"{dot}{it['icon']} **{it['title']}** "
                    f"<span style='color:{THEME_SLATE_LIGHT};font-size:0.78rem'>"
                    f"· {_notif.relative_time(it['ts'])}</span><br>"
                    f"<span style='color:{THEME_SLATE};font-size:0.85rem'>"
                    f"{it['detail']}</span>",
                    unsafe_allow_html=True)
        st.divider()
        if email and unread:
            if st.button("Mark all as read", key="notif_mark_read",
                         width='stretch'):
                _notif.mark_all_read(email)
                st.rerun()


def _render_org_identity() -> None:
    """Tenant (org) identity — the logo + name from the Org profile — rendered
    top-RIGHT, just below the app bar. Deliberately separate from the APP branding
    inside the bar (app logo + RFPIS + slogan): this is the deployment's own logo,
    uploaded by a user in Org setup, and changes per tenant."""
    import base64
    try:
        logo_bytes, logo_mime = settings.get_org_logo()
    except Exception:
        logo_bytes, logo_mime = None, None
    name_full = settings.get_org_name() or ""
    if " — " in name_full:
        primary, secondary = name_full.split(" — ", 1)
    else:
        primary, secondary = name_full, ""
    img_html = ""
    if logo_bytes:
        b64 = base64.b64encode(logo_bytes).decode()
        img_html = (f"<img class='rfpis-orgid-logo' "
                    f"src='data:{logo_mime or 'image/png'};base64,{b64}'/>")
    sub = f"<div class='rfpis-orgid-sub'>{secondary}</div>" if secondary else ""
    st.markdown(
        f"<div class='rfpis-orgid'>{img_html}"
        f"<div class='rfpis-orgid-text'>"
        f"<div class='rfpis-orgid-name'>{primary}</div>{sub}</div></div>",
        unsafe_allow_html=True)


def render_app_header() -> None:
    """Top-of-page branding.

    Renders the "RFPIS" wordmark at the top of the sidebar (via the CSS
    ::before in _GLOBAL_CSS — no image asset) and the deploying-org logo +
    name as a left-aligned strip at the
    top of the main content area. Also injects the global theme CSS — once
    per page render, before any chart / table renders so colors apply
    consistently.

    Call ONCE per page, immediately after the login gate. Pair with
    `render_sidebar_footer()` at the end of the page so the RFPIS name +
    version sits BELOW the sign-in / logout block instead of above it.
    """
    # ────────────────── Global theme CSS ──────────────────────────────
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)

    # ────────────────── Hide user-menu pages from the sidebar ─────────
    # Profile / Help / Settings are registered with st.navigation (so they
    # have stable URLs and are reachable via st.page_link), but as of the
    # 2026-06-07 redesign they live in the top-right user menu, NOT the
    # sidebar rail. Hide their sidebar nav links by URL-slug suffix. The
    # `i` flag makes the match case-insensitive, so it works whether
    # Streamlit derives the href from the url_path (lowercase) or the page
    # title (capitalised). Settings is additionally omitted from the nav
    # entirely for non-admins (App.py) and its own page guard rejects deep
    # links — this CSS just keeps the rail clean for everyone.
    st.markdown(
        """
        <style>
          [data-testid="stSidebarNav"] a[href$="/profile" i],
          [data-testid="stSidebarNav"] a[href$="/help" i],
          [data-testid="stSidebarNav"] a[href$="/search" i],
          [data-testid="stSidebarNav"] a[href$="/settings" i],
          section[data-testid="stSidebar"] a[href$="/profile" i],
          section[data-testid="stSidebar"] a[href$="/help" i],
          section[data-testid="stSidebar"] a[href$="/search" i],
          section[data-testid="stSidebar"] a[href$="/settings" i] {
            display: none !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ────────────────── SIDEBAR — RFPIS wordmark ─────────────────────
    # No graphic logo. The "RFPIS" wordmark is drawn at the top of the
    # sidebar nav via CSS (::before on stSidebarNav in _GLOBAL_CSS), so it
    # shows in BOTH the expanded and the collapsed icon-rail states without
    # an image asset. The 🏠 Home nav item directly below it goes home.

    # ────────────────── MAIN CONTENT — deploying-org branding ─────────
    _u = st.session_state.get("app_user") or {}
    # Keyed container so the mobile CSS can target the top bar specifically
    # (keep it horizontal + borderless icons) without affecting other columns.
    with st.container(key="rfpis_topbar"):
        # Flat columns (no nesting → mobile-friendly). CSS makes `left` grow and the
        # three icon columns shrink to content + cluster tight on the right.
        left, c_search, c_bell, c_user = st.columns([6, 1, 1, 1], gap="small")
        with left:
            # APP branding (NOT the tenant): app logo badge + app name "RFPIS" and
            # the slogan in italics. The tenant/org identity is rendered separately,
            # below the bar (see _render_org_identity).
            st.markdown(
                f"<div class='rfpis-brand'>"
                f"<span class='rfpis-applogo'>{_APP_LOGO_SVG}</span>"
                f"<span class='rfpis-org-name'>"
                f"<span class='rfpis-org-primary' style='font-weight:700; "
                f"font-size:1.15rem;'>{APP_SHORT}</span>"
                f"<span class='rfpis-org-sub' style='font-style:italic; "
                f"font-weight:500; font-size:0.82rem;'>{APP_SLOGAN}</span>"
                f"</span></div>",
                unsafe_allow_html=True)
        with c_search:
            _render_search(_u)
        with c_bell:
            _bell_unread = _render_notifications(_u)
        with c_user:
            _render_user_menu()
        # Bell count badge — injected here (after the columns, NOT inside the bell
        # column) so it adds no element that would shift the bell out of line. The
        # bell is the 3rd column (brand · search · bell · profile); target it by
        # position and hang a small red badge in its top-right corner.
        if _bell_unread:
            _bcnt = str(_bell_unread) if _bell_unread < 100 else "99+"
            st.markdown(
                "<style>"
                "[class*='st-key-rfpis_topbar'] [data-testid='stHorizontalBlock'] >"
                " [data-testid='stColumn']:nth-child(3) [data-testid='stPopover'] button"
                " { position: relative !important; }"
                "[class*='st-key-rfpis_topbar'] [data-testid='stHorizontalBlock'] >"
                " [data-testid='stColumn']:nth-child(3) [data-testid='stPopover'] button::after {"
                f" content: '{_bcnt}'; position: absolute; top: -3px; right: -5px;"
                " background: #dc2626; color: #ffffff !important; font-size: 0.6rem;"
                " font-weight: 700; line-height: 1; padding: 2px 5px; border-radius: 9px;"
                " font-style: normal; }"
                "</style>",
                unsafe_allow_html=True)

    # Tenant (org) identity — moved OUT of the bar to the top-right, just below it.
    _render_org_identity()

    # No st.divider() here — the pinned top bar carries its own bottom
    # border (see `.st-key-rfpis_topbar` in _GLOBAL_CSS). A separate
    # divider would scroll away from the sticky header and leave a gap.

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
