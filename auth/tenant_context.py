"""Multi-tenant session context (Phase 2 — Option A).

Keep streamlit-authenticator for login, but once a user is authenticated, resolve
WHICH tenant (a CHAI country / global team) they belong to and mint a short-lived
JWT that carries a `tenant_id` claim, signed with the project's Supabase JWT secret.
That JWT becomes the PostgREST bearer (see `db.supabase_client.get_client`), so the
Phase-3 RLS policies can enforce isolation via `request.jwt.claims ->> 'tenant_id'`.

DORMANT until `SUPABASE_JWT_SECRET` is configured. With no secret: `mint_tenant_jwt`
returns None, no tenant JWT is stored, `get_client()` keeps returning today's anon
singleton, and the app behaves exactly as before. Nothing here raises into a page —
every entry point is best-effort.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import streamlit as st

from db.supabase_client import _read_secret, get_client

_JWT_TTL = 3600          # token lifetime (s); refreshed on demand near expiry
_JWT_SKEW = 120          # re-mint when fewer than this many seconds remain


def jwt_secret() -> Optional[str]:
    """The Supabase project JWT secret (Dashboard → Settings → API → JWT Secret).
    Its presence is the on/off switch for the whole multi-tenant path."""
    return _read_secret("SUPABASE_JWT_SECRET")


def mint_tenant_jwt(user_id: str | None, tenant_id: str | None, *,
                    user_role: str = "collaborator", email: str | None = None,
                    ttl: int = _JWT_TTL) -> Optional[str]:
    """HS256 JWT (signed with the project JWT secret) carrying the tenant_id claim +
    role=authenticated so PostgREST accepts it as a normal authenticated user. Returns
    None if the secret or ids are missing, or PyJWT is unavailable."""
    secret = jwt_secret()
    if not (secret and user_id and tenant_id):
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
        "tenant_id": str(tenant_id),     # <- the isolation claim (Phase-3 RLS reads this)
        "user_role": user_role,          # app role WITHIN the tenant (super_user/admin/…)
        "iat": now,
        "exp": now + max(300, int(ttl)),
    }
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
        rows = (get_client().table("users").select("id")
                .eq("email", email).limit(1).execute().data or [])
        return str(rows[0]["id"]) if rows else None
    except Exception:
        return None


def active_memberships(user_id: str | None) -> list[dict[str, Any]]:
    """The user's ACTIVE tenant memberships → [{tenant_id, name, role}] (best-effort)."""
    if not user_id:
        return []
    try:
        rows = (get_client().table("tenant_memberships")
                .select("tenant_id, role, tenants(name)")
                .eq("user_id", user_id).eq("status", "active").execute().data or [])
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        t = r.get("tenants") if isinstance(r.get("tenants"), dict) else {}
        out.append({"tenant_id": r.get("tenant_id"),
                    "name": (t or {}).get("name"), "role": r.get("role")})
    return out


def set_active_tenant(user: dict, tenant_id: str, *, role: str | None = None,
                      name: str | None = None) -> bool:
    """Select a tenant for THIS session: mint the JWT and stash tenant id/name in
    session_state. Clears any cached per-session client so it rebuilds with the new
    token. Returns True if a JWT was minted (secret configured), else False."""
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
    return st.session_state.get("tenant_id")


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
        if len(mems) == 1:
            set_active_tenant(user, mems[0]["tenant_id"],
                              role=mems[0].get("role"), name=mems[0].get("name"))
        # 0 or >1 active memberships → handled by Phase-4 onboarding / tenant picker.
    except Exception:
        return
