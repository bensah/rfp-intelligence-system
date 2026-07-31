"""Multi-tenant session context (Phase 2 — Option A).

Keep streamlit-authenticator for login, but once a user is authenticated, resolve
WHICH tenant (a the organisation country / global team) they belong to and mint a short-lived
JWT that carries a `tenant_id` claim, signed with the project's Supabase JWT secret.
That JWT becomes the PostgREST bearer (see `db.supabase_client.get_client`), so the
Phase-3 RLS policies can enforce isolation via `request.jwt.claims ->> 'tenant_id'`.

DORMANT until `SUPABASE_JWT_SECRET` is configured. With no secret: `mint_tenant_jwt`
returns None, no tenant JWT is stored, `get_client()` keeps returning today's anon
singleton, and the app behaves exactly as before. Nothing here raises into a page —
every entry point is best-effort.
"""
from __future__ import annotations

import contextvars
import re
import time
from typing import Any, Optional

import streamlit as st

from db.supabase_client import _read_secret, service_client

# ---------------------------------------------------------------------------
# Headless tenant override (no Streamlit session — e.g. the cron screening loop)
# ---------------------------------------------------------------------------
# When set, this FORCES the active tenant for every tenant-aware layer
# (current_tenant_id, tenant_store, the get_client scoping wrapper, get_policies),
# so a background job can screen one tenant at a time without a browser session.
# A ContextVar (not a global) so it's isolated per async/thread context. Always
# set/reset it in a try/finally (see run_screening / screen_all_tenants).
_TENANT_OVERRIDE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "rfpis_tenant_override", default=None)


def set_tenant_override(tenant_id: Optional[str]):
    """Force the active tenant for a headless run. Returns a reset token."""
    return _TENANT_OVERRIDE.set(str(tenant_id) if tenant_id else None)


def reset_tenant_override(token) -> None:
    try:
        _TENANT_OVERRIDE.reset(token)
    except Exception:
        pass


def override_tenant_id() -> Optional[str]:
    """The forced tenant id, or None when no headless override is active."""
    try:
        return _TENANT_OVERRIDE.get()
    except Exception:
        return None

_JWT_TTL = 3600          # token lifetime (s); refreshed on demand near expiry
_JWT_SKEW = 120          # re-mint when fewer than this many seconds remain


def jwt_secret() -> Optional[str]:
    """The Supabase project JWT secret (Dashboard → Settings → JWT Keys → the legacy
    HS256 secret). Its presence is the on/off switch for the whole multi-tenant path."""
    return _read_secret("SUPABASE_JWT_SECRET")


def multitenant_enabled() -> bool:
    """Multi-tenant mode is ON only when the JWT secret is configured. Everything
    tenant-related (JWT minting, session client, Phase-4 onboarding) is dormant
    otherwise, so the single-tenant app behaves exactly as before."""
    return bool(jwt_secret())


def mint_tenant_jwt(user_id: str | None, tenant_id: str | None, *,
                    user_role: str = "collaborator", email: str | None = None,
                    ttl: int = _JWT_TTL) -> Optional[str]:
    """HS256 JWT (signed with the project JWT secret), role=authenticated so PostgREST
    accepts it as a normal authenticated user. Includes the `tenant_id` claim only when
    a tenant is given — a logged-in user with NO tenant yet still gets an authenticated
    token (tenant_id absent) so they can create/join a tenant during Phase-4 onboarding
    (writes to tenants/tenant_memberships are allowed to `authenticated`; scoped-table
    RLS still denies them, since the tenant_id claim is null). Returns None if the secret
    or user_id is missing, or PyJWT is unavailable."""
    secret = jwt_secret()
    if not (secret and user_id):
        return None
    try:
        import jwt as pyjwt
    except Exception:
        return None
    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "role": "authenticated",         # the Postgres role PostgREST switches to
        "aud": "authenticated",
        "email": email or "",
        "user_role": user_role,          # app role WITHIN the tenant (super_user/admin/…)
        "iat": now,
        "exp": now + max(300, int(ttl)),
    }
    if tenant_id:
        payload["tenant_id"] = str(tenant_id)   # the isolation claim (Phase-3 RLS reads this)
    try:
        tok = pyjwt.encode(payload, secret, algorithm="HS256")
        return tok.decode() if isinstance(tok, bytes) else tok   # PyJWT 1.x returned bytes
    except Exception:
        return None


