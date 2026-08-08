"""Supabase client singleton.

Reads SUPABASE_URL and SUPABASE_KEY from environment (or Streamlit secrets when
running on Streamlit Community Cloud). The service-role key is required for
server-side writes from the scanner; the anon key is sufficient for read-only
dashboards but Phase 1 uses the service-role key throughout.
"""
from __future__ import annotations

import os
import threading

from core.dotenv_compat import load_dotenv   # tolerates a venv without python-dotenv
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


def _session_tenant_client() -> Client | None:
    """Multi-tenant Phase 2: when running INSIDE a Streamlit session that has a tenant
    JWT (set by auth.tenant_context after login), return a PER-SESSION client authed as
    that user — apikey stays the anon key, the Authorization bearer becomes the tenant
    JWT — so Phase-3 RLS enforces isolation via `request.jwt.claims ->> 'tenant_id'`.

    Returns None outside Streamlit (scripts / cron) and when no tenant JWT is set (i.e.
    until SUPABASE_JWT_SECRET is configured and a tenant is selected) → callers fall back
    to the anon singleton, so today's behaviour is unchanged. PER-SESSION (cached in
    session_state) so one user's token can never bleed onto the shared singleton."""
    try:
        import streamlit as st  # type: ignore
        ss = st.session_state
        jwt = ss.get("_tenant_jwt")
    except Exception:
        return None
    if not jwt:
        return None
    if ss.get("_tenant_client") is not None and ss.get("_tenant_client_jwt") == jwt:
        return ss["_tenant_client"]
    url = _read_secret("SUPABASE_URL")
    key = _read_secret("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        client = create_client(url, key)
        client.postgrest.auth(jwt)        # bearer = tenant JWT (apikey stays the anon key)
    except Exception:
        return None
    ss["_tenant_client"] = client
    ss["_tenant_client_jwt"] = jwt
    return client


def service_client() -> Client:
    """The RLS-BYPASSING service-role client (SUPABASE_KEY is the service-role key),
    ALWAYS ignoring any per-session tenant JWT.

    Use for IDENTITY / BOOTSTRAP / tenant-ADMINISTRATION operations that must succeed
    regardless of the tenant tables' RLS state and regardless of whether the (deprecated,
    legacy-HS256) tenant JWT is accepted by PostgREST: resolving a user's memberships,
    listing the tenant directory for onboarding, creating/joining a tenant, and the
    super_user Tenants CRUD. These are pre-tenant or privileged operations — gating them
    on RLS is a bootstrap circularity (you can't ask RLS "which tenant am I in" before the
    context exists). Per-tenant DATA access stays on get_client() (the tenant-scoped
    client) so Phase-3 RLS isolation is preserved for the actual data tables.

    When multi-tenant is OFF (no JWT minted) this is identical to get_client()."""
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


# ---------------------------------------------------------------------------
# App-layer tenant read/write isolation (no Postgres RLS required)
# ---------------------------------------------------------------------------
# The per-tenant DATA tables (migration 067). Reads through get_client() for a
# non-super tenant user are auto-filtered to their tenant; inserts are auto-stamped
# with it. SHARED tables (extracted_solicitations, donor_intel, donor_sources, users,
# scan_logs, app_settings, tenants, tenant_memberships, …) are NOT scoped. Keep this in
# sync with migration 067's _SCOPED_TABLES.
_TENANT_SCOPED_TABLES = {
    "rfp_submissions", "meeting_logs", "meeting_schedule", "engagement_logs",
    "applied_funding", "narrative_logs", "scan_decisions", "donor_contacts",
    # The de-dup tombstone ledger is per-tenant too (migration 076) — otherwise the
    # per-tenant screening loop would let the first tenant's tombstones suppress the
    # same call for every later tenant.
    "rfp_seen",
    # Phase-B proposal queue (migration 080). A proposal belongs to the proposer's
    # tenant; the proposer path (create/list-mine/withdraw) is RLS-scoped here. Developer
    # review (list-all/approve/reject) deliberately uses service_client() (RLS-bypassing)
    # gated on permissions.is_developer_super — see core/suggestions.py. NOT in
    # _PUBLIC_VISIBLE_TABLES (a proposal is private to its tenant, never broadcast).
    "resource_suggestions",
}

# Of the scoped tables, these user-facing ACTIVITY tables also surface rows owned by
# PUBLIC ('individual', migration 078) tenants in everyone's read scope. Deliberately
# EXCLUDES rfp_seen (a public tenant's tombstones must NOT suppress others' screening)
# and scan_decisions (per-tenant ML training data). Writes are never broadened.
_PUBLIC_VISIBLE_TABLES = {
    "rfp_submissions", "meeting_logs", "meeting_schedule", "engagement_logs",
    "applied_funding", "narrative_logs", "donor_contacts",
}


def _stamp_tenant(rows, tid: str):
    """Add tenant_id to insert/upsert payload rows that don't already set it."""
    def _one(r):
        if isinstance(r, dict) and not r.get("tenant_id"):
            return {**r, "tenant_id": tid}
        return r
    return [_one(r) for r in rows] if isinstance(rows, list) else _one(rows)


class _ScopedTable:
    """Wraps a postgrest table builder so SELECT/UPDATE/DELETE auto-filter by tenant_id
    and INSERT/UPSERT auto-stamp it. Fail-open: any wrapping error falls back to the
    unscoped builder so a bug here can never break a query."""

    def __init__(self, builder, tid: str, name: str = ""):
        self._b = builder
        self._tid = tid
        self._name = name

    def select(self, *a, **k):
        try:
            q = self._b.select(*a, **k)
            # Activity tables also expose PUBLIC ('individual') tenants' rows to everyone
            # (migration 078). Broaden the read to my tenant + the public ones; keep the
            # cheap single-tenant `.eq` when there are none. Reads only — writes stay
            # stamped to my own tenant below.
            if self._name in _PUBLIC_VISIBLE_TABLES:
                try:
                    from auth.tenant_context import public_tenant_ids
                    pub = [t for t in public_tenant_ids() if t and t != self._tid]
                except Exception:
                    pub = []
                if pub:
                    return q.in_("tenant_id", [self._tid, *pub])
            return q.eq("tenant_id", self._tid)
        except Exception:
            return self._b.select(*a, **k)

    def insert(self, rows, *a, **k):
        try:
            rows = _stamp_tenant(rows, self._tid)
        except Exception:
            pass
        return self._b.insert(rows, *a, **k)

    def upsert(self, rows, *a, **k):
        try:
            rows = _stamp_tenant(rows, self._tid)
        except Exception:
            pass
        return self._b.upsert(rows, *a, **k)

    def update(self, values, *a, **k):
        try:
            return self._b.update(values, *a, **k).eq("tenant_id", self._tid)
        except Exception:
            return self._b.update(values, *a, **k)

    def delete(self, *a, **k):
        try:
            return self._b.delete(*a, **k).eq("tenant_id", self._tid)
        except Exception:
            return self._b.delete(*a, **k)

    def __getattr__(self, name):
        return getattr(self._b, name)


class _TenantScopedClient:
    """Thin proxy over a Supabase client that returns tenant-scoped table builders for
    the per-tenant data tables and delegates everything else unchanged."""

    def __init__(self, real, tid: str):
        self._real = real
        self._tid = tid

    def table(self, name):
        b = self._real.table(name)
        return _ScopedTable(b, self._tid, name) if name in _TENANT_SCOPED_TABLES else b

    def from_(self, name):                 # postgrest alias for .table()
        return self.table(name)

    def __getattr__(self, name):
        return getattr(self._real, name)


# A tenant_id that no row will ever carry — used to FAIL CLOSED. When we're inside a
# live tenant web session but can't resolve a real tenant_id, we scope reads to this
# sentinel (→ 0 rows) instead of handing back the unscoped RLS-bypassing service client
# (which would be a cross-tenant firehose). The all-zeros UUID is a valid uuid literal
# so `.eq("tenant_id", …)` type-checks server-side and simply matches nothing.
_NO_TENANT_SENTINEL = "00000000-0000-0000-0000-000000000000"


def _live_tenant_session() -> bool:
    """True inside a LOGGED-IN Streamlit web session under multi-tenant mode — the only
    context where an unresolved tenant must FAIL CLOSED rather than fall back to the
    unscoped service-role firehose. False for single-tenant deploys, for cron/scripts
    (no Streamlit session), and when a headless override is active (it always resolves to
    a concrete tid, handled in _tenant_scope_tid). Best-effort; any failure → False."""
    try:
        from auth.tenant_context import override_tenant_id
        if override_tenant_id():
            return False            # headless override → concrete tid, not the danger case
    except Exception:
        pass
    try:
        import streamlit as st  # type: ignore
        from auth.tenant_context import multitenant_enabled
        if not multitenant_enabled():
            return False            # single-tenant deploy → unscoped is correct
        return bool(st.session_state.get("app_user"))
    except Exception:
        return False


def _super_viewing_as() -> bool:
    """True when a super_user is VIEWING-AS another tenant — su_view_tenant set AND different
    from their own session tenant. ONLY that cross-tenant read needs the RLS-bypassing
    service client (the super's JWT is pinned to their home tenant, so it can't read another
    tenant). A super on their OWN tenant uses the RLS-backed JWT client, which works with any
    apikey — so a super's own data doesn't depend on SUPABASE_KEY being the service-role key.
    Best-effort; any failure → False (treated as own-tenant, the more available path)."""
    try:
        import streamlit as st  # type: ignore
        ss = st.session_state
        su = ss.get("su_view_tenant")
        return bool(su) and su != ss.get("tenant_id")
    except Exception:
        return False


def _is_super_session() -> bool:
    """True when the live session's user is a super_user. A super's tenant JWT is pinned
    to their HOME tenant, so it can't back a 'view-as another tenant' read — supers use
    the RLS-bypassing service client with the app-layer wrapper scoping to su_view/home
    instead (see get_client). Best-effort; any failure → False (treated as a normal user,
    the safer default)."""
    try:
        import streamlit as st  # type: ignore
        u = st.session_state.get("app_user") or {}
        return str(u.get("role") or "").lower() == "super_user"
    except Exception:
        return False


def _tenant_scope_tid() -> str | None:
    """The tenant_id to scope this session's data access to, or None for NO scoping —
    when multi-tenant is off, the user is a super_user (sees all tenants), there's no
    selected tenant, or we're outside a Streamlit session (cron / scripts). Best-effort;
    any failure → None (unscoped), so isolation never breaks data access."""
    # A headless tenant override (cron per-tenant screening) wins unconditionally: it is
    # the explicit 'operate as this tenant' signal, so reads/inserts scope + stamp to it.
    try:
        from auth.tenant_context import override_tenant_id
        ov = override_tenant_id()
        if ov:
            return ov
    except Exception:
        pass
    try:
        import streamlit as st  # type: ignore
        from auth.tenant_context import multitenant_enabled
        if not multitenant_enabled():
            return None
        user = st.session_state.get("app_user") or {}
        if str(user.get("role") or "").lower() == "super_user":
            # Super_user is scoped like everyone else — to the tenant they're VIEWING
            # (su_view_tenant, set from a ?tenant= link) or their own home tenant — so
            # their pipelines/report aren't a merged firehose of every tenant. Cross-tenant
            # AGGREGATES (Analytics dashboard, system-discovery counter) go through
            # service_client directly and bypass this scoping.
            return (st.session_state.get("su_view_tenant")
                    or st.session_state.get("tenant_id") or None)
        return st.session_state.get("tenant_id") or None
    except Exception:
        return None


def _scoped(base, tid: str) -> Client:
    """Wrap `base` in the tenant-scoping proxy; if wrapping itself fails, fall back to a
    FRESH service-role client scoped to the fail-closed sentinel so a wrapper bug can never
    downgrade to an unscoped firehose inside a tenant context."""
    try:
        return _TenantScopedClient(base, tid)           # type: ignore[return-value]
    except Exception:
        try:
            return _TenantScopedClient(service_client(), _NO_TENANT_SENTINEL)  # type: ignore[return-value]
        except Exception:
            return base


def get_client() -> Client:
    """The tenant-aware data client. FAILS CLOSED inside a live multi-tenant web session:
    it never returns the unscoped RLS-bypassing service-role client there, so an
    unresolved tenant reads NOTHING rather than every tenant's rows (the cross-tenant leak).

    Routing:
      * live NON-super session → the per-session tenant JWT client (Postgres RLS is a real
        DB backstop: a role=authenticated bearer overrides the service-role apikey and RLS
        filters by request.jwt.claims->>'tenant_id') PLUS the app-layer wrapper. If the JWT
        client is unavailable, still scope the service client to the resolved tid — or the
        sentinel — so the read stays closed.
      * live SUPER_USER session → on their OWN tenant, the RLS-backed JWT client (so a
        super's own data works with any apikey, not only the service-role key); on a
        cross-tenant VIEW-AS (su_view_tenant ≠ home), the RLS-bypassing service client
        (their JWT is pinned to home and can't read another tenant). Either way the wrapper
        scopes it (or sentinel-closes an unresolved tenant) — never the raw firehose.
      * NOT a live tenant session (single-tenant deploy, cron/scripts, headless override) →
        service client, scoped to a resolved/override tid when there is one, else unscoped
        (correct: there are no other tenants to leak to)."""
    tid = _tenant_scope_tid()
    live = _live_tenant_session()

    if live and not _is_super_session():
        sc = _session_tenant_client()     # RLS-backed JWT client when one applies
        base = sc if sc is not None else service_client()
        return _scoped(base, tid or _NO_TENANT_SENTINEL)

    if live:                              # live SUPER_USER
        # A super on their OWN tenant uses the RLS-backed JWT client (works with any apikey);
        # only a cross-tenant view-as needs the RLS-bypassing service client (the super's JWT
        # is pinned to home and can't read another tenant). Either way the wrapper scopes it,
        # and an unresolved tenant fails closed to the sentinel — never the firehose.
        if _super_viewing_as():
            base = service_client()
        else:
            sc = _session_tenant_client()
            base = sc if sc is not None else service_client()
        return _scoped(base, tid or _NO_TENANT_SENTINEL)

    # NOT a live tenant session (single-tenant deploy, cron/scripts, headless override).
    base = service_client()
    if tid:
        return _scoped(base, tid)
    return base                           # unscoped is correct: no other tenants to leak to


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
