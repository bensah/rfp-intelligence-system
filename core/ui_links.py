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
"""
from __future__ import annotations

import html
from urllib.parse import quote

# Inherit Streamlit's own link colour so an internal link is not visibly a different KIND of
# thing from any other link on the page — the difference is where it goes, not how it looks.
_STYLE = "color:inherit;text-decoration:underline;text-underline-offset:2px"


def internal_href(path: str, **params: object) -> str:
    """``/opportunity?uid=AS-1`` — an in-app URL with its query string escaped."""
    p = "/" + str(path or "").strip().lstrip("/")
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