def _resolve_user_id(user: dict) -> Optional[str]:
    """The user's uuid — from the app_user dict, else looked up by email."""
    uid = user.get("id")
    if uid:
        return str(uid)
    email = user.get("email")
    if not email:
        return None
    try:
        rows = (service_client().table("users").select("id")
                .eq("email", email).limit(1).execute().data or [])
        return str(rows[0]["id"]) if rows else None
    except Exception:
        return None


def active_memberships(user_id: str | None) -> list[dict[str, Any]]:
    """The user's ACTIVE memberships → [{tenant_id, name, slug, role, is_platform}]
    (best-effort).

    Runs on the RLS-BYPASSING service client: membership resolution is an identity
    operation that must see the true rows before any tenant context exists, and must not
    be filtered by the tenant tables' RLS state (see db.supabase_client.service_client).
    Falls back to a leaner select if the `is_platform` column isn't there yet (migration
    072), so it never hard-fails on ordering."""
    if not user_id:
        return []
    rows = None
    for sel in ("tenant_id, role, tenants(name, slug, is_platform, is_developer, status)",
                "tenant_id, role, tenants(name, slug, is_platform, status)",
                "tenant_id, role, tenants(name, slug, status)",
                "tenant_id, role, tenants(name, slug, is_platform)",
                "tenant_id, role, tenants(name, slug)"):
        try:
            rows = (service_client().table("tenant_memberships").select(sel)
                    .eq("user_id", user_id).eq("status", "active").execute().data or [])
            break
        except Exception:
            rows = None
    if rows is None:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        t = r.get("tenants") if isinstance(r.get("tenants"), dict) else {}
        # A BLACKLISTED tenant (migration 077) is a hard block — drop the membership
        # so its members lose all tenant context. (Suspend is intentionally NOT
        # enforced here; only the dedicated blacklist state blocks access.)
        if (t or {}).get("status") == "blacklisted":
            continue
        out.append({"tenant_id": r.get("tenant_id"),
                    "name": (t or {}).get("name"), "slug": (t or {}).get("slug"),
                    "role": r.get("role"), "is_platform": bool((t or {}).get("is_platform")),
                    "is_developer": bool((t or {}).get("is_developer"))})
    return out


_PUBLIC_TIDS_CACHE: dict[str, Any] = {"at": 0.0, "ids": []}
_PUBLIC_TIDS_TTL = 60.0


def public_tenant_ids() -> list[str]:
    """Tenant ids whose activity is PUBLIC — the 'individual' kind (migration 078). Their
    rows on the user-facing activity tables are merged into EVERY user's read scope by the
    scoping wrapper (db.supabase_client). Best-effort, 60s-cached on the RLS-bypassing
    service client; returns [] in single-tenant mode or before migration 078 (missing
    `kind` column → query errors → [])."""
    try:
        if not multitenant_enabled():
            return []
    except Exception:
        return []
    now = time.time()
    if (now - _PUBLIC_TIDS_CACHE["at"]) < _PUBLIC_TIDS_TTL:
        return _PUBLIC_TIDS_CACHE["ids"]
    try:
        rows = (service_client().table("tenants").select("id")
                .eq("kind", "individual").execute().data or [])
        ids = [str(r["id"]) for r in rows if r.get("id")]
        _PUBLIC_TIDS_CACHE["ids"] = ids            # only overwrite on success
    except Exception:
        ids = _PUBLIC_TIDS_CACHE["ids"]            # keep last-good on a transient error
    _PUBLIC_TIDS_CACHE["at"] = now
    return ids


_DEV_TIDS_CACHE: dict[str, Any] = {"at": 0.0, "ids": []}
_DEV_TIDS_TTL = 60.0


