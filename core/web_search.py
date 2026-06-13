"""Live web discovery of funding calls — Serper (Google) + Tavily, recall-first.

Why two providers: Google Custom Search JSON API is closed to new customers
(hard 403) and Brave needs a card. Serper.dev gives real Google SERPs (2,500
free queries, no card) — best recall for fresh/niche calls — and Tavily adds an
LLM-tuned index. We fan out across BOTH; either alone works.

Pipeline:
  1. build_queries() expands ONE keyword into several RFP-framed queries (a
     broad term like "health" fans out across sub-themes; a specific term like
     "malaria" stays focused). Geo is NOT injected into the query — region-wide
     calls rarely name a country, and forcing one would drop valid RFPs.
  2. run every (provider, query) pair concurrently; dedupe by URL.
  3. lenient post-filter: blacklist + health-theme + RFP signal; deep-read the
     top candidates to confirm a real call and drop only confidently-expired or
     very-old-with-no-future-deadline pages. A human reviews the rest.

Credentials (NOT committed — set in env / Streamlit secrets / GitHub Actions):
  SERPER_API_KEY  — from https://serper.dev (real Google; best recall).
  TAVILY_API_KEY  — from https://app.tavily.com/ (starts with "tvly-").
  EXA_API_KEY     — from https://exa.ai (neural/semantic; usage-priced).
Degrades gracefully (available() == False) only when ALL are unset; any one
provider alone works, and configured providers are fanned out together.
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

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_SERPER_ENDPOINT = "https://google.serper.dev/search"  # real Google SERPs
_EXA_ENDPOINT = "https://api.exa.ai/search"            # neural / semantic

# OR-group that biases the web search toward real calls. The post-filter
# (rfp_signal_gate) does the precise filtering, so this just improves recall.
# Kept short so Google (Serper) doesn't over-constrain — long boolean queries
# tank Google recall.
_RFP_OR_GROUP = (
    '("call for proposals" OR "request for proposals" OR '
    '"funding opportunity" OR "request for applications" OR '
    '"expression of interest")'
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
    """True when ANY web-search provider is configured (Serper / Tavily / Exa)."""
    return bool(_secret("SERPER_API_KEY") or _secret("TAVILY_API_KEY")
                or _secret("EXA_API_KEY"))


# Broad themes that should FAN OUT into many sub-theme queries, so a single
# keyword ("health") sweeps the whole global-health portfolio instead of one
# literal search. Anything not in this set is treated as a specific topic
# (e.g. "malaria") and searched as-is, just wrapped in the RFP framing.
_BROAD_TERMS = {"", "health", "global health", "public health",
                "healthcare", "health care", "all", "any"}

# Health sub-theme pivots used to expand a broad term. Each becomes its own
# RFP-framed query. Geo is deliberately NOT injected into the query: forcing a
# country name would EXCLUDE valid region-wide calls (e.g. CGD's "Global South"
# RfP that never says "Cameroon"). Geography is judged later, leniently.
_HEALTH_PIVOTS = (
    "global health", "maternal and child health",
    "sexual and reproductive health", "infectious disease",
    "HIV AIDS", "malaria", "tuberculosis",
    "neglected tropical diseases", "non-communicable diseases",
    "health systems strengthening", "immunization vaccines",
    "nutrition", "water sanitation hygiene WASH", "pandemic preparedness",
)


def _is_broad(term: str) -> bool:
    return (term or "").strip().lower() in _BROAD_TERMS


def build_queries(user_terms: str, max_queries: int = 14) -> list[str]:
    """Expand ONE keyword into the RFP queries our philosophy implies.

    * Broad term ("health") → fan out across `_HEALTH_PIVOTS`, each wrapped in
      the RFP OR-group → one search sweeps SRHR / maternal / NTD / etc.
    * Specific term ("malaria") → keep the term, wrap in the RFP OR-group, plus
      a couple of natural phrasings — restricted to that topic.

    No geo in the query (recall-first; region-wide calls rarely name a country).
    """
    term = (user_terms or "").strip()
    if _is_broad(term):
        topics = list(_HEALTH_PIVOTS)
        out = [f"{t} {_RFP_OR_GROUP}" for t in topics]
    else:
        out = [
            f"{term} {_RFP_OR_GROUP}",
            f'"{term}" "call for proposals" 2026',
            f'"{term}" grant "funding opportunity"',
        ]
    seen, res = set(), []
    for q in out:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            res.append(q)
        if len(res) >= max_queries:
            break
    return res


def build_query(user_terms: str, geo_terms: list[str] | None = None) -> str:
    """Back-compat single-query builder (now just the first expanded query)."""
    qs = build_queries(user_terms)
    return qs[0] if qs else (user_terms or "").strip()


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
    # RECALL-FIRST: any funding/call signal — strong phrase, RFP acronym, OR
    # weaker application wording — keeps the result. We deliberately do NOT
    # drop blog/news/report URLs here anymore: real calls are often published
    # as blog posts (e.g. CGD's $5M RfP lives on a /blog/ URL). Precision is
    # recovered later by the page-body validation (rfp_ok) during deep-read,
    # and a human reviews what's left.
    if (any(p in body for p in A._RFP_STRONG_PHRASES)
            or A._has_rfp_acronym(f"{title} {snippet}")
            or any(p in body for p in A._RFP_WEAK_PHRASES)):
        return True
    return False


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


def _tavily_items(query: str, key: str, num: int) -> list[dict]:
    """One Tavily call → normalized [{url,title,content}]. Never raises."""
    import httpx
    try:
        r = httpx.post(
            _TAVILY_ENDPOINT,
            json={"api_key": key, "query": query,
                  "max_results": max(1, min(int(num), 20)),
                  "search_depth": "basic"},
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"},
            timeout=15.0,
        )
        if r.status_code != 200:
            return []
        return [{"url": x.get("url"), "title": x.get("title"),
                 "content": x.get("content")}
                for x in (r.json().get("results") or [])]
    except Exception:
        return []


def _serper_items(query: str, key: str, num: int) -> list[dict]:
    """One Serper (real Google) call → normalized [{url,title,content}].
    Never raises. Serper returns organic[].{link,title,snippet}."""
    import httpx
    try:
        r = httpx.post(
            _SERPER_ENDPOINT,
            json={"q": query, "num": max(1, min(int(num), 20))},
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            timeout=15.0,
        )
        if r.status_code != 200:
            return []
        return [{"url": x.get("link"), "title": x.get("title"),
                 "content": x.get("snippet")}
                for x in (r.json().get("organic") or [])]
    except Exception:
        return []


def _exa_items(query: str, key: str, num: int) -> list[dict]:
    """One Exa (neural/semantic) call → normalized [{url,title,content}].
    Never raises. Exa works best on a NATURAL query, so we strip the boolean
    OR-group and hand it the topic + plain RFP words. A short text snippet is
    requested so the pre-filter has something to read (small → cheap)."""
    import httpx
    nat = (query.split(" (")[0].strip() or query.strip())
    exa_q = f"{nat} call for proposals funding opportunity grant"
    try:
        r = httpx.post(
            _EXA_ENDPOINT,
            json={"query": exa_q, "numResults": max(1, min(int(num), 20)),
                  "type": "auto",
                  "contents": {"text": {"maxCharacters": 600}}},
            headers={"x-api-key": key, "Content-Type": "application/json"},
            timeout=15.0,
        )
        if r.status_code != 200:
            return []
        return [{"url": x.get("url"), "title": x.get("title"),
                 "content": x.get("text") or x.get("snippet")}
                for x in (r.json().get("results") or [])]
    except Exception:
        return []


@st.cache_data(ttl=900, show_spinner=False)
def search(user_terms: str, num: int = 10, max_age_days: int = 540) -> dict:
    """Fan-out web search across Serper (Google) + Tavily — recall-first.

    Expands ONE keyword (build_queries) into several RFP-framed queries, runs
    each across every configured provider concurrently, dedupes by URL, then
    applies a lenient filter (blacklist + health-theme + RFP signal; drops only
    confidently-expired, or very-old-with-no-future-deadline). A human reviews
    the survivors. Cached 15 min per query.

    Returns: {ok, configured, query, queries, providers, raw_count,
              results:[{title,link,snippet,domain,deadline,page_date}],
              dropped_*, error}.
    """
    ser = _secret("SERPER_API_KEY")
    tav = _secret("TAVILY_API_KEY")
    exa = _secret("EXA_API_KEY")
    if not (ser or tav or exa):
        return {"ok": False, "configured": False, "query": "", "queries": [],
                "providers": [], "raw_count": 0, "results": [], "error": None}

    import httpx  # local — keep page load light when web search isn't used
    from core import blacklist, policies

    pol = policies.get_policies()
    themes = pol.get("themes", {}) or {}
    required = [t.lower() for t in (themes.get("required_any") or [])]
    excluded = [t.lower() for t in (themes.get("excluded_any") or [])]

    queries = build_queries(user_terms)
    providers = [p for p, k in (("serper", ser), ("tavily", tav), ("exa", exa)) if k]
    per_query = max(1, min(int(num), 15))

    def _run(provider: str, q: str) -> list[dict]:
        if provider == "serper":
            return _serper_items(q, ser, per_query)
        if provider == "exa":
            return _exa_items(q, exa, per_query)
        return _tavily_items(q, tav, per_query)

    # Fan out: every (provider, query) pair concurrently.
    raw: list[dict] = []
    tasks = [(p, q) for q in queries for p in providers]
    try:
        with _cf.ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_run, p, q): (p, q) for p, q in tasks}
            for f in _cf.as_completed(futs):
                try:
                    raw.extend(f.result() or [])
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "configured": True, "query": queries[0] if queries else "",
                "queries": queries, "providers": providers, "raw_count": 0,
                "results": [], "error": str(exc)[:200]}

    # Dedupe by normalized URL (strip fragment + trailing slash).
    by_url: dict[str, dict] = {}
    for it in raw:
        u = (it.get("url") or "").strip()
        if not u:
            continue
        nu = u.split("#")[0].rstrip("/").lower()
        by_url.setdefault(nu, it)
    items = list(by_url.values())

    # 1) Cheap pre-filter on snippets: blacklist + health/RFP signal, then the
    #    SAME geo + scholarship gates the scanner uses — drop calls whose scope
    #    clearly excludes us (Ukraine / UK-Indonesia / Canada-Fund-style country
    #    lists) and individual scholarships. Undefined-geo calls still pass
    #    (slip in for review).
    from core import auto_scorer as A
    candidates: list[dict] = []
    dropped_geo = dropped_scholarship = dropped_offtopic = dropped_lang = 0
    for it in items:
        link = it.get("url") or ""
        title = _clean(it.get("title") or "")
        snippet = _clean(it.get("content") or "")
        if not link or blacklist.is_blacklisted(link):
            continue
        if not _relevant(title, snippet, link, required, excluded):
            continue
        cand = {"opportunity_title": title, "brief_description": snippet,
                "opportunity_link": link}
        if A.individual_award_reject(cand)[0]:
            dropped_scholarship += 1
            continue
        # Jobs / vacancies / clearly non-funding pages (course / policy).
        if A.non_funding_reject(cand)[0]:
            dropped_offtopic += 1
            continue
        # Non-Latin (Arabic / CJK / Cyrillic) — English/French only.
        if not A.language_eligible(cand)[0]:
            dropped_lang += 1
            continue
        # Defined scope that excludes our region/countries → drop.
        if A._geo_strength(cand, pol) == "foreign":
            dropped_geo += 1
            continue
        candidates.append({"title": title, "link": link, "snippet": snippet,
                           "domain": urlparse(link).netloc})

    # 2) Deep validation: crawl the top candidates ONCE (concurrently) — content
    #    date + whether the PAGE BODY carries call wording (kills journals /
    #    overview pages, confirms a real call like the CGD /blog/ RfP).
    sigs: dict[str, dict] = {}
    fetch_list = candidates[:30]
    if fetch_list:
        try:
            with httpx.Client() as client:
                with _cf.ThreadPoolExecutor(max_workers=8) as ex:
                    futs = {ex.submit(_fetch_signals, c["link"], client): c["link"]
                            for c in fetch_list}
                    for f in _cf.as_completed(futs):
                        try:
                            sigs[futs[f]] = f.result()
                        except Exception:
                            sigs[futs[f]] = {"fetched": False}
        except Exception:
            pass

    from core.scraper import _extract_deadline_from_text
    today = date.today()
    cutoff = (today - timedelta(days=max_age_days)) if max_age_days else None
    results: list[dict] = []
    dropped_expired = dropped_old = dropped_notrfp = 0
    for c in candidates:
        sig = sigs.get(c["link"]) or {"fetched": False}
        try:
            dl = _extract_deadline_from_text(f"{c['title']}. {c['snippet']}")
        except Exception:
            dl = None
        pdate = sig.get("date") or _url_date(c["link"])
        future_dl = bool(dl and dl >= today)
        # Confidently expired → out.
        if dl and dl < today:
            dropped_expired += 1
            continue
        # Very old page (>~18 mo) with no future deadline → out. Lenient on
        # purpose so a recent call on an older-looking page survives.
        if not future_dl and cutoff and pdate and pdate < cutoff:
            dropped_old += 1
            continue
        # Body isn't a call (and not open via a future deadline) → out. Only
        # when we actually fetched and validated the page.
        if sig.get("fetched") and not future_dl and not sig.get("rfp_ok"):
            dropped_notrfp += 1
            continue
        results.append({"title": c["title"], "link": c["link"],
                        "snippet": c["snippet"], "domain": c["domain"],
                        "deadline": dl.isoformat() if dl else "",
                        "page_date": pdate.isoformat() if pdate else ""})

    return {"ok": True, "configured": True,
            "query": queries[0] if queries else "",
            "queries": queries, "providers": providers,
            "raw_count": len(items), "results": results[:30],
            "dropped_expired": dropped_expired, "dropped_old": dropped_old,
            "dropped_notrfp": dropped_notrfp, "dropped_geo": dropped_geo,
            "dropped_scholarship": dropped_scholarship,
            "dropped_offtopic": dropped_offtopic, "dropped_lang": dropped_lang,
            "error": None}
