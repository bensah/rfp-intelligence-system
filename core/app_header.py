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
    padding-top: 0.8rem !important;
    padding-bottom: 1rem !important;
  }}
  [data-testid="stSidebar"] > div:first-child {{
    padding-top: 0.6rem !important;
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
  [data-testid="stExpandSidebarButton"] {{
    top: 0.5rem !important;
    left: 0.6rem !important;
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
  /* "RFPIS" wordmark at the top of the sidebar nav — replaces the graphic
     logo and stays visible in both the expanded and the collapsed rail
     states. (A CSS ::before isn't clickable; the 🏠 Home item right below
     navigates home.) */
  [data-testid="stSidebarNav"]::before {{
    content: "RFPIS";
    display: block;
    font-weight: 700;
    font-size: 1.15rem;
    letter-spacing: 0.04em;
    color: {THEME_NAVY};
    padding: 0 0.85rem 0.55rem;
    margin-top: -0.9rem;
  }}
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarNav"]::before {{
    font-size: 0.72rem;
    padding: 0 0 0.5rem;
    text-align: center;
  }}
  /* Collapse/expand arrows follow the rule WIDE → « (collapse) / NARROW →
     » (expand) — which is already Streamlit's native direction, so we flip
     NOTHING. We only hide the redundant in-sidebar collapse « while in the
     rail; the floating » expand control is the one that belongs there, so
     the narrow view shows a single, correctly-pointing (») arrow. */
  section[data-testid="stSidebar"][aria-expanded="false"] [data-testid="stSidebarCollapseButton"] {{
    display: none !important;
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

  /* ── Pinned top app bar ───────────────────────────────────────────
     Keep the org logo/name + search/bell/user icons visible while the
     page body scrolls. White backdrop + hairline + soft shadow so the
     content scrolling beneath doesn't bleed through. This replaces the
     old st.divider under the header (removed from render_app_header). */
  [class*="st-key-rfpis_topbar"] {{
    position: sticky;
    top: 0;
    z-index: 1000;
    background: #ffffff;
    padding-top: 0.2rem;
    padding-bottom: 0.45rem;   /* keep the org name clear of the border line */
    margin-bottom: 0.4rem;
    border-bottom: 1px solid #e3e7e3;
    box-shadow: 0 3px 6px -4px rgba(0, 0, 0, 0.25);
  }}

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
      padding: 0.25rem 0.35rem !important;
      font-size: 1.3rem !important;
      white-space: nowrap !important;   /* keep 🔔¹² on one line → icons align */
    }}
    [class*="st-key-rfpis_topbar"] [data-testid="stPopover"] button p {{
      white-space: nowrap !important;
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
    # Render the count as small SUPERSCRIPT digits so it hangs on the bell like
    # a badge and stays on ONE line — keeping the top-bar icons aligned (a
    # plain " 12" wrapped to a 2nd line and pushed the bell out of line).
    _cnt = str(unread) if unread < 100 else "99+"
    _sup = _cnt.translate(str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"))
    label = f"🔔{_sup}" if unread else "🔔"

    with st.popover(label, width='stretch'):
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
          [data-testid="stSidebarNav"] a[href$="/organization" i],
          [data-testid="stSidebarNav"] a[href$="/submit-new-rfp" i],
          [data-testid="stSidebarNav"] a[href$="/help" i],
          [data-testid="stSidebarNav"] a[href$="/search" i],
          [data-testid="stSidebarNav"] a[href$="/settings" i],
          section[data-testid="stSidebar"] a[href$="/profile" i],
          section[data-testid="stSidebar"] a[href$="/organization" i],
          section[data-testid="stSidebar"] a[href$="/submit-new-rfp" i],
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
    try:
        org_bytes, _ = settings.get_org_logo()
    except Exception:
        org_bytes = None

    _u = st.session_state.get("app_user") or {}
    # Keyed container so the mobile CSS can target the top bar specifically
    # (keep it horizontal + borderless icons) without affecting other columns.
    with st.container(key="rfpis_topbar"):
        left, _spacer, c_search, c_bell, c_user = st.columns(
            [5, 3, 1, 1, 1], gap="small")
        with c_search:
            _render_search(_u)
        with c_bell:
            _render_notifications(_u)
        with c_user:
            _render_user_menu()
        with left:
            l_icon, l_text = st.columns([1, 4], gap="small")
            with l_icon:
                if org_bytes:
                    try:
                        st.image(org_bytes, width=45)
                    except Exception:
                        pass
            with l_text:
                # Org name on (up to) two tidy lines: the part before " — "
                # on line 1, the rest on line 2. Each is a display:block span
                # so "CHAI Cameroon" and "Business Development Team" never
                # share a line (and never wrap mid-line into a stray "Team"
                # that crosses the header border). Falls back to a single
                # line when the name has no " — " separator.
                _org_name_full = settings.get_org_name()
                if " — " in _org_name_full:
                    _org_primary, _org_secondary = _org_name_full.split(" — ", 1)
                else:
                    _org_primary, _org_secondary = _org_name_full, ""
                _name_html = (
                    f"<div style='padding-top:0.3rem; margin:0; "
                    f"line-height:1.18; color:{THEME_NAVY};'>"
                    f"<span style='display:block; font-weight:700; "
                    f"font-size:0.98rem;'>{_org_primary}</span>"
                )
                if _org_secondary:
                    _name_html += (
                        f"<span style='display:block; font-weight:500; "
                        f"font-size:0.8rem;'>{_org_secondary}</span>"
                    )
                _name_html += "</div>"
                st.markdown(_name_html, unsafe_allow_html=True)

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