def developer_tenant_ids() -> list[str]:
    """Tenant ids flagged `is_developer=true` — the DEVELOPER / SYSTEM tenants whose
    members may perform cross-tenant DEVELOPER tasks (donor mapping, Sources catalog +
    Blocked tokens, Run Extraction, Records Verify/Reset, Learning data). Best-effort,
    60s-cached on the RLS-bypassing service
    client (migration 079).

    Graceful degradation: if the `is_developer` column doesn't exist yet (pre-079), we
    fall back to the `is_platform` tenant so the super_user's home still counts as a
    developer tenant and they aren't locked out on the flag day. Any hard error → keep
    last-good. Returns [] in single-tenant mode (the caller treats that as developer)."""
    try:
        if not multitenant_enabled():
            return []
    except Exception:
        return []
    now = time.time()
    if (now - _DEV_TIDS_CACHE["at"]) < _DEV_TIDS_TTL:
        return _DEV_TIDS_CACHE["ids"]
    ids: list[str] | None = None
    try:
        rows = (service_client().table("tenants").select("id")
                .eq("is_developer", True).execute().data or [])
        ids = [str(r["id"]) for r in rows if r.get("id")]
    except Exception:
        # Column not present yet (pre-079) → fall back to the platform/home tenant.
        try:
            rows = (service_client().table("tenants").select("id")
                    .eq("is_platform", True).execute().data or [])
            ids = [str(r["id"]) for r in rows if r.get("id")]
        except Exception:
            ids = None
    if ids is not None:
        _DEV_TIDS_CACHE["ids"] = ids               # only overwrite on success
    _DEV_TIDS_CACHE["at"] = now
    return _DEV_TIDS_CACHE["ids"]


def active_tenant_is_developer() -> bool:
    """True when THIS session's home tenant is a developer/system tenant — the gate for
    cross-tenant DEVELOPER tasks (combined with a role check in core.permissions).

    Keys off `current_tenant_id()`, which is the user's OWN home tenant: a super_user's
    'view-as' another tenant sets a SEPARATE `su_view_tenant` (see core.app_header) and
    does NOT change current_tenant_id(), so their developer powers over shared resources
    persist while they inspect a client tenant — and a client-tenant admin never gains
    them. SINGLE-TENANT (multi-tenant OFF) → True: the sole deployment is its own
    developer, so these gates collapse to the plain role check and nothing is locked out.
    Best-effort: never raises."""
    try:
        if not multitenant_enabled():
            return True
    except Exception:
        return True
    try:
        tid = current_tenant_id()
        if not tid:
            return False
        return str(tid) in set(developer_tenant_ids())
    except Exception:
        return False


def _slug_base(name: str | None) -> str:
    """A URL-safe base slug from a tenant name ('Taadom Digital PLC' → 'taadom-digital-plc')."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "tenant"


def make_tenant_slug(name: str | None) -> str:
    """A URL-safe, UNIQUE slug for a NEW tenant, deduped against existing slugs. Every tenant
    should have one so the super_user view-as URL is a readable, stable `?tenant=<slug>` (not
    a raw UUID). Best-effort on the RLS-bypassing service client; the base slug on any error."""
    base = _slug_base(name)
    try:
        rows = (service_client().table("tenants").select("slug").execute().data or [])
        existing = {(r.get("slug") or "").strip().lower() for r in rows if r.get("slug")}
    except Exception:
        return base
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def resolve_tenant_by_key(key: str | None) -> dict | None:
    """Resolve a tenant by slug OR id → {id, name, slug}, or None. Used by the super_user
    'view-as' entry (a ?tenant=<slug|id> link). RLS-bypassing service client (privileged
    platform lookup)."""
    if not key:
        return None
    for col in ("slug", "id"):
        try:
            rows = (service_client().table("tenants").select("id,name,slug")
                    .eq(col, key).limit(1).execute().data or [])
            if rows:
                return rows[0]
        except Exception:
            continue
    return None


def tenant_store(tenant_id: str | None = None) -> tuple[Any, str] | None:
    """`(service_client, tenant_id)` for reading/writing a tenant's own records
    (org identity + org profile), or None when multi-tenant is off or no tenant resolves.

    `tenant_id` overrides the session's current tenant — this is how the super_user views
    or edits ANOTHER tenant's Organization page. Uses the RLS-bypassing service client:
    the tenant id is always server-derived (the session's own tenant, or a super_user's
    explicit pick), never user-supplied, so bypassing RLS here is safe and removes the
    dependency on the tenant tables' RLS policies for the Organization page to work.

    A headless override (cron per-tenant screening) resolves even when the JWT master
    switch isn't configured in the cron env — the override is itself the explicit
    'operate as this tenant' signal."""
    try:
        ov = override_tenant_id()
        tid = tenant_id or ov or current_tenant_id()
        if not tid:
            return None
        if ov is None and not multitenant_enabled():
            return None             # session path still requires the master switch
        return service_client(), str(tid)
    except Exception:
        return None


def _default_membership(user: dict, mems: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the membership to auto-select for THIS session, or None to leave the user
    tenant-less (onboarding / future picker):
      * exactly one active membership → that one;
      * super_user with several → their platform HOME tenant (flagged `is_platform`, else
        slug 'rfpis', else a name starting with "RFPIS"), else the first (name-ordered)
        so they always land somewhere rather than tenant-less;
      * anyone else with several → None (a multi-tenant switcher is future work)."""
    if not mems:
        return None
    if len(mems) == 1:
        return mems[0]
    if (user.get("role") or "").lower() == "super_user":
        for pred in (lambda m: m.get("is_platform"),
                     lambda m: (m.get("slug") or "").strip().lower() == "rfpis",
                     lambda m: (m.get("name") or "").strip().lower().startswith("rfpis")):
            home = next((m for m in mems if pred(m)), None)
            if home is not None:
                return home
        return sorted(mems, key=lambda m: (m.get("name") or "").lower())[0]
    return None


