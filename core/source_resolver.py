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
# NB: grants.gov/search… is a LISTING/search page (an aggregator-style index),
# but grants.gov/search-results-DETAIL/<id> is the canonical opportunity page —
# the source itself — so the negative lookahead keeps detail pages out.
_AGGREGATOR_RE = re.compile(
    r"(developmentaid\.org|fundsforngos\.org|grantforward|grantstation|"
    r"opengrants\.io|instrumentl\.com|grants\.gov/search(?!-results-detail))", re.I)

# Never accept these as the "source": ANY aggregator (we must resolve to the
# donor's OWN page, never swap one aggregator for another — copyright + SEO),
# social shares, login/paywall walls, job boards, generic search/listing pages.
_EXCLUDE_RE = re.compile(
    r"(developmentaid\.org|fundsforngos?\.org|fundsforngo\.com|grantbite\.com|"
    r"instrumentl\.com|grantstation\.com|grantforward\.com|opengrants\.io|"
    r"grantwatch\.com|devex\.com|candid\.org|grantgopher\.com|"
    r"linkedin\.com|facebook\.com|twitter\.com|//x\.com|"
    r"instagram\.com|youtube\.com|reddit\.com|/login|/sign-?in|/subscribe|"
    r"indeed\.com|glassdoor|/search\?|/jobs?\b)", re.I)

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


# News / press-release / generic-listing hosts — never the primary source.
_NEWS_PR_RE = re.compile(
    r"(businesswire\.com|prnewswire\.com|globenewswire\.com|einnews\.com|"
    r"openpr\.com|prweb\.com|prleap\.com|finance\.yahoo|news\.yahoo|yahoo\.com|"
    r"reuters\.com|bloomberg\.com|apnews\.com|medium\.com|substack\.com|"
    r"wamda\.com|for9a\.com|naaee\.org|issuu\.com|scribd\.com|slideshare\.net|"
    r"wikipedia\.org|crunchbase\.com)", re.I)

# Generic words to DROP when matching a name against a domain — they appear in
# many domains and don't identify the funder. What's left are the distinctive
# tokens (the funder's name, a programme's coined word) we match on.
_NAME_STOP = _STOP | {
    "global", "international", "national", "regional", "world", "africa",
    "african", "asia", "asian", "europe", "european", "sustainability",
    "sustainable", "humanitarian", "impact", "development", "developing",
    "health", "research", "innovation", "innovative", "foundation", "fund",
    "funds", "initiative", "challenge", "challenges", "prize", "prizes",
    "award", "awards", "open", "opens", "opening", "applications", "submission",
    "submissions", "now", "cycle", "annual", "edition", "south", "north",
    "project", "projects", "schools", "organizations", "organisations",
}
_MIN_DOMAIN_SCORE = 4.0   # ≥ one distinctive token (len ≥ 4) matched in-domain


def _domain_core(url: str) -> tuple[str, str]:
    """(host, alnum-core) — e.g. zayedsustainabilityprize.com →
    ('zayedsustainabilityprize.com', 'zayedsustainabilityprizecom')."""
    host = _domain(url)
    return host, re.sub(r"[^a-z0-9]", "", host)


def _name_tokens(title: str | None, funder: str | None) -> set[str]:
    """Distinctive tokens (len ≥ 4, non-generic) from funder + title — the ones
    a funder's OWN domain tends to echo."""
    return {t for t in re.findall(r"[a-z0-9]{4,}",
                                  f"{funder or ''} {title or ''}".lower())
            if t not in _NAME_STOP}


def _score_domain(url: str, name_tokens: set[str]) -> float:
    """How strongly a domain looks like the funder's OWN site. Dominant signal:
    name-token overlap weighted by token LENGTH (rare/long tokens like
    'sustainability' outweigh generic ones)."""
    host, core = _domain_core(url)
    s = sum(len(t) for t in name_tokens if t in core)
    if host.endswith((".org", ".edu", ".gov", ".int", ".eu", ".ac.uk")):
        s += 2.0
    if any(w in core for w in ("foundation", "fund", "stiftung", "fondation",
                               "trust", "philanthrop", "institute", "prize")):
        s += 1.5
    return s


