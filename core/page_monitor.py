"""Page-change detection for `method: manual` sources.

Manual sources (CZI, Mastercard, WHO ETDR, EC EU Portal, etc.) aren't
scraped — they require human review. To make them visible we hash each
URL's HTML content on every scan and emit a scan_logs row whenever the
hash changes vs. the last seen value. The admin team can then re-check
the page manually.

Storage: hashes live in `app_settings` under the key
`manual_source_hashes` as a JSON dict { url: sha256_hex }.

Cost: one HTTP HEAD/GET per manual source per scan. No external
dependencies, no API keys. Free.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

import requests

from core.settings import get_setting, set_setting

log = logging.getLogger(__name__)

HASH_KEY = "manual_source_hashes"
HTTP_TIMEOUT = 12
USER_AGENT = (
    "Mozilla/5.0 (compatible; RFPIS-PageMonitor/1.0; "
    "+contact: bdt@clintonhealthaccess.org)"
)


def _load_hashes() -> dict[str, str]:
    raw = get_setting(HASH_KEY)
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {str(k): str(v) for k, v in d.items()}
    except (ValueError, TypeError):
        pass
    return {}


def _save_hashes(hashes: dict[str, str]) -> None:
    set_setting(HASH_KEY, json.dumps(hashes))


def _fetch_hash(url: str) -> tuple[str | None, str | None]:
    """Return (sha256_hex, error_msg). One of them is None."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html, */*"},
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        r.raise_for_status()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    # Hash response body. Strip common volatile tokens so we don't flag
    # every micro-template-change (script srcs, cache-busted asset URLs).
    body = r.text or ""
    return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest(), None


def check_manual_sources(manual_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hash each manual source URL, compare to stored hash.

    Returns a list of change events:
        [{
            "name": "<source name>",
            "url": "<url>",
            "first_seen": False,            # True if no prior hash
            "old_hash": "<prev>" | None,
            "new_hash": "<curr>",
            "error": "<msg>" | None,
            "duration": <seconds>,
        }, ...]
    Only entries with `changed=True` are interesting to the user — others
    are silent. Hashes are persisted at the end.
    """
    if not manual_sources:
        return []

    hashes = _load_hashes()
    events: list[dict[str, Any]] = []

    for src in manual_sources:
        url = src.get("url")
        name = src.get("name") or url or "(unnamed)"
        if not url:
            continue
        t0 = time.time()
        new_hash, err = _fetch_hash(url)
        duration = time.time() - t0
        if err or not new_hash:
            events.append({
                "name": name, "url": url,
                "first_seen": False,
                "old_hash": hashes.get(url),
                "new_hash": None,
                "error": err,
                "changed": False,
                "duration": duration,
            })
            continue

        old_hash = hashes.get(url)
        first_seen = old_hash is None
        changed = (not first_seen) and (old_hash != new_hash)
        hashes[url] = new_hash
        events.append({
            "name": name, "url": url,
            "first_seen": first_seen,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "error": None,
            "changed": changed,
            "duration": duration,
        })

    # Persist updated hashes for next run.
    _save_hashes(hashes)
    return events


def summarize_change_events(events: list[dict[str, Any]]) -> str:
    """One-line human-readable summary of a check_manual_sources() result."""
    if not events:
        return "no manual sources checked"
    changed = [e for e in events if e.get("changed")]
    first = [e for e in events if e.get("first_seen")]
    errors = [e for e in events if e.get("error")]
    parts = [f"{len(events)} checked"]
    if changed:
        parts.append(f"{len(changed)} changed")
    if first:
        parts.append(f"{len(first)} first-seen")
    if errors:
        parts.append(f"{len(errors)} error(s)")
    return " · ".join(parts)
