"""Polite, shared HTTP layer for all crawling.

Every outbound `requests` call in the crawler should route through here instead
of calling `requests.get/post/head` directly. It gives us three things that a
once-daily-but-bursty scan needs to avoid "Too many requests" bans, WITHOUT
slowing the whole scan down:

  1. Per-host throttle (the speed-preserving lever). A global concurrency cap
     (SCAN_PARALLELISM) would slow EVERY source to protect the one host that
     rate-limits us. Instead we enforce a minimum interval between requests to
     the SAME host (RFPIS_HOST_MIN_INTERVAL, default 1.0s) while leaving global
     parallelism high — many different domains still crawl at full speed, but a
     single domain is never hammered. (Scrapy's CONCURRENT_REQUESTS_PER_DOMAIN +
     DOWNLOAD_DELAY model.)

  2. Graceful 429/503 backoff. A shared Session mounts an adapter that retries
     429/500/502/503/504 with exponential backoff and honours the server's
     `Retry-After` header — so a transient rate-limit self-heals instead of
     failing the source.

  3. TTL response cache (GET only). The verification/expand-listing bursts and
     same-day re-runs re-fetch the same URLs repeatedly; an in-process TTL cache
     (RFPIS_HTTP_CACHE_TTL, default 6h) serves those from memory, cutting both
     latency and load on the host.

Proxies: the Session honours HTTPS_PROXY/HTTP_PROXY env vars automatically (same
as bare `requests`), so the dormant proxy passthrough still works if ever set.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 ships with requests; Retry path differs across versions.
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    Retry = None  # type: ignore

log = logging.getLogger(__name__)


def _f(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, default))
    except (TypeError, ValueError):
        return default


# Minimum seconds between two requests to the SAME host. 0 disables throttling.
HOST_MIN_INTERVAL = _f("RFPIS_HOST_MIN_INTERVAL", 1.0)
# In-process GET cache lifetime, seconds. 0 disables caching.
CACHE_TTL = _f("RFPIS_HTTP_CACHE_TTL", 6 * 3600)

# --- per-host throttle state ------------------------------------------------
_host_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_host_last: dict[str, float] = {}
_locks_guard = threading.Lock()

# --- TTL cache state --------------------------------------------------------
_cache: dict[str, tuple[float, requests.Response]] = {}
_cache_guard = threading.Lock()


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _host_lock(host: str) -> threading.Lock:
    with _locks_guard:
        return _host_locks[host]


def _throttle(host: str) -> None:
    """Block until at least HOST_MIN_INTERVAL has passed since this host's last
    request. Serialises requests to one host without touching other hosts."""
    if HOST_MIN_INTERVAL <= 0 or not host:
        return
    lock = _host_lock(host)
    with lock:
        last = _host_last.get(host)
        if last is not None:
            wait = HOST_MIN_INTERVAL - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        _host_last[host] = time.monotonic()


def _make_retry():
    """Build a urllib3 Retry across versions. The method-allowlist kwarg was
    renamed `method_whitelist` → `allowed_methods` in urllib3 1.26; older builds
    raise TypeError on the new name. We must never let that crash module import
    (it's pulled in by core.scraper → scripts/run_scan.py, so a TypeError here
    would fail the whole scan with exit code 1). Degrade gracefully instead."""
    if Retry is None:
        return None
    base = dict(total=3, connect=2, read=2, status=3, backoff_factor=1.5,
                status_forcelist=(429, 500, 502, 503, 504),
                respect_retry_after_header=True, raise_on_status=False)
    methods = frozenset(["GET", "HEAD", "POST"])
    for kw in ({"allowed_methods": methods}, {"method_whitelist": methods}, {}):
        try:
            return Retry(**base, **kw)
        except TypeError:
            continue
    try:
        return Retry(total=3)  # last resort: defaults only
    except Exception:
        return None


def _build_session() -> requests.Session:
    s = requests.Session()
    try:
        retry = _make_retry()
        if retry is not None:
            adapter = HTTPAdapter(max_retries=retry)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
    except Exception as exc:  # never let retry setup break crawling
        log.warning("http: retry adapter disabled (%s)", exc)
    return s


_session = _build_session()


def _cache_key(method: str, url: str, params) -> str:
    if params:
        try:
            items = sorted(params.items())
        except AttributeError:
            items = sorted(params)
        return f"{method}:{url}?{items}"
    return f"{method}:{url}"


def request(method: str, url: str, **kwargs) -> requests.Response:
    """requests.request with per-host throttle, shared retry/backoff session,
    and a GET-only TTL cache. Drop-in for requests.<method>."""
    method = method.upper()
    cacheable = method == "GET" and CACHE_TTL > 0 and not kwargs.get("stream")
    key = _cache_key(method, url, kwargs.get("params")) if cacheable else None

    if key is not None:
        with _cache_guard:
            hit = _cache.get(key)
            if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
                return hit[1]

    _throttle(_host(url))
    resp = _session.request(method, url, **kwargs)

    if key is not None and resp.status_code == 200:
        # Touch .content so the cached Response is fully read and reusable.
        try:
            _ = resp.content
        except Exception:
            pass
        with _cache_guard:
            _cache[key] = (time.monotonic(), resp)
    return resp


def get(url: str, **kwargs) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return request("POST", url, **kwargs)


def head(url: str, **kwargs) -> requests.Response:
    return request("HEAD", url, **kwargs)


def clear_cache() -> None:
    with _cache_guard:
        _cache.clear()