def _ok_primary(url: str | None) -> bool:
    """A URL is an acceptable PRIMARY source iff it isn't an aggregator, blog,
    news/PR, social or login/listing page."""
    if not url or _EXCLUDE_RE.search(url) or _NEWS_PR_RE.search(url):
        return False
    try:
        from core import aggregators
        return not aggregators.is_non_primary(url)[0]
    except Exception:
        return True


def _resolve_via_backlink(items: list[dict], name_tokens: set[str]) -> str | None:
    """Hard case — the primary's domain doesn't echo the name. Fetch the top
    results (even aggregators) and pull their best OUTBOUND link to the funder's
    own site (aggregators reliably cite the source / 'apply'/'official website').
    Returns the best-scoring external primary link, or None."""
    try:
        import requests
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        from core.scraper import USER_AGENT, HTTP_TIMEOUT
    except Exception:
        return None
    for it in items[:3]:
        url = it.get("link") or it.get("url") or ""
        if not url:
            continue
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT},
                             timeout=HTTP_TIMEOUT, allow_redirects=True)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            continue
        src_host = _domain(url)
        best, best_s = None, 0.0
        for a in soup.find_all("a", href=True):
            href = urljoin(url, (a.get("href") or "").strip())
            if not href.startswith("http") or _domain(href) == src_host:
                continue
            if not _ok_primary(href):
                continue
            s = _score_domain(href, name_tokens)
            atext = (a.get_text(" ", strip=True) or "").lower()
            if any(k in atext for k in ("official", "website", "apply",
                                        "source", "visit", "homepage", "learn more")):
                s += 2.0
            if s > best_s:
                best_s, best = s, href
        if best and best_s >= _MIN_DOMAIN_SCORE:
            return best
    return None


def resolve(title: str, funder: str | None = None, *, num: int = 10) -> str | None:
    """Best canonical PRIMARY source URL for an aggregator-listed call, or None.

    Strategy: phrase-search Google (Serper); (1) take the knowledge-graph website
    if present (strongest direct signal); else (2) rank organic hits by how well
    the funder's NAME tokens echo the domain (the key heuristic — a funder's own
    site says its name); else (3) follow the top hits' outbound 'official/apply'
    backlink (for funders whose domain doesn't match the name). Aggregators,
    blogs, news/PR and social are excluded throughout."""
    title = (title or "").strip()
    if not title or not available():
        return None
    try:
        from core.web_search import _secret, _serper_raw
        key = _secret("SERPER_API_KEY")
        f = (funder or "").strip()
        # A real funder name sharpens the search; an aggregator label is noise.
        if f and "aggregator" not in f.lower() and "developmentaid" not in f.lower():
            query = f'"{title}" {f}'
        else:
            query = f'"{title}"'
            f = ""
        raw = _serper_raw(query, key, num) or {}
        items = raw.get("organic") or []
        name_tokens = _name_tokens(title, f)

        # 1) Knowledge-graph official website — the most direct primary signal.
        kg = (raw.get("knowledgeGraph") or {}).get("website")
        if kg and _ok_primary(kg):
            return kg

        # 2) Rank organic results by domain↔name-token overlap.
        ranked = []
        for it in items:
            url = it.get("link") or it.get("url") or ""
            if not _ok_primary(url):
                continue
            s = _score_domain(url, name_tokens)
            blob = f"{it.get('title','')} {it.get('snippet') or it.get('content','')}"
            if _RFP_HINT_RE.search(blob):
                s += 1.0
            ranked.append((s, url))
        ranked.sort(key=lambda x: x[0], reverse=True)
        if ranked and ranked[0][0] >= _MIN_DOMAIN_SCORE:
            return ranked[0][1]

        # 3) Back-link fallback (domain doesn't echo the name).
        back = _resolve_via_backlink(items, name_tokens)
        if back:
            return back

        # 4) Last resort: best non-excluded with any positive name overlap.
        if ranked and ranked[0][0] > 0:
            return ranked[0][1]
        return None
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
        cand["call_submission_deadline"] = dl
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
