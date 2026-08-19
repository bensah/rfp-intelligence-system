"""Links that stay inside the app, and links that leave it.

Streamlit renders every markdown link with ``target="_blank"``. So
``st.markdown("[Open in Review](/pipelines?uid=…)")`` — a move from one page of this app to
another — opened a NEW BROWSER TAB, and a reviewer walking a pipeline collected a tab per
click. Nothing in the code said "open outward"; it was the default for the markup being used.

The distinction is not cosmetic, it is about what the link means:

  INTERNAL  another page of this app. Same tab, always: the reader is continuing a task, and
            their back button is part of how they navigate. `internal_link`.
  EXTERNAL  the funder's own site, a source PDF, a portal. New tab is right — losing the app
            mid-review to read a call would be worse. Plain markdown already does this, so
            external links need nothing from here.

Anchors rather than `st.page_link` because these carry a uid in the query string, which
`st.page_link` has no way to pass.

AND ONE MORE DISTINCTION, learned the hard way. An anchor — even `target="_self"` — is a
FULL BROWSER NAVIGATION. Measured in a Streamlit 1.61 harness: the document is torn down
and the app comes back in a BRAND-NEW SESSION with `st.session_state` empty. The query
string survives; nothing else does. For this app that meant:

  * a super_user's view-as (`su_view_tenant`, session-state only) was dropped by the first
    feed click, silently returning them to their own tenant;
  * every click paid a cold start — the login gate re-ran from the cookie, and where that
    read is unreliable behind the host's proxy (see auth.authenticator, top of file) the
    click landed on /login instead of the page asked for. Which is what "the link looks
    like it will open and then doesn't" was.

So an in-app destination is now navigated with `internal_nav` — a link-STYLED button that
calls `st.switch_page` inside the SAME session. `internal_link` / `internal_button` remain
for the places that genuinely want a URL (an address to copy, a bookmark, a new tab), and
they now carry `?tenant=` so a cold load restores the tenant the link was made in.
"""
from __future__ import annotations

import html
from urllib.parse import quote

# Inherit Streamlit's own link colour so an internal link is not visibly a different KIND of
# thing from any other link on the page — the difference is where it goes, not how it looks.
_STYLE = "color:inherit;text-decoration:underline;text-underline-offset:2px"


# url_path (what the address bar shows) -> page script (what st.switch_page takes). Two
# names for one page, and only Streamlit knows the mapping, so it is written down once here
# rather than at every call site.
PAGE_SCRIPTS: dict[str, str] = {
    "": "app_pages/home.py",
    "home": "app_pages/home.py",
    "pipelines": "app_pages/pipelines.py",
    "grants": "app_pages/grants.py",
    "actions": "app_pages/actions.py",
    "report": "app_pages/report.py",
    "donors": "app_pages/donors.py",
    "organization": "app_pages/organization.py",
    "submit-new-rfp": "app_pages/submit_rfp.py",
    "opportunity": "app_pages/opportunity.py",
    "profile": "app_pages/profile.py",
    "help": "app_pages/help.py",
    "search": "app_pages/search.py",
    "settings": "app_pages/admin.py",
}

# Where a hand-off from internal_nav parks the query values that st.switch_page cannot
# carry. Read (and cleared) by the destination page, which then restates them in the URL so
# the address stays truthful and the page is still bookmarkable.
NAV_HANDOFF_KEY = "_nav_handoff"


def _session_tenant() -> str | None:
    """The tenant slug this session is actually showing — the view-as target for a
    super_user, else their own. Only used to keep a copied URL truthful; the incoming value
    is still never trusted to CHOOSE a tenant (auth.tenant_context owns that)."""
    try:
        import streamlit as st
        ss = st.session_state
        return (ss.get("su_view_slug") or ss.get("tenant_slug")) or None
    except Exception:
        return None


def internal_href(path: str, **params: object) -> str:
    """``/opportunity?uid=AS-1&tenant=client`` — an in-app URL with its query string escaped.

    `tenant` rides along automatically (unless the caller names one) because a URL is the
    one route into the app that survives a cold load, and a cold load has no session to
    remember which tenant the link was made in. Pass ``tenant=None`` to suppress it.
    """
    p = "/" + str(path or "").strip().lstrip("/")
    if "tenant" not in params:
        params = {**params, "tenant": _session_tenant()}
    pairs = [(k, v) for k, v in params.items() if v not in (None, "")]
    if not pairs:
        return p
    return p + "?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in pairs)


def internal_link(label: str, path: str, *, bold: bool = False,
                  style: str = "", **params: object) -> str:
    """HTML for a link to another page of THIS app — same tab.

    Render with ``st.markdown(..., unsafe_allow_html=True)``. The label is escaped; the caller
    supplies only the path and query values.
    """
    text = html.escape(str(label or ""))
    if bold:
        text = f"<strong>{text}</strong>"
    css = _STYLE + (";" + style if style else "")
    return (f"<a href='{internal_href(path, **params)}' target='_self' "
            f"style='{css}'>{text}</a>")


