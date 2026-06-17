"""Playwright 'deep read' for non-API candidate pages — the robust-crawl stage.

Renders a candidate's detail page in headless Chromium, reads the fully
rendered text, and re-extracts the deadline / eligibility / description that a
plain ``requests`` fetch missed (JS-rendered donor portals, calendar widgets,
prose-only application windows). Used by the scan pipeline for candidates that
survive the cheap first-pass gate but still lack a deadline / are thin.

ROBUST CRAWL (ML pipeline Phase 2)
----------------------------------
``enrich`` is a small crawl, not a single fetch:
  1. render the page in Chromium and capture the HTTP status;
  2. ERROR-CHECK — a dead status (404/410) or a soft-404 / error template body
     (incl. JS-rendered "page not found" SPAs that a requests fetch can't see)
     flags ``_dead_page`` so the pipeline's RE-GATE drops it;
  3. harvest deadline / eligibility / description from the rendered page;
  4. when a date is still missing, follow the call's PDF guide or a companion
     calendar page for it;
  5. when the page is still THIN, follow the best valid parent/child detail link
     (one hop, error-checked) and harvest deadline / eligibility / description
     from THAT page.
The pipeline then re-runs ``is_eligible`` on the enriched candidate and only
scores MUST/PREFER for survivors.

WHERE THIS RUNS
---------------
Chromium is NOT available on Streamlit Community Cloud, so manual scans there
degrade gracefully to the requests-based reader + core.live_check (everything
below no-ops). The GitHub Actions weekly scan (.github/workflows/scan.yml)
installs Chromium, so the deep read runs there. It also no-ops if
RFPIS_DEEP_READ=0.

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

# Status codes that mean the resource is gone → reject. 403 / 5xx are NOT here:
# a live page can sit behind a bot wall or blip transiently, so we only reject
# on those when the rendered BODY is itself an error template.
_DEAD_STATUSES = {404, 410}

# Below this many chars of description we treat the page as "thin" and try to
# follow a child/companion detail link for the real content.
_THIN_DESC = 120

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
    """Render ``url`` in Chromium. Returns (visible_text, BeautifulSoup, status)
    or None. ``status`` is the navigation HTTP status (int) or None. Bounded by
    RFPIS_DEEP_READ_MAX."""
    browser = _ensure_browser()
    if not browser or _state["count"] >= _MAX:
        return None
    from bs4 import BeautifulSoup
    from core.scraper import USER_AGENT
    ctx = None
    status = None
    try:
        ctx = browser.new_context(user_agent=USER_AGENT)
        page = ctx.new_page()
        page.set_default_timeout(_TIMEOUT_MS)
        resp = page.goto(url, wait_until="networkidle", timeout=_TIMEOUT_MS)
        status = resp.status if resp is not None else None
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
    return soup.get_text(" ", strip=True), soup, status


def _soup_title(soup) -> str:
    try:
        return (soup.title.string if soup is not None and soup.title else "") or ""
    except Exception:
        return ""


def _dead_reason(status, title: str, text: str) -> str | None:
    """Return a reason string when a rendered page is dead / an error template,
    else None. Reuses the scan gate's error-page regex so the requests path
    (core.live_check) and this Chromium path stay in lock-step — and catches
    JS-rendered soft-404s (200 status, 'page not found' body) a plain fetch
    can't see."""
    if status in _DEAD_STATUSES:
        return f"dead link (HTTP {status})"
    try:
        from core.auto_scorer import _ERROR_PAGE_RE
    except Exception:
        return None
    blob = f"{title or ''} {(text or '')[:1500]}"
    if _ERROR_PAGE_RE.search(blob):
        return "dead link (soft-404 / error page body)"
    return None


