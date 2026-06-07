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

import os
import re
from urllib.parse import urlparse

import streamlit as st

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
    t = (title or "").lower()
    link_words = re.sub(r"[-_/]+", " ", (link or "").lower())
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


@st.cache_data(ttl=900, show_spinner=False)
def search(user_terms: str, num: int = 10) -> dict:
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

    items = data.get("results") or []
    results: list[dict] = []
    for it in items:
        link = it.get("url") or ""
        title = _clean(it.get("title") or "")
        snippet = _clean(it.get("content") or "")
        if not link or blacklist.is_blacklisted(link):
            continue
        if not _relevant(title, snippet, link, required, excluded):
            continue
        results.append({"title": title, "link": link, "snippet": snippet,
                        "domain": urlparse(link).netloc})
    return {"ok": True, "configured": True, "query": query,
            "raw_count": len(items), "results": results, "error": None}