def set_active_tenant(user: dict, tenant_id: str | None, *, role: str | None = None,
                      name: str | None = None) -> bool:
    """Set THIS session's identity: mint the JWT and stash tenant id/name in
    session_state. `tenant_id=None` mints a tenant-LESS authenticated token (for a
    logged-in user still in onboarding — lets them create/join a tenant under RLS).
    Clears any cached per-session client so it rebuilds with the new token. Returns
    True if a JWT was minted (secret configured), else False."""
    uid = _resolve_user_id(user)
    tok = mint_tenant_jwt(uid, tenant_id,
                          user_role=role or user.get("role") or "collaborator",
                          email=user.get("email"))
    st.session_state["tenant_id"] = tenant_id
    st.session_state["tenant_name"] = name
    st.session_state["_tenant_jwt"] = tok
    st.session_state["_tenant_jwt_exp"] = (int(time.time()) + _JWT_TTL) if tok else 0
    st.session_state.pop("_tenant_client", None)          # force rebuild in get_client()
    st.session_state.pop("_tenant_client_jwt", None)
    return tok is not None


def current_tenant_id() -> Optional[str]:
    # A headless override (cron per-tenant screening) wins; else the browser session.
    ov = override_tenant_id()
    if ov:
        return ov
    try:
        return st.session_state.get("tenant_id")
    except Exception:
        return None                 # outside a Streamlit session (scripts / cron)


def current_tenant_name() -> Optional[str]:
    return st.session_state.get("tenant_name")


def ensure_tenant_context(user: dict) -> None:
    """Called once per page after auth (from ensure_logged_in). DORMANT when
    SUPABASE_JWT_SECRET is unset. When set:
      (a) a valid, non-expired tenant JWT already this session → refresh only near expiry;
      (b) else resolve the user's ACTIVE memberships — exactly one → auto-select; more
          than one → leave for the Phase-4 picker; none → no context yet (Phase-4
          onboarding creates/joins a tenant).
    Never raises into the page."""
    try:
        if not jwt_secret():
            return                                        # dormant until configured
        exp = st.session_state.get("_tenant_jwt_exp", 0)
        if st.session_state.get("_tenant_jwt") and (exp - int(time.time())) > _JWT_SKEW:
            return                                        # still-fresh token — nothing to do
        tid = st.session_state.get("tenant_id")
        if tid:                                           # known tenant → near-expiry refresh
            set_active_tenant(user, tid, name=st.session_state.get("tenant_name"))
            return
        uid = _resolve_user_id(user)
        mems = active_memberships(uid) if uid else []
        chosen = _default_membership(user, mems)
        if chosen is not None:
            set_active_tenant(user, chosen["tenant_id"],
                              role=chosen.get("role"), name=chosen.get("name"))
        else:
            # No resolvable default (0 memberships, or >1 for a non-super user) → mint a
            # tenant-LESS authenticated token so the user still has an identity for
            # Phase-4 onboarding (create/join a tenant). The Phase-4 gate routes 0-tenant
            # users; a proper multi-tenant switcher for >1 is future work.
            set_active_tenant(user, None)
    except Exception:
        return
