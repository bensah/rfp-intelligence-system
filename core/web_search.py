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


def build_query(user_terms: str) -> str:
    """User keyword + RFP-indicator OR-group."""
    return (f"{(user_terms or '').strip()} {_RFP_OR_GROUP}").strip()


def _clean(text: str) -> str:
    return _TAG_RE.sub("", text or "").strip()


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
    from core import auto_scorer, blacklist

    key = _secret("TAVILY_API_KEY")
    query = build_query(user_terms)
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
        results.append({"title": title, "link": link, "snippet": snippet,
                        "domain": urlparse(link).netloc})
    return {"ok": True, "configured": True, "query": query,
            "raw_count": len(items), "results": results, "error": None}
