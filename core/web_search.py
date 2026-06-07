"""Brave Search API for live web discovery of funding calls.

We use Brave Search (an independent web index with a simple REST API + a free
tier) because Google's Custom Search JSON API was closed to new customers in
2025 — new Cloud projects get a hard "project does not have access" 403, so it
can't be used for a fresh deployment.

Pipeline (unchanged — only the provider differs):
  1. query = user keyword + an RFP-indicator OR-group, biasing toward calls.
  2. call Brave web search.
  3. for each hit: drop blacklisted domains, then run auto_scorer.rfp_signal_gate
     treating it as an open-web source (so explicit call wording in the
     title/snippet is required, not just a donor mention). Survivors returned.

Credential (NOT committed — set in env or Streamlit secrets):
  BRAVE_SEARCH_API_KEY  — free key from https://api-dashboard.search.brave.com/
                          ("Data for Search" free plan).
Degrades gracefully (available() == False) when unset.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import streamlit as st

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

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
    """True only when the Brave API key is configured."""
    return bool(_secret("BRAVE_SEARCH_API_KEY"))


def build_query(user_terms: str) -> str:
    """User keyword + RFP-indicator OR-group."""
    return (f"{(user_terms or '').strip()} {_RFP_OR_GROUP}").strip()


def _clean(text: str) -> str:
    """Strip Brave's <strong> highlight markup from titles/snippets."""
    return _TAG_RE.sub("", text or "").strip()


@st.cache_data(ttl=900, show_spinner=False)
def search(user_terms: str, num: int = 10) -> dict:
    """Run a filtered web search via Brave.

    Returns: {ok, configured, query, raw_count, results:[{title,link,snippet,
              domain}], error}. Cached 15 min per query (Brave free tier is
              rate-limited; this also avoids burning the monthly quota).
    """
    if not available():
        return {"ok": False, "configured": False, "query": "",
                "raw_count": 0, "results": [], "error": None}

    import httpx  # local — keep page load light when web search isn't used
    from core import auto_scorer, blacklist

    query = build_query(user_terms)
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": _secret("BRAVE_SEARCH_API_KEY"),
    }
    params = {
        "q": query,
        "count": max(1, min(int(num), 20)),
        "result_filter": "web",
        "safesearch": "off",
    }
    try:
        resp = httpx.get(_ENDPOINT, params=params, headers=headers, timeout=12.0)
        if resp.status_code != 200:
            # Surface Brave's actual reason (e.g. invalid key / quota) rather
            # than a bare status code.
            msg = f"HTTP {resp.status_code}"
            try:
                j = resp.json()
                if isinstance(j, dict):
                    err = j.get("error") or {}
                    msg = (err.get("detail") or err.get("message")
                           or j.get("message") or msg)
            except Exception:
                pass
            return {"ok": False, "configured": True, "query": query,
                    "raw_count": 0, "results": [], "error": str(msg)[:200]}
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI
        return {"ok": False, "configured": True, "query": query,
                "raw_count": 0, "results": [], "error": str(exc)[:200]}

    items = ((data.get("web") or {}).get("results")) or []
    results: list[dict] = []
    for it in items:
        link = it.get("url") or ""
        title = _clean(it.get("title") or "")
        snippet = _clean(it.get("description") or "")
        if not link or blacklist.is_blacklisted(link):
            continue
        candidate = {
            "opportunity_title": title,
            "brief_description": snippet,
            "opportunity_link": link,
            # Open-web source → gate demands explicit call wording.
            "_source_origin": "google alert",
        }
        ok, _ = auto_scorer.rfp_signal_gate(candidate)
        if not ok:
            continue
        domain = (it.get("meta_url") or {}).get("hostname") or urlparse(link).netloc
        results.append({"title": title, "link": link, "snippet": snippet,
                        "domain": domain})
    return {"ok": True, "configured": True, "query": query,
            "raw_count": len(items), "results": results, "error": None}
