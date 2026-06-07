"""Supabase client singleton.

Reads SUPABASE_URL and SUPABASE_KEY from environment (or Streamlit secrets when
running on Streamlit Community Cloud). The service-role key is required for
server-side writes from the scanner; the anon key is sufficient for read-only
dashboards but Phase 1 uses the service-role key throughout.
"""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()


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


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = _read_secret("SUPABASE_URL")
    key = _read_secret("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set (env or Streamlit secrets)."
        )
    return create_client(url, key)


# Transient httpx/network errors that should be retried rather than crash the
# page (Supabase occasionally drops a keep-alive connection mid-request).
_TRANSIENT_EXC = (
    "ReadError", "ConnectError", "ConnectTimeout", "ReadTimeout",
    "WriteError", "WriteTimeout", "PoolTimeout", "RemoteProtocolError",
)


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
