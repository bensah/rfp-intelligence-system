"""RFPIS — RFP Intelligence System · entry / router.

Builds the sidebar navigation with `st.navigation` (icons per page via
`st.Page`, including Home), runs the login gate + global header, then
dispatches to the selected page in `app_pages/`.

Why st.navigation instead of the file-based `pages/` directory:
  * Lets the entry page (Home) carry a nav icon — the auto pages/ nav
    can't icon the entry script.
  * Icons come from `icon=` params, so page filenames stay clean ASCII
    (no emoji-in-filename / variation-selector fragility).
  * The Admin page is gated by omitting it from the nav for non-admins,
    instead of hiding the link with CSS.

Why navigation is built BEFORE the login gate (refresh persistence):
  `st.navigation()` resolves the requested page from the browser URL and
  records it as the run's current page. A later `st.rerun()` carries that
  current-page hash. The login gate may `st.rerun()` while it waits for the
  auth cookie's JS round-trip to settle — and if that rerun happens *before*
  navigation has run, it carries an empty page hash, so the next run falls
  back to the default page (Home). That's exactly why a refresh on /donors
  used to bounce to Home. Running navigation first makes the deep link
  survive the cookie-settle rerun.
"""
from __future__ import annotations

import streamlit as st

# Must be the FIRST Streamlit call so a direct refresh lands in wide layout.
st.set_page_config(
    page_title="RFP Intelligence System - RFPIS",
    page_icon="🛈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

from auth.authenticator import ensure_logged_in  # noqa: E402
from core import permissions as _perms  # noqa: E402
from core.app_header import render_app_header  # noqa: E402

# Silence Streamlit's benign "Couldn't find fragment with id …" WARNING. It fires
# when an @st.dialog (a fragment) receives a rerun after a full app rerun (our
# `st.rerun()` inside dialogs) already tore it down — harmless log noise, no data
# lost. Targeted filter so every OTHER script-runner warning/error still shows.
import logging  # noqa: E402


class _DropFragmentNotFound(logging.Filter):
    def filter(self, record):
        return "Couldn't find fragment with id" not in record.getMessage()


logging.getLogger("streamlit.runtime.scriptrunner.script_runner").addFilter(
    _DropFragmentNotFound())


def _pages(include_admin: bool) -> list:
    """Explicit url_path on each page gives every page a stable URL slug, so a
    browser refresh on (say) /pipelines reloads that page. Home is the default
    page and lives at the app root.

    Registered pages fall into two groups:
      * RAIL pages — shown in the sidebar nav: the six everyday work surfaces
        (Home, Pipelines, Grants, Actions, Report, Donors) plus Settings
        (admins only) as a first-class rail item.
      * MENU-only pages (Profile, Help, Search) — registered with st.navigation
        for stable URLs / st.page_link, but their sidebar nav links are HIDDEN by
        CSS (core/app_header.py) so they live only in the top-right user menu.
      (Organization + Submit RFP are also registered for stable URLs and are
      reached via the user menu / in-app links.)
    """
    pages = [
        st.Page("app_pages/home.py", title="Home", icon="🏠", default=True),
        st.Page("app_pages/pipelines.py", title="Pipelines", icon="📚", url_path="pipelines"),
        st.Page("app_pages/grants.py", title="Grants", icon="💼", url_path="grants"),
        st.Page("app_pages/actions.py", title="Actions", icon="🗒️", url_path="actions"),
        st.Page("app_pages/report.py", title="Report", icon="📊", url_path="report"),
        st.Page("app_pages/donors.py", title="Donors", icon="🗺️", url_path="donors"),
        # ── Top-right user-menu pages (sidebar links hidden via CSS) ──
        st.Page("app_pages/organization.py", title="Tenant", icon="🏢",
                url_path="organization"),
        st.Page("app_pages/submit_rfp.py", title="Submit", icon="📝",
                url_path="submit-new-rfp"),
        # One opportunity on its own page (/opportunity?uid=…) — every title in the Live
        # Opportunity Feed links here. Menu-only, like the pages above: it is always
        # reached from a feed item, never browsed to empty-handed.
        st.Page("app_pages/opportunity.py", title="Opportunity", icon="🎯",
                url_path="opportunity"),
        st.Page("app_pages/profile.py", title="Profile", icon="👤", url_path="profile"),
        st.Page("app_pages/help.py", title="Help", icon="❓", url_path="help"),
        st.Page("app_pages/search.py", title="Search", icon="🔍", url_path="search"),
        # PUBLIC: reachable without signing in (see the gate below). A person holding an
        # invitation has no password yet, so the activation flow cannot live behind the
        # login gate.
        st.Page("app_pages/activate.py", title="Activate", icon="🔑",
                url_path="activate-account"),
    ]
    if include_admin:
        pages.append(st.Page("app_pages/admin.py", title="Settings", icon="⚙️", url_path="settings"))
    return pages


# Build navigation FIRST (see module docstring). Pre-auth (no cached user) we
# include every page so ANY deep link — including /admin — resolves and its
# intent survives the cookie-settle rerun. The nav is hidden on the login
# screen, and the post-login rerun below rebuilds it with the user's real role
# gating *before* any page renders, so a non-admin never executes /admin.
_session_user = st.session_state.get("app_user")
_include_admin = True if _session_user is None else _perms.is_admin(_session_user)
_nav = st.navigation(_pages(_include_admin))

# PUBLIC PAGES RUN BEFORE THE LOGIN GATE. Account activation is the one flow whose whole
# premise is that the visitor cannot sign in yet, so it cannot sit behind ensure_logged_in().
# Only the paths named here are exempt, and each renders its own screen and stops - nothing
# else on the app is reachable from them.
_PUBLIC_URL_PATHS = {"activate-account"}
if getattr(_nav, "url_path", None) in _PUBLIC_URL_PATHS:
    _nav.run()
    st.stop()

user = ensure_logged_in()
if not user:
    # Reset the sync flag while logged out so the next login re-gates the nav.
    st.session_state.pop("_post_login_nav_synced", None)
    st.stop()

# First authed run: the nav above was built from the (then-empty) session
# cache, so rerun once to rebuild it with the user's real role gating. The
# rerun carries the current page (navigation already ran), so the deep link is
# preserved and there's no flash of a wrong nav. Fires at most once per login.
if not st.session_state.get("_post_login_nav_synced"):
    st.session_state["_post_login_nav_synced"] = True
    st.rerun()

render_app_header()

# Global DB-connectivity boundary: a dropped Supabase connection (internet
# disruption / high traffic) otherwise bubbles out of any page's `.execute()` as a
# raw redacted httpx traceback and kills the whole app. Catch ONLY transient
# connectivity errors here and show a friendly Retry screen; re-raise everything
# else so real bugs stay visible. The client layer already retries connections and
# hot loaders degrade to empty — this is the last-resort net for a full outage.
try:
    _nav.run()
except Exception as _exc:  # noqa: BLE001 — re-raised below unless it's connectivity
    from db.supabase_client import get_client as _gc, is_connectivity_error
    if not is_connectivity_error(_exc):
        raise
    st.error("⚠️ Can't reach the database right now.")
    st.caption(
        "This is almost always a brief network hiccup connecting to the database. "
        "Wait a moment and retry — your data is safe.")
    if st.button("🔄 Retry", type="primary", key="db_conn_retry"):
        st.cache_data.clear()
        try:
            _gc.cache_clear()          # drop the cached (possibly half-open) client
        except Exception:
            pass
        st.rerun()
    st.stop()
