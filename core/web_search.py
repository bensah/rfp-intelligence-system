"""Tavily Search API for live web discovery of funding calls.

Provider history: Google Custom Search JSON API is closed to new customers
(hard 403), and Brave's API needs a card on file — so we use Tavily, which has
a genuine no-card free tier (~1,000 searches/month) and a simple REST API
designed for exactly this kind of programmatic web search.

Pipeline (unchanged across providers — only the HTTP call differs):
  1. query = user keyword + an RFP-indicator OR-group, biasing toward calls.
  2. call Tavily web search.
  3. for each hit: drop blacklisted domains, then run auto_scorer.rfp_signal_gate
     treating it as an open-web source (so explicit call wording in the
     title/snippet is required, not just a donor mention). Survivors returned.

Credential (NOT committed — set in env or Streamlit secrets):
  TAVILY_API_KEY  — free key from https://app.tavily.com/ (starts with "tvly-").
Degrades gracefully (available() == False) when unset.
"""
from __future__ import annotations

import concurrent.futures as _cf
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import streamlit as st
from dotenv import load_dotenv

# Load .env so the key resolves when running locally (`streamlit run App.py`),
# independent of import order. No-op on Streamlit Cloud (uses st.secrets there).
load_dotenv()

_ENDPOINT = "https://api.tavily.com/search"

# OR-group that biases the web search toward real calls. The post-filter
# (rfp_signal_gate) does the precise filtering, so this just improves recall.
_RFP_OR_GROUP = (
    '("request for proposals" OR "call for proposals" OR '
    '"request for applications" OR "request for information" OR '
    '"expression of interest" OR "notice of funding" OR '
    '"grand challenge" OR "funding opportunity")'
)

_TAG_RE = re.compile(r"<[^>]+>")

# URL path fragments that mark academic/publication content (books, chapters,
# DOIs, journal issues) — not funding calls. Dropped before crawling. This
# generalises the "link.springer.com/book/…" case beyond a single domain.
_URL_NOISE = (
    "/book/", "/books/", "/chapter/", "/doi/", "/abstract", "/proceedings/",
    "/issue/", "/article-abstract", "/citation/", "/fulltext", "/epdf/",
)


