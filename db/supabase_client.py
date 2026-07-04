"""Supabase client singleton.

Reads SUPABASE_URL and SUPABASE_KEY from environment (or Streamlit secrets when
running on Streamlit Community Cloud). The service-role key is required for
server-side writes from the scanner; the anon key is sufficient for read-only
dashboards but Phase 1 uses the service-role key throughout.
"""
from __future__ import annotations

import os
import threading

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()


def _force_http1_transport() -> None:
    """Work around an httpx HTTP/2 socket bug that surfaces as
    ``httpx.ReadError: [WinError 10035] A non-blocking socket operation could not
    be completed immediately`` on EVERY Supabase call (seen on Python 3.14 +
    httpx 0.28 on Windows). postgrest hard-codes ``Client(..., http2=True)`` (see
    postgrest/_sync/client.py) and supabase rebuilds that client on auth-state
    changes, so a one-off session swap wouldn't stick. Each sub-package imports
    the httpx client as a module-level ``from httpx import Client``; we replace
    that symbol with an HTTP/1.1 subclass so every session they build (now and on
    rebuild) negotiates HTTP/1.1. Idempotent and fully defensive — never block
    client creation if an internal layout changes."""
    try:
        import httpx
    except Exception:
        return

    # Default network hardening applied to EVERY Supabase session (postgrest,
    # storage, auth) so a dropped connection / slow network doesn't crash the app:
    #   * bounded timeouts — never hang forever under high traffic;
    #   * a transport that AUTO-RETRIES connection failures (ConnectError /
    #     ConnectTimeout — the "internet disruption" crash) at the socket layer, for
    #     every `.execute()`, with no call-site change. (Read errors mid-response are
    #     not retried here — safe_execute() + the App-level boundary cover those.)
    _DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

    class _Http1Client(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["http2"] = False          # force HTTP/1.1
            kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
            # setdefault so an explicitly-passed transport is never overridden.
            if "transport" not in kwargs:
                try:
                    kwargs["transport"] = httpx.HTTPTransport(retries=3, http2=False)
                except Exception:
                    pass                     # fall back to the default transport
            super().__init__(*args, **kwargs)

    for modname in (
        "postgrest._sync.client",
        "storage3._sync.client",
        "supabase_functions._sync.functions_client",
        "supabase_auth._sync.gotrue_base_api",
        "supabase_auth._sync.gotrue_admin_api",
        "supabase_auth._sync.gotrue_client",
    ):
        try:
            mod = __import__(modname, fromlist=["Client"])
        except Exception:
            continue
        # Only patch the genuine httpx.Client (skip if already wrapped on a rerun).
        if getattr(mod, "Client", None) is httpx.Client:
            mod.Client = _Http1Client


_force_http1_transport()


def _read_secret(name: str) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    try:
        import streamlit as st  # type: ignore
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return None


# True single-client singleton with a double-checked lock. (Was @lru_cache, which
# lets a cold-cache traffic spike build many clients simultaneously — the lock
# serializes that so exactly ONE client is created and shared.) `cache_clear` is
# preserved so existing callers (App.py boundary, auth retry) keep working.
_CLIENT: Client | None = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> Client:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:                      # double-check inside the lock
            url = _read_secret("SUPABASE_URL")
            key = _read_secret("SUPABASE_KEY")
            if not url or not key:
                raise RuntimeError(
                    "SUPABASE_URL and SUPABASE_KEY must be set (env or Streamlit secrets)."
                )
            _CLIENT = create_client(url, key)
    return _CLIENT


def _clear_client_cache() -> None:
    """Drop the cached client so the next get_client() rebuilds it (used by the UI
    Retry path after a connectivity failure to discard a possibly half-open pool)."""
    global _CLIENT
    with _CLIENT_LOCK:
        _CLIENT = None


get_client.cache_clear = _clear_client_cache      # keep the lru_cache-era API


# Transient httpx/network errors that should be retried rather than crash the
# page (Supabase occasionally drops a keep-alive connection mid-request).
_TRANSIENT_EXC = (
    "ReadError", "ConnectError", "ConnectTimeout", "ReadTimeout",
    "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
)


def is_connectivity_error(exc: BaseException) -> bool:
    """True when `exc` (or any error it was raised from) is a transient network /
    httpx failure reaching Supabase — as opposed to a real config/logic bug. Walks the
    __cause__/__context__ chain so a wrapped postgrest/httpx error is still recognised.
    Single source of truth reused by auth + the App-level error boundary."""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if type(e).__name__ in _TRANSIENT_EXC or "httpx" in type(e).__module__:
            return True
        e = e.__cause__ or e.__context__
    return False


def safe_execute(query, *, retries: int = 3):
    """Run a postgrest query's `.execute()` with a short retry on transient
    httpx network errors. A single dropped connection otherwise bubbles up as
    `httpx.ReadError` and takes down the whole Streamlit page.

    Pass the query builder WITHOUT calling `.execute()`:
        rows = safe_execute(sb.table("x").select("*").eq("a", 1)).data
    """
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return query.execute()
        except Exception as exc:  # noqa: BLE001 — re-raised below if not transient
            last_exc = exc
            if type(exc).__name__ not in _TRANSIENT_EXC:
                raise
            _time.sleep(0.4 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("safe_execute: no attempts ran")
