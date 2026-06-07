"""Google Programmable Search (Custom Search JSON API) for live web discovery.

Lets the team manually search the public web for funding calls and keep only
the results that pass the SAME rules the scanner uses — so the noise the open
web is full of (overview pages, news, expired calls) is filtered out and what
remains matches our configuration.

Pipeline:
  1. Build a query = user keyword + an RFP-indicator OR-group, biasing Google
     toward actual calls (RFP / EOI / grand challenge / funding opportunity …).
  2. Call the Custom Search JSON API.
  3. For each hit, drop blacklisted domains, then run auto_scorer.rfp_signal_gate
     treating it as an open-web source (so a real call CTA in the title/snippet
     is required, not just a donor mention). Survivors are returned.

Credentials (NOT committed — set in env or Streamlit secrets):
  GOOGLE_CSE_API_KEY  — Custom Search JSON API key
  GOOGLE_CSE_ID       — Programmable Search Engine id (the "cx" value)
The Programmable Search Engine itself (in Google's console) can be set to search
the whole web or restricted to specific donor sites; this module post-filters
either way. Degrades gracefully (available() == False) when unset.
"""
from __future__ import annotations

import os

import streamlit as st

_ENDPOINT = "https://www.googleapis.com/customsearch/v1"

# OR-group that biases Google toward real calls. Whole-phrase quoted so Google
# treats them as units; acronyms are upper-cased (Google search is case-
# insensitive but it reads clearly).
_RFP_OR_GROUP = (
    '("request for proposals" OR "call for proposals" OR '
    '"request for applications" OR "request for information" OR '
    '"expression of interest" OR "notice of funding" OR '
    '"grand challenge" OR "funding opportunity" OR '
    'RFP OR RFA OR EOI OR NOFO OR FOA)'
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
    """True only when both API credentials are configured."""
    return bool(_secret("GOOGLE_CSE_API_KEY") and _secret("GOOGLE_CSE_ID"))


def build_query(user_terms: str) -> str:
    """User keyword + RFP-indicator OR-group."""
    ut = (user_terms or "").strip()
    return (f"{ut} {_RFP_OR_GROUP}").strip()


@st.cache_data(ttl=900, show_spinner=False)
def search(user_terms: str, num: int = 10) -> dict:
    """Run a filtered web search.

    Returns: {ok: bool, configured: bool, query: str, raw_count: int,
              results: [{title, link, snippet, domain}], error: str|None}.
    Cached 15 min per query so repeated clicks don't burn the API quota
    (Custom Search free tier = 100 queries/day).
    """
    if not available():
        return {"ok": False, "configured": False, "query": "",
                "raw_count": 0, "results": [], "error": None}

    import httpx  # local — keep page load light when web search isn't used
    from core import auto_scorer, blacklist

    query = build_query(user_terms)
    params = {
        "key": _secret("GOOGLE_CSE_API_KEY"),
        "cx": _secret("GOOGLE_CSE_ID"),
        "q": query,
        "num": max(1, min(int(num), 10)),
    }
    try:
        resp = httpx.get(_ENDPOINT, params=params, timeout=12.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI
        return {"ok": False, "configured": True, "query": query,
                "raw_count": 0, "results": [], "error": str(exc)[:200]}

    items = data.get("items") or []
    results: list[dict] = []
    for it in items:
        link = it.get("link") or ""
        title = it.get("title") or ""
        snippet = it.get("snippet") or ""
        if blacklist.is_blacklisted(link):
            continue
        candidate = {
            "opportunity_title": title,
            "brief_description": snippet,
            "opportunity_link": link,
            # Treat as open-web so the gate demands explicit call wording in
            # the title/snippet rather than trusting the source.
            "_source_origin": "google alert",
        }
        ok, _ = auto_scorer.rfp_signal_gate(candidate)
        if not ok:
            continue
        results.append({
            "title": title, "link": link, "snippet": snippet,
            "domain": it.get("displayLink") or "",
        })
    return {"ok": True, "configured": True, "query": query,
            "raw_count": len(items), "results": results, "error": None}