def _secret(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return None


def available() -> bool:
    """True only when the Tavily API key is configured."""
    return bool(_secret("TAVILY_API_KEY"))


def build_query(user_terms: str, geo_terms: list[str] | None = None) -> str:
    """User keyword + RFP-indicator OR-group + (optional) geographic-scope
    OR-group, so the web search is biased toward calls in our geography."""
    q = f"{(user_terms or '').strip()} {_RFP_OR_GROUP}"
    if geo_terms:
        geo = " OR ".join(f'"{g}"' if " " in g else g for g in geo_terms[:6])
        q += f" ({geo})"
    return q.strip()


def _clean(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _relevant(title: str, snippet: str, link: str,
              required: list[str], excluded: list[str]) -> bool:
    """Relevance filter for MANUAL web discovery — HEALTH-FIRST, then RFP.

    Tuned for recall (a human reviews the hits) but enforces the deploying
    org's configuration:
      * must mention a configured health theme (themes.required_any) — drops
        IT / logistics / energy / sports tenders that merely matched the query;
      * dropped if it hits a configured exclusion (themes.excluded_any, e.g.
        clinical-trial / basic-research);
      * must then show a call/funding signal (strong phrase, RFP acronym, or
        weaker application wording) and not be an obvious non-call page type.
    `required`/`excluded` are passed in (fetched once per search) to avoid a
    settings read per result.
    """
    from core import auto_scorer as A
    raw_link = (link or "").lower()
    # Publication/book/journal URLs are never funding calls → drop.
    if any(p in raw_link for p in _URL_NOISE):
        return False
    t = (title or "").lower()
    link_words = re.sub(r"[-_/]+", " ", raw_link)
    body = f"{t} {(snippet or '').lower()}"
    if any(p in f"{body} {link_words}" for p in A._ERROR_PAGE_PATTERNS):
        return False
    # Config exclusions (clinical trial / basic research …) → drop.
    if any(x in body for x in excluded):
        return False
    # HEALTH-FIRST: must mention a configured health theme.
    if required and not any(h in body for h in required):
        return False
    # Strong call wording or an RFP acronym anywhere in title/snippet → keep.
    if (any(p in body for p in A._RFP_STRONG_PHRASES)
            or A._has_rfp_acronym(f"{title} {snippet}")):
        return True
    # Clear non-call page type (about / blog / privacy / report …) → drop.
    if any(p in f"{t} {link_words}" for p in A._NON_RFP_PATTERNS):
        return False
    # Weaker funding / application wording → keep (a human will vet it).
    if any(p in body for p in A._RFP_WEAK_PHRASES):
        return True
    return False


_HEALTH_PIVOTS = (
    "global health funding", "infectious disease grant",
    "maternal and child health RFP", "non-communicable disease funding",
    "health systems strengthening grant", "vaccine research funding",
)


def suggest_terms(user_terms: str, limit: int = 6) -> list[str]:
    """Related / alternative searches to widen discovery from the query."""
    q = (user_terms or "").strip()
    ql = q.lower()
    out: list[str] = []
    if q:
        for m in ("grant", "call for proposals", "funding opportunity"):
            if m not in ql:
                out.append(f"{q} {m}")
    out.extend(_HEALTH_PIVOTS)
    seen, res = set(), []
    for s in out:
        k = s.lower()
        if k != ql and k not in seen:
            seen.add(k)
            res.append(s)
    return res[:limit]


def _safe_parse_date(s: str) -> date | None:
    try:
        from dateutil import parser as _dp
        return _dp.parse(s, default=datetime(2000, 1, 1),
                         ignoretz=True).date()
    except Exception:
        return None


# FIRST-POSTED date fields ONLY. We deliberately exclude modified / updated /
# lastmod / Last-Modified so a CMS re-touch can't make an old RFP look recent —
# recency is judged by when the call was POSTED, not last edited.
_JSONLD_PUB_RE = re.compile(
    r'"date(?:Published|Created)"\s*:\s*"([^"]{6,40})"', re.I)
_PUB_META_KEYS = (
    "article:published_time", "datepublished", "dc.date", "dcterms.date",
    "dcterms.created", "dcterms.issued", "date", "pubdate", "publishdate",
    "publication_date", "sailthru.date", "parsely-pub-date",
)
_META_A_RE = re.compile(
    r'<meta[^>]+(?:property|name|itemprop)=["\']([^"\']+)["\'][^>]*?'
    r'content=["\']([^"\']+)["\']', re.I)
_META_B_RE = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?'
    r'(?:property|name|itemprop)=["\']([^"\']+)["\']', re.I)


_UA = "Mozilla/5.0 (compatible; RFPIS-discovery/1.0; +https://example.org)"
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_TAGS_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    s = _SCRIPT_RE.sub(" ", s or "")
    s = _TAGS_RE.sub(" ", s)
    return _WS_RE.sub(" ", s)


def _page_date_from_html(headers, html_txt: str) -> date | None:
    """FIRST-POSTED date — datePublished / article:published_time / dc.date /
    JSON-LD datePublished. Ignores modified/updated/Last-Modified so a 2024 RFP
    re-touched in 2026 is still judged by when it was POSTED. Returns the
    EARLIEST such date (the original posting), or None."""
    found: list[date] = []
    for m in _JSONLD_PUB_RE.finditer(html_txt):
        d = _safe_parse_date(m.group(1))
        if d:
            found.append(d)
    for rx in (_META_A_RE, _META_B_RE):
        for m in rx.finditer(html_txt):
            key = (m.group(1) if rx is _META_A_RE else m.group(2)).lower()
            val = (m.group(2) if rx is _META_A_RE else m.group(1))
            if key in _PUB_META_KEYS:
                d = _safe_parse_date(val)
                if d:
                    found.append(d)
    return min(found) if found else None


_URL_DATE_RE = re.compile(r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:[/-]|$)")
_URL_YM_RE = re.compile(r"/(20\d{2})[/-](\d{1,2})(?:/|$)")


def _url_date(url: str) -> date | None:
    """Date embedded in the URL path (e.g. /2024/08/09/slug → 2024-08-09, or
    /2024/08/ → 2024-08-01). Common for WordPress/blog permalinks — a cheap
    recency clue needing no fetch."""
    if not url:
        return None
    m = _URL_DATE_RE.search(url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = _URL_YM_RE.search(url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            pass
    return None


def _fetch_signals(url: str, client) -> dict:
    """Crawl one result page ONCE and return validation signals:
      * date    — FIRST-POSTED date (datePublished / article:published_time /
                  dc.date), NOT last-modified;
      * rfp_ok  — the PAGE BODY carries real call wording / an RFP acronym
                  (validates it's actually a call, not just a snippet match).
    `fetched` is False when the page couldn't be read (we then stay lenient).

    We deliberately do NOT parse a deadline from the full body — pages are full
    of unrelated dates (copyright, archives, events) that produced false
    'expired' drops. The snippet deadline + the metadata date are the reliable
    signals; rfp_ok confirms it's a real call."""
    out = {"date": None, "rfp_ok": False, "fetched": False}
    try:
        r = client.get(url, timeout=7.0, follow_redirects=True,
                       headers={"User-Agent": _UA})
    except Exception:
        return out
    out["fetched"] = True
    html_txt = (r.text or "")[:400000]
    out["date"] = _page_date_from_html(r.headers, html_txt)

    text = _strip_html(html_txt)[:60000]
    try:
        from core import auto_scorer as A
        tl = text.lower()
        out["rfp_ok"] = (any(p in tl for p in A._RFP_STRONG_PHRASES)
                         or A._has_rfp_acronym(text))
    except Exception:
        out["rfp_ok"] = True  # fail-open: don't reject on validation error
    return out


@st.cache_data(ttl=900, show_spinner=False)
def search(user_terms: str, num: int = 10, max_age_days: int = 90) -> dict:
    """Run a filtered web search via Tavily.

    Returns: {ok, configured, query, raw_count, results:[{title,link,snippet,
              domain}], error}. Cached 15 min per query to conserve the free
              monthly credit allowance.
    """
    if not available():
        return {"ok": False, "configured": False, "query": "",
                "raw_count": 0, "results": [], "error": None}

    import httpx  # local — keep page load light when web search isn't used
    from core import blacklist, policies

    # Pull config ONCE per search (not per result): geographic scope biases the
    # query; health themes + exclusions drive the post-filter.
    pol = policies.get_policies()
    countries = pol.get("countries", {}) or {}
    themes = pol.get("themes", {}) or {}
    geo_terms = ((countries.get("broad_terms") or [])[:4]
                 + (countries.get("eligible") or []))
    required = [t.lower() for t in (themes.get("required_any") or [])]
    excluded = [t.lower() for t in (themes.get("excluded_any") or [])]

    key = _secret("TAVILY_API_KEY")
    query = build_query(user_terms, geo_terms)
    payload = {
        "api_key": key,                       # body auth (stable across versions)
        "query": query,
        "max_results": max(1, min(int(num), 20)),
        "search_depth": "basic",              # 1 credit/search
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",     # header auth (newer API) — harmless if unused
    }
    try:
        resp = httpx.post(_ENDPOINT, json=payload, headers=headers, timeout=15.0)
        if resp.status_code != 200:
            msg = f"HTTP {resp.status_code}"
            try:
                j = resp.json()
                if isinstance(j, dict):
                    msg = (j.get("detail") or j.get("error")
                           or j.get("message") or msg)
            except Exception:
                pass
            return {"ok": False, "configured": True, "query": query,
                    "raw_count": 0, "results": [], "error": str(msg)[:200]}
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI
        return {"ok": False, "configured": True, "query": query,
                "raw_count": 0, "results": [], "error": str(exc)[:200]}

    today = date.today()
    cutoff = (today - timedelta(days=max_age_days)) if max_age_days else None
    items = data.get("results") or []

    # 1) Cheap pre-filter on Tavily snippets (health + RFP signal + blacklist)
    #    to decide which pages are worth crawling.
    candidates: list[dict] = []
    for it in items:
        link = it.get("url") or ""
        title = _clean(it.get("title") or "")
        snippet = _clean(it.get("content") or "")
        if not link or blacklist.is_blacklisted(link):
            continue
        if not _relevant(title, snippet, link, required, excluded):
            continue
        candidates.append({"title": title, "link": link, "snippet": snippet,
                           "domain": urlparse(link).netloc})

    # 2) Deep validation: crawl each candidate ONCE (concurrently) and read its
    #    real signals — content date, whether the PAGE BODY carries call wording,
    #    and any deadline in the body. This is what removes very old posts
    #    (e.g. a 2012 'Request for Proposals') and pages that aren't real calls.
    sigs: dict[str, dict] = {}
    if candidates:
        try:
            with httpx.Client() as client:
                with _cf.ThreadPoolExecutor(max_workers=8) as ex:
                    futs = {ex.submit(_fetch_signals, c["link"], client): c["link"]
                            for c in candidates[:20]}
                    for f in _cf.as_completed(futs):
                        try:
                            sigs[futs[f]] = f.result()
                        except Exception:
                            sigs[futs[f]] = {"fetched": False}
        except Exception:
            pass

    from core.scraper import _extract_deadline_from_text
    results: list[dict] = []
    dropped_expired = dropped_old = dropped_notrfp = 0
    for c in candidates:
        sig = sigs.get(c["link"]) or {"fetched": False}
        try:
            dl = _extract_deadline_from_text(f"{c['title']}. {c['snippet']}")
        except Exception:
            dl = None
        # Page date: metadata (latest of publish/modify) wins; fall back to a
        # date embedded in the URL (e.g. /2024/08/09/) — no fetch needed.
        pdate = sig.get("date") or _url_date(c["link"])
        future_dl = bool(dl and dl >= today)

        # Expired deadline → out.
        if dl and dl < today:
            dropped_expired += 1
            continue
        # Stale: old page (metadata date OR a date in the URL) with no future
        # deadline → out. Applies even when the page couldn't be fetched.
        if not future_dl and cutoff and pdate and pdate < cutoff:
            dropped_old += 1
            continue
        # Page body isn't actually a call (and not open via a future deadline)
        # → out — kills journals / overview pages. Only when we fetched it.
        if sig.get("fetched") and not future_dl and not sig.get("rfp_ok"):
            dropped_notrfp += 1
            continue
        results.append({"title": c["title"], "link": c["link"],
                        "snippet": c["snippet"], "domain": c["domain"],
                        "deadline": dl.isoformat() if dl else "",
                        "page_date": pdate.isoformat() if pdate else ""})

    return {"ok": True, "configured": True, "query": query,
            "raw_count": len(items), "results": results,
            "dropped_expired": dropped_expired, "dropped_old": dropped_old,
            "dropped_notrfp": dropped_notrfp, "error": None}
