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
