"""User-managed scan blacklist.

Patterns from the `scan_blacklist` table are matched as case-insensitive
SUBSTRINGS against a candidate's opportunity_link. Any match → hard reject at
the eligibility gate (before scoring), so off-topic donor-site sections
(fundraising, shop, careers, social) and sites with no calls page (cdc.gov)
never become records.

Process-level TTL cache (no Streamlit dependency — the scanner runs in a plain
subprocess). Admin edits take effect within `_TTL` seconds, or immediately via
`clear_cache()` after a save.
"""
from __future__ import annotations

import time

from db.supabase_client import get_client

_TTL = 300.0
_CACHE: dict = {"t": 0.0, "patterns": None}


def get_patterns() -> list[str]:
    now = time.time()
    if _CACHE["patterns"] is not None and now - _CACHE["t"] < _TTL:
        return _CACHE["patterns"]
    patterns: list[str] = []
    try:
        rows = get_client().table("scan_blacklist").select("pattern").execute().data or []
        patterns = [
            (r.get("pattern") or "").strip().lower()
            for r in rows
            if (r.get("pattern") or "").strip()
        ]
    except Exception:
        patterns = []  # table missing / DB blip — fail open (don't block scans)
    _CACHE.update(t=now, patterns=patterns)
    return patterns


def is_blacklisted(url: str | None) -> str | None:
    """Return the matching pattern if `url` is blacklisted, else None."""
    if not url:
        return None
    u = url.lower()
    for p in get_patterns():
        if p and p in u:
            return p
    return None


def clear_cache() -> None:
    _CACHE.update(t=0.0, patterns=None)