def internal_button(label: str, path: str, **params: object) -> str:
    """A link styled as a button, for sitting in a row of real buttons. Same tab."""
    return (f"<a href='{internal_href(path, **params)}' target='_self' "
            "style='display:block;text-align:center;padding:8px 12px;border-radius:8px;"
            "border:1px solid #16734a;color:#16734a;font-weight:600;"
            f"text-decoration:none'>{html.escape(str(label or ''))}</a>")


def internal_nav(label: str, path: str, *, key: str, icon: str | None = None,
                 button: bool = False, help: str | None = None,
                 width: str = "content", **params: object) -> bool:
    """Go to another page of THIS app, in the SAME session. Returns True on the run the
    click happened (it switches pages immediately, so the return value is mostly a
    formality).

    Renders a Streamlit button rather than an anchor, because an anchor is a full browser
    navigation and throws the session away (see the module docstring). `button=False` — the
    default — uses the tertiary style, which Streamlit draws as a link, so the reader sees
    what they saw before.

    Query values that `st.switch_page` cannot carry (uid, q, …) are parked in session state
    and picked up by the destination via `take_handoff`.
    """
    import streamlit as st

    slug = str(path or "").strip().lstrip("/")
    script = PAGE_SCRIPTS.get(slug)
    if script is None:
        # An unmapped destination must not silently render a dead control: fall back to the
        # URL form, which at least goes somewhere, and is visibly a link.
        st.markdown(internal_link(label, path, **params), unsafe_allow_html=True)
        return False

    clicked = st.button(label, key=key, type=("secondary" if button else "tertiary"),
                        icon=icon, help=help, width=width)
    if clicked:
        payload = {k: v for k, v in params.items() if v not in (None, "")}
        st.session_state[NAV_HANDOFF_KEY] = {"page": slug, "params": payload}
        st.switch_page(script)
    return clicked


def take_handoff(slug: str) -> dict:
    """Claim the query values `internal_nav` parked for THIS page, and clear them.

    Returns {} when the page was reached any other way (a typed URL, a bookmark, a
    refresh), so a caller reads the query string first and falls back to this.
    """
    import streamlit as st

    data = st.session_state.get(NAV_HANDOFF_KEY)
    if not isinstance(data, dict) or data.get("page") != slug:
        return {}
    st.session_state.pop(NAV_HANDOFF_KEY, None)
    out = data.get("params")
    return out if isinstance(out, dict) else {}


# ── in-app history ──────────────────────────────────────────────────────────────────────
# Streamlit has no back button of its own, and the browser's is a poor substitute here: a
# page reached by st.switch_page shares a history entry with the one it replaced, so
# browser-back can jump two moves at once or leave the app entirely. So the app keeps its
# own trail of where the reader has been, in the session, and offers one step back.
NAV_HISTORY_KEY = "_nav_history"
_NAV_BACK_FLAG = "_nav_going_back"
_HISTORY_CAP = 25

# Human names for the trail, so "Back" can say what it goes back TO.
PAGE_TITLES: dict[str, str] = {
    "": "Home", "home": "Home", "pipelines": "Pipelines", "grants": "Grants",
    "actions": "Actions", "report": "Report", "donors": "Donors",
    "organization": "Tenant", "submit-new-rfp": "Submit", "opportunity": "Opportunity",
    "profile": "Profile", "help": "Help", "search": "Search", "settings": "Settings",
}


def record_visit(slug: str | None, params: dict | None = None) -> None:
    """Note the page being rendered. Called once per run, from the router.

    A RERUN is not a move: the same page with the same query is folded into the entry
    already on top, or every widget interaction would bury the reader's real previous page
    under a stack of duplicates. Arriving via Back is not a move either — the stack was
    rewritten before the switch.
    """
    import streamlit as st

    ss = st.session_state
    if ss.pop(_NAV_BACK_FLAG, False):
        return
    page = str(slug or "home")
    entry = {"page": page, "params": {k: v for k, v in (params or {}).items()
                                      if k != "tenant"}}
    hist = ss.setdefault(NAV_HISTORY_KEY, [])
    if hist and hist[-1] == entry:
        return
    hist.append(entry)
    if len(hist) > _HISTORY_CAP:
        del hist[:-_HISTORY_CAP]


def render_back_button(*, key: str = "nav_back") -> bool:
    """One step back, if there is one. Renders nothing on the first page of a session.

    Uses the same hand-off as `internal_nav`, so returning to an opportunity returns to
    THAT opportunity rather than the empty page.
    """
    import streamlit as st

    hist = st.session_state.get(NAV_HISTORY_KEY) or []
    if len(hist) < 2:
        return False
    prev = hist[-2]
    script = PAGE_SCRIPTS.get(str(prev.get("page") or ""))
    if not script:
        return False
    name = PAGE_TITLES.get(str(prev.get("page") or ""), "the previous page")
    if st.button("← Back", key=key, type="tertiary", help=f"Back to {name}"):
        hist.pop()                                   # leave the page we are on
        st.session_state[_NAV_BACK_FLAG] = True      # the arrival is not a new move
        st.session_state[NAV_HANDOFF_KEY] = {"page": prev.get("page"),
                                             "params": prev.get("params") or {}}
        st.switch_page(script)
    return True
