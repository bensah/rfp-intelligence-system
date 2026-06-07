"""Playwright 'deep read' for non-API candidate pages.

Renders a candidate's detail page in headless Chromium, reads the fully
rendered text, and re-extracts the deadline / eligibility / description that a
plain ``requests`` fetch missed (JS-rendered donor portals, calendar widgets,
prose-only application windows). Used by the scan pipeline for candidates that
survive the cheap first-pass gate but still lack a deadline.

WHERE THIS RUNS
---------------
Chromium is NOT available on Streamlit Community Cloud, so manual scans there
degrade gracefully to the requests-based reader (everything below no-ops). The
GitHub Actions weekly scan (.github/workflows/scan.yml) installs Chromium, so
the deep read runs there. It also no-ops if RFPIS_DEEP_READ=0.

One headless browser is launched lazily and reused for the whole run; reads are
bounded by RFPIS_DEEP_READ_MAX and a per-page timeout.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_ENABLED = os.environ.get("RFPIS_DEEP_READ", "1") != "0"
_MAX = int(os.environ.get("RFPIS_DEEP_READ_MAX", "60"))
_TIMEOUT_MS = int(os.environ.get("RFPIS_DEEP_READ_TIMEOUT_MS", "20000"))

_state: dict[str, Any] = {
    "checked": False, "pw": None, "browser": None, "count": 0,
}


def _ensure_browser():
    """Lazily launch one headless Chromium for the whole run. Returns the
    browser, or None if Playwright/Chromium isn't available (or disabled)."""
    if _state["checked"]:
        return _state["browser"]
    _state["checked"] = True
    if not _ENABLED:
        log.info("deep_read: disabled via RFPIS_DEEP_READ=0")
        return None
    try:
        from playwright.sync_api import sync_playwright  # noqa: WPS433 (lazy)
    except ImportError:
        log.info("deep_read: playwright not installed — deep reads skipped")
        return None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
    except Exception as exc:
        # Chromium binary missing (e.g. Streamlit Cloud) or launch failure.
        log.info("deep_read: chromium unavailable (%s) — deep reads skipped", exc)
        return None
    _state["pw"] = pw
    _state["browser"] = browser
    import atexit
    atexit.register(close)
    return browser


def available() -> bool:
    """True only where headless Chromium can actually launch."""
    return _ensure_browser() is not None


def close() -> None:
    try:
        if _state.get("browser"):
            _state["browser"].close()
    except Exception:
        pass
    try:
        if _state.get("pw"):
            _state["pw"].stop()
    except Exception:
        pass
    _state["browser"] = None
    _state["pw"] = None


def _is_api_source(candidate: dict) -> bool:
    """Structured API sources (grants.gov) already have clean fields — skip."""
    o = (candidate.get("_source_origin") or "").lower()
    return "grants.gov" in o or "(kw=" in o


def render_text(url: str):
    """Render ``url`` in Chromium. Returns (visible_text, BeautifulSoup) or
    None. Bounded by RFPIS_DEEP_READ_MAX."""
    browser = _ensure_browser()
    if not browser or _state["count"] >= _MAX:
        return None
    from bs4 import BeautifulSoup
    from core.scraper import USER_AGENT
    ctx = None
    try:
        ctx = browser.new_context(user_agent=USER_AGENT)
        page = ctx.new_page()
        page.set_default_timeout(_TIMEOUT_MS)
        page.goto(url, wait_until="networkidle", timeout=_TIMEOUT_MS)
        page.wait_for_timeout(800)
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
        except Exception:
            pass
        html = page.content()
    except Exception as exc:
        log.debug("deep_read: render failed for %s: %s", url, exc)
        return None
    finally:
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass
    _state["count"] += 1
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True), soup


def enrich(candidate: dict) -> bool:
    """Deep-read a candidate's detail page and fill in deadline / eligibility /
    description that the requests pass missed. Returns True if a render ran.
    Skips API sources, PDFs, and non-http links."""
    if _is_api_source(candidate):
        return False
    link = candidate.get("opportunity_link") or ""
    if not link.startswith("http") or link.lower().split("?", 1)[0].endswith(".pdf"):
        return False
    rendered = render_text(link)
    if not rendered:
        return False
    text, soup = rendered
    from core.scraper import (  # noqa: WPS433 (lazy — avoids import cycle)
        _extract_deadline_from_text, _extract_description_from_soup,
        _extract_eligibility_from_text, _find_application_pdf,
        _try_pdf_guide_deadline,
    )
    # Deadline — text, then a linked application-guide PDF.
    if not candidate.get("submission_deadline"):
        d = _extract_deadline_from_text(text)
        if not d:
            try:
                pdf_url = _find_application_pdf(soup, link)
                if pdf_url:
                    d, _brief = _try_pdf_guide_deadline(pdf_url)
            except Exception:
                d = None
        if d:
            candidate["submission_deadline"] = d
    # Eligibility / geography prose -> fold into the description so the country
    # gate can read it (mirrors the requests-path behaviour).
    try:
        elig = _extract_eligibility_from_text(text)
    except Exception:
        elig = ""
    if elig:
        base = candidate.get("brief_description") or ""
        if elig.lower() not in base.lower():
            candidate["brief_description"] = (base + " " + elig).strip()
    # Description fallback when we still have nothing.
    if not (candidate.get("brief_description") or "").strip():
        try:
            desc = _extract_description_from_soup(soup)
        except Exception:
            desc = ""
        if desc:
            candidate["brief_description"] = desc
    candidate["_deep_read"] = True
    return True