def _harvest(candidate: dict, text: str, soup) -> None:
    """Pull deadline / eligibility / description out of one rendered page into
    the candidate (only filling what's still missing). No link-following — the
    caller decides when to follow."""
    from core.scraper import (  # noqa: WPS433 (lazy — avoids import cycle)
        _extract_deadline_from_text, _extract_description_from_soup,
        _extract_eligibility_from_text,
    )
    if not candidate.get("submission_deadline"):
        d = _extract_deadline_from_text(text)
        if d:
            candidate["submission_deadline"] = d
    # Eligibility / geography prose → fold into the description so the country
    # gate (which reads brief_description) can judge scope without us guessing a
    # synonym-expanded geographic_scope list.
    try:
        elig = _extract_eligibility_from_text(text)
    except Exception:
        elig = ""
    if elig:
        base = candidate.get("brief_description") or ""
        if elig.lower() not in base.lower():
            candidate["brief_description"] = (base + " " + elig).strip()
    if not (candidate.get("brief_description") or "").strip():
        try:
            desc = _extract_description_from_soup(soup)
        except Exception:
            desc = ""
        if desc:
            candidate["brief_description"] = desc


def _thin(candidate: dict) -> bool:
    return (not candidate.get("submission_deadline")
            or len(candidate.get("brief_description") or "") < _THIN_DESC)


def _follow_for_deadline(candidate: dict, soup, base_url: str) -> None:
    """Recover a still-missing deadline from the call's PDF guide, then a
    companion calendar page (Fondation Pierre Fabre → odess.io)."""
    from core.scraper import (
        _find_application_pdf, _try_pdf_guide_deadline,
        _follow_companion_for_deadline,
    )
    try:
        pdf_url = _find_application_pdf(soup, base_url)
        if pdf_url:
            d, _brief = _try_pdf_guide_deadline(pdf_url)
            if d:
                candidate["submission_deadline"] = d
    except Exception:
        pass
    if not candidate.get("submission_deadline"):
        try:
            d = _follow_companion_for_deadline(soup, base_url)
            if d:
                candidate["submission_deadline"] = d
        except Exception:
            pass


def _follow_child_and_harvest(candidate: dict, soup, base_url: str) -> None:
    """When a landing page is a stub, follow the best valid child/companion
    detail link (one hop, error-checked) and harvest deadline / eligibility /
    description from it. Bounded: at most the top 2 ranked links, each subject
    to the global render cap."""
    try:
        from core.scraper import _find_companion_call_links
        links = _find_companion_call_links(soup, base_url)
    except Exception:
        links = []
    for child in links[:2]:
        if child.lower().split("?", 1)[0].endswith(".pdf"):
            continue                          # PDFs handled by the deadline path
        rendered = render_text(child)
        if not rendered:
            continue
        ctext, csoup, cstatus = rendered
        if _dead_reason(cstatus, _soup_title(csoup), ctext):
            continue                          # don't adopt an error child page
        _harvest(candidate, ctext, csoup)
        candidate["_followed_child"] = child
        if not _thin(candidate):
            break


def enrich(candidate: dict) -> bool:
    """Robust-crawl a candidate's detail page: error-check, harvest, follow a
    PDF / companion / child link for what's missing. Returns True if a render
    ran. Skips API sources, PDFs, and non-http links. Mutates the candidate;
    the pipeline RE-GATEs afterward."""
    if _is_api_source(candidate):
        return False
    link = candidate.get("opportunity_link") or ""
    if not link.startswith("http") or link.lower().split("?", 1)[0].endswith(".pdf"):
        return False
    rendered = render_text(link)
    if not rendered:
        return False
    text, soup, status = rendered
    candidate["_deep_read"] = True

    # 1) Error-check the rendered page — a dead/soft-404/error template is not a
    #    real call. Flag it so the pipeline's re-gate drops it.
    dead = _dead_reason(status, _soup_title(soup), text)
    if dead:
        candidate["_dead_page"] = True
        candidate["_dead_reason"] = dead
        return True

    # 2) Harvest the rendered page.
    _harvest(candidate, text, soup)

    # 3) Deadline still missing → PDF guide, then companion calendar page.
    if not candidate.get("submission_deadline"):
        _follow_for_deadline(candidate, soup, link)

    # 4) Still thin → follow the best valid child/companion detail link.
    if _thin(candidate):
        _follow_child_and_harvest(candidate, soup, link)

    return True
