"""Resolve aggregator hits to the donor's own source page.

Aggregators like DevelopmentAid profit off RFP listings and limit crawling, so
their detail pages give us a title but no readable deadline / eligibility. This
module takes such a hit, runs a real Google search (Serper — already wired in
core.web_search, so NO new dependency and no IP-blocking like DIY scraping
libs), picks the donor's OWN canonical page (skipping the aggregator, social and
login/paywall links), then fetches THAT page so the normal scan gate sees the
real deadline / scope.

Example: "Call for Proposals: Daylight Research Grant Program" →
veluxstiftung.ch/funding-areas/daylight-research/ (or the EPFL memento detail
page) — whichever ranks first as a genuine, unrestricted detail page.

Bounded by design: only relevant aggregator hits are resolved (one Serper call
each), and everything is best-effort — it never raises into the scan.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Hosts whose detail pages should be resolved to the original source.
_AGGREGATOR_RE = re.compile(
    r"(developmentaid\.org|fundsforngos\.org|grantforward|grantstation|"
    r"opengrants\.io|instrumentl\.com|grants\.gov/search)", re.I)

# Never accept these as the "source": the aggregator itself, social shares,
# login/paywall walls, job boards, and generic search/listing endpoints.
_EXCLUDE_RE = re.compile(
    r"(developmentaid\.org|linkedin\.com|facebook\.com|twitter\.com|//x\.com|"
    r"instagram\.com|youtube\.com|reddit\.com|/login|/sign-?in|/subscribe|"
    r"indeed\.com|glassdoor|/search\?|/jobs?\b|fundsforngos\.org)", re.I)

_RFP_HINT_RE = re.compile(
    r"(call for proposal|request for (proposal|application)|funding opportunit|"
    r"grant|apply|deadline|expression of interest|fellowship|award)", re.I)

_STOP = {"the", "for", "and", "a", "an", "of", "to", "in", "on", "call",
         "proposals", "proposal", "rfp", "rfa", "cfa", "cfas", "request",
         "applications", "application", "grants", "grant", "program",
         "programme", "funding", "opportunity", "opportunities", "2024",
         "2025", "2026", "2027"}


def available() -> bool:
    """True only when a Serper key is configured (real Google)."""
    try:
        from core.web_search import _secret
        return bool(_secret("SERPER_API_KEY"))
    except Exception:
        return False


def is_aggregator(url: str | None) -> bool:
    return bool(url) and bool(_AGGREGATOR_RE.search(url))


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
            if w not in _STOP}


def _domain(url: str) -> str:
    return urlsplit(url or "").netloc.lower().lstrip("www.")


def _score(result: dict, title_tokens: set[str], funder_tokens: set[str]) -> float:
    """Rank a Google result as a candidate source page (higher = better)."""
    url = result.get("url") or ""
    rtoks = _tokens(f"{result.get('title','')} {result.get('content','')}")
    dom = _domain(url)
    score = 0.0
    # Title overlap — the core signal that this result is the same call.
    overlap = len(title_tokens & rtoks)
    score += 2.0 * overlap
    # Funder-domain match (e.g. funder 'Velux Stiftung' → veluxstiftung.ch).
    if funder_tokens and any(t in dom for t in funder_tokens if len(t) >= 4):
        score += 5.0
    # Looks like an actual call page.
    if _RFP_HINT_RE.search(f"{result.get('title','')} {result.get('content','')}"):
        score += 2.0
    # Prefer a donor/institutional domain over random news.
    if dom.endswith((".org", ".edu", ".gov", ".int", ".ac.uk")) or "fond" in dom \
            or "stiftung" in dom or "foundation" in dom:
        score += 1.5
    return score


def resolve(title: str, funder: str | None = None, *, num: int = 10) -> str | None:
    """Best canonical source URL for an aggregator-listed call, or None.

    Searches Google for the exact title (+ funder when it's a real org, NOT the
    aggregator's own name), ranks the organic results, and returns the highest
    non-excluded one that clearly matches the call."""
    title = (title or "").strip()
    if not title or not available():
        return None
    try:
        from core.web_search import _secret, _serper_items
        key = _secret("SERPER_API_KEY")
        # A real funder name sharpens the search; the aggregator's own label
        # ("DevelopmentAid Grants Aggregator") is noise, so drop it.
        f = (funder or "").strip()
        if f and "aggregator" not in f.lower() and "developmentaid" not in f.lower():
            query = f'"{title}" {f}'
            funder_tokens = _tokens(f)
        else:
            query = f'"{title}"'
            funder_tokens = set()
        items = _serper_items(query, key, num) or []
        title_tokens = _tokens(title)
        ranked = []
        for it in items:
            url = it.get("url") or ""
            if not url or _EXCLUDE_RE.search(url):
                continue
            ranked.append((_score(it, title_tokens, funder_tokens), url))
        if not ranked:
            return None
        ranked.sort(key=lambda x: x[0], reverse=True)
        best_score, best_url = ranked[0]
        # Require a minimum match so we don't swap in an unrelated page.
        if best_score < 4.0:
            return None
        return best_url
    except Exception as exc:
        log.debug("source_resolver.resolve failed: %s", exc)
        return None


def resolve_and_enrich(cand: dict) -> bool:
    """Resolve an aggregator candidate to its source URL, then fetch that page
    and fill deadline / description / eligibility so the gate can judge the REAL
    call. Mutates `cand` and returns True if a source was resolved + fetched."""
    src = resolve(cand.get("opportunity_title"), cand.get("funding_agency"))
    if not src:
        return False
    try:
        import requests
        from bs4 import BeautifulSoup
        from core.scraper import (
            USER_AGENT, HTTP_TIMEOUT, _extract_deadline_from_text,
            _extract_description_from_soup, _extract_eligibility_from_text,
            _extract_page_date,
        )
        r = requests.get(src, headers={"User-Agent": USER_AGENT},
                         timeout=HTTP_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
    except Exception as exc:
        log.debug("source_resolver fetch failed for %s: %s", src, exc)
        return False
    cand["_aggregator_link"] = cand.get("opportunity_link")
    cand["opportunity_link"] = src
    cand["_resolved_from_aggregator"] = True
    dl = _extract_deadline_from_text(text)
    if dl:
        cand["submission_deadline"] = dl
    pd = _extract_page_date(soup)
    if pd and not cand.get("date_posted"):
        cand["date_posted"] = pd
    desc = _extract_description_from_soup(soup) or (text[:600] if text else "")
    elig = _extract_eligibility_from_text(text)
    if elig:
        desc = (desc + ("\n\n" if desc else "") + "Eligibility: " + elig)
    if desc:
        cand["brief_description"] = desc[:1800]
    log.info("resolved aggregator → source: %s", src)
    return True
