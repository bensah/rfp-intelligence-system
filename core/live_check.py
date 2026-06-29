"""Cheap HTTP liveness + re-enrich for thin scan candidates.

Plain `requests` (no Chromium) so it runs EVERYWHERE — Streamlit Cloud included
— complementing core.deep_read (Playwright, GitHub-Actions-only). For a
candidate that is thin (no deadline / no description), fetch the page ONCE and
either:

  * flag it dead (`_dead_page`) when the URL is gone (HTTP 404/410) or returns a
    soft-404 / error template (200 status but a "page not found" body — e.g. the
    WordPress "No Results Found · The page you requested could not be found"
    page on healthresearch.org); or
  * recover the deadline / description / posted-date from the FULL body so the
    gate can judge the real call (e.g. an expired "Submission deadline: October
    10, 2019" buried in GAC prose that the search/listing snippet didn't carry).

Best-effort: never raises into the scan. Bounded per-run by RFPIS_LIVE_CHECK_MAX
so a manual Cloud scan can't spend unbounded time fetching.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Definitively-gone statuses → reject. 403 (bot wall) / 5xx (transient) are NOT
# treated as dead: the page may be perfectly live behind a wall, so we never
# reject on them — we just can't enrich.
_DEAD_STATUSES = {404, 410}


def max_checks() -> int:
    """Per-run cap on liveness fetches (env-tunable)."""
    try:
        return int(os.environ.get("RFPIS_LIVE_CHECK_MAX", "80"))
    except (TypeError, ValueError):
        return 80


def recheck_and_enrich(cand: dict) -> bool:
    """Fetch the candidate's link once. Set `_dead_page`/`_dead_reason` if the
    page is gone / an error template; otherwise fill any missing
    deadline / description / date_posted from the full body.

    Returns True if the page was fetched (dead OR enriched), False if it
    couldn't even be attempted (no usable URL / deps missing)."""
    url = (cand.get("opportunity_link") or "").strip()
    if not url.lower().startswith("http"):
        return False
    try:
        import requests
        from bs4 import BeautifulSoup
        from core.scraper import (
            USER_AGENT, HTTP_TIMEOUT, _extract_deadline_from_text,
            _extract_description_from_soup, _extract_eligibility_from_text,
            _extract_page_date,
        )
        from core.auto_scorer import _ERROR_PAGE_RE
    except Exception as exc:                       # pragma: no cover
        log.debug("live_check unavailable: %s", exc)
        return False

    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT},
                         timeout=HTTP_TIMEOUT, allow_redirects=True)
    except Exception as exc:
        log.debug("live_check fetch failed for %s: %s", url, exc)
        return False

    # Hard dead — the resource is gone.
    if r.status_code in _DEAD_STATUSES:
        cand["_dead_page"] = True
        cand["_dead_reason"] = f"dead link (HTTP {r.status_code})"
        return True
    # Other non-2xx — don't judge (bot wall / transient). Can't enrich.
    if r.status_code >= 400:
        return True

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        title = (soup.title.string if soup.title else "") or ""
    except Exception:
        return True

    # Soft-404 / error template served with a 200 status.
    if _ERROR_PAGE_RE.search(f"{title} {text[:1500]}"):
        cand["_dead_page"] = True
        cand["_dead_reason"] = "dead link (soft-404 / error page body)"
        return True

    # Live page — recover the signals the gate needs to judge the real call.
    if not cand.get("call_submission_deadline"):
        dl = _extract_deadline_from_text(text)
        if dl:
            cand["call_submission_deadline"] = dl
    if not (cand.get("brief_description") or "").strip():
        desc = _extract_description_from_soup(soup) or (text[:600] if text else "")
        elig = _extract_eligibility_from_text(text)
        if elig:
            desc = desc + ("\n\n" if desc else "") + "Eligibility: " + elig
        if desc:
            cand["brief_description"] = desc[:1800]
    if not cand.get("date_posted"):
        try:
            pd = _extract_page_date(soup)
            if pd:
                cand["date_posted"] = pd
        except Exception:
            pass
    return True
