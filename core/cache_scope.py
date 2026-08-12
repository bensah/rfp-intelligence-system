"""The tenant discriminator that every process-global cache key must carry.

`st.cache_data` caches are PROCESS-GLOBAL — shared by every session, and therefore by every
tenant signed into that process. The rows behind them are tenant-scoped by `get_client()`. So a
cached loader with no tenant in its key serves whichever tenant rendered first to all the others.

That is not hypothetical. The report's loaders took a scope argument for exactly this reason and
still leaked, because the parameter was named `_scope`: Streamlit EXCLUDES underscore-prefixed
arguments from a cache key, so the safeguard was disabled by its own name. The report showed
another tenant's rows — 161 auto-scan rows over two months where the tenant's own data spanned
seven months and thirteen people.

Two rules, both enforced by tests:

  1. Every cached function that reads tenant-scoped data takes a scope argument.
  2. That argument's name does NOT begin with an underscore.

`scope_key()` returns the same discriminator `get_client()` scopes by, so the key and the query
can never disagree: a tenant id for a scoped user, or "t:all" for a super_user / single-tenant
deployment, who genuinely do see everything.
"""
from __future__ import annotations


def scope_key() -> str:
    """Cache-key discriminator for the current session's data scope.

    Never raises: a cache key is not worth taking a page down for, and the fallback is the
    conservative one — "unknown" is its own bucket rather than silently sharing another
    tenant's entry.
    """
    try:
        from db.supabase_client import _tenant_scope_tid
        return f"t:{_tenant_scope_tid() or 'all'}"
    except Exception:
        return "t:unknown"
