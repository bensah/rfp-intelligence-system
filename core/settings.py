"""Tiny key/value settings layer backed by the `app_settings` table.

Used so admins can change app-wide defaults (currently just `year`) without
editing code. Cached briefly per process so we don't query Supabase on every
page render.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Optional

from db.supabase_client import get_client

_CACHE: dict[str, tuple[float, str]] = {}
_TTL = 60.0  # seconds


def _now() -> float:
    return time.time()


# Config blobs that are PER-TENANT (multi-tenant): the check-in schedule, the eligibility
# policies, and the team roster. When a tenant context resolves, these read/write the
# per-tenant tenant_settings store (migration 075) with NO fallback to the global
# app_settings value — so a fresh tenant gets the caller's CODE default (permissive
# policies, empty schedule/team), not the organisation's config. Everything else stays global/shared.
_TENANT_SCOPED_KEYS = {"schedule_json", "scan_policies", "team_members_json"}


def _scoped_store(key: str):
    """(service_client, tenant_id) when `key` is a per-tenant config key AND a tenant
    resolves this session; else None (→ global app_settings). Reuses _tenant_ctx (the
    session tenant, so a super_user sees their own RFPIS home config)."""
    if key not in _TENANT_SCOPED_KEYS:
        return None
    return _tenant_ctx()


def _is_missing_tenant_settings(exc: Exception) -> bool:
    """True when `exc` means the tenant_settings table itself is absent (migration 075
    not applied yet) — the ONLY case where a scoped read/write may fall back to the global
    store. A transient network error must NOT be mistaken for this (it would misroute a
    tenant write to the shared store)."""
    m = str(exc).lower()
    return "tenant_settings" in m and (
        "does not exist" in m or "could not find" in m
        or "42p01" in m or "pgrst205" in m or "schema cache" in m)


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    store = _scoped_store(key)
    if store is not None:
        client, tid = store
        ck = f"{tid}::{key}"
        cached = _CACHE.get(ck)
        if cached and _now() - cached[0] < _TTL:
            return cached[1] if cached[1] is not None else default
        try:
            rows = (client.table("tenant_settings").select("value")
                    .eq("tenant_id", tid).eq("key", key).limit(1).execute().data or [])
            val = rows[0]["value"] if rows else None
            _CACHE[ck] = (_now(), val)          # cache "no override" too → code default
            return val if val is not None else default
        except Exception as exc:
            if not _is_missing_tenant_settings(exc):
                # Transient/other error → return the CODE default, never serve the global
                # (the organisation) value as this tenant's config. (Only a missing table falls back.)
                return default
            # tenant_settings table missing (pre-075) → fall back to the global store below
    cached = _CACHE.get(key)
    if cached and _now() - cached[0] < _TTL:
        return cached[1]
    try:
        sb = get_client()
        res = sb.table("app_settings").select("value").eq("key", key).limit(1).execute()
        rows = res.data or []
        val = rows[0]["value"] if rows else None
    except Exception:
        val = None
    if val is None:
        return default
    _CACHE[key] = (_now(), val)
    return val


def set_setting(key: str, value: str, updated_by: Optional[str] = None) -> None:
    store = _scoped_store(key)
    if store is not None:
        client, tid = store
        try:
            client.table("tenant_settings").upsert(
                {"tenant_id": tid, "key": key, "value": value, "updated_by": updated_by},
                on_conflict="tenant_id,key",
            ).execute()
            _CACHE[f"{tid}::{key}"] = (_now(), value)
            return
        except Exception as exc:
            if not _is_missing_tenant_settings(exc):
                # Transient/other write error → SURFACE it. Do NOT silently redirect a
                # per-tenant write to the global store (it would read back as the default
                # and mutate the shared blob). Only a missing table falls through.
                raise
            # tenant_settings table missing (pre-075) → write the global store below
    sb = get_client()
    sb.table("app_settings").upsert(
        {"key": key, "value": value, "updated_by": updated_by},
        on_conflict="key",
    ).execute()
    _CACHE[key] = (_now(), value)


def clear_cache() -> None:
    _CACHE.clear()
    _ORG_CACHE.clear()


def get_year() -> int:
    """Active review year — auto-rolls over with the calendar.

    Resolution order:
      1. If `app_settings.year` is set AND points to the current calendar
         year OR a future year, honour it. (Future-year override is
         useful for early planning — e.g. set 2027 in late 2026 to begin
         populating next-year week dropdowns.)
      2. Otherwise return the current calendar year.

    Past-year overrides are IGNORED automatically. This prevents the
    weekly-dropdown / review-week selectors from going stale once the
    calendar advances past the configured year — the historical bug
    where a 2026-locked deployment kept rendering 2026 weeks well into
    2027 is impossible by construction now.

    Historical viewing (look at 2024 data) is handled per-view via an
    explicit year picker — never via this global default.
    """
    current = date.today().year
    raw = get_setting("year")
    try:
        stored = int(raw) if raw else None
    except (TypeError, ValueError):
        stored = None
    if stored is None or stored < current:
        return current
    return stored


# -----------------------------------------------------------------------------
# Deploying-organization profile
# -----------------------------------------------------------------------------
# RFPIS is designed to be deployed by any non-profit / research org running
# an RFP discovery + screening pipeline. The deploying org's profile lives
# in app_settings (single-org model — when we go multi-tenant later, this
# becomes an organizations table with a foreign key). Defaults are intentional
# placeholders — each deployment should set its real values via the Admin
# UI on first install. Nothing here exposes who's actually running the app.
# -----------------------------------------------------------------------------
_ORG_DEFAULTS = {
    "org_name":            "Your Organization",
    "org_short":           "Org",
    "org_country":         "",
    "org_team":            "Business Development Team",
    # Geography hard-gate (scan): is the deploying org itself a US entity?
    # When "false" (default — e.g. a non-US country office), US-domestic-only RFPs are
    # rejected at scan time. Set "true" for a US-based deployment.
    "org_is_us_entity":    "false",
    # Donor-intel hard-gate: does the deploying org have a locally-constituted
    # Board of Directors? "no" makes donors that REQUIRE a local board a hard
    # MUST-4 disqualifier. Blank (default) = unknown — don't apply the gate.
    "org_has_local_board": "",
    "org_contact_email":   "",
    "org_logo_url":        "",  # legacy — uploaded logo lives in org_logo_b64
    "org_website":         "",
    # Bid-fitness inputs (drive PREFER 9 Bid effort + PREFER 8 Competitiveness):
    "org_has_bd_team":     "false",   # Business-Development / fundraising team?
    "org_is_grassroot":    "false",   # grassroots/local NGO (else international)?
    "org_is_multi_country": "false",  # operate across many countries?
    "org_hq_country":      "",        # HQ country (matched vs donor HQ)
}


# -----------------------------------------------------------------------------
# Multi-tenant identity — PER-TENANT org identity lives in `tenants.org_identity`
# (a jsonb blob, distinct from `tenants.org_profile` so saving one never clobbers
# the other). When multi-tenant is ON and a tenant resolves, get_org/set_org read
# and write that blob; the tenant's DISPLAY name falls back to `tenants.name`.
# When multi-tenant is OFF (or no tenant / the column isn't there yet), everything
# falls back to the legacy per-key app_settings store — behaviour is unchanged.
# A short per-tenant cache keeps the header (which calls get_org every render)
# from re-querying; writes clear it.
# -----------------------------------------------------------------------------
_ORG_CACHE: dict[Optional[str], tuple[float, dict[str, str]]] = {}
_ORG_TTL = 30.0


def _tenant_ctx(tenant_id: Optional[str] = None):
    """(service_client, tenant_id) for per-tenant identity, or None → legacy store.
    Lazy import so core.settings has no import-time dependency on the auth layer."""
    try:
        from auth import tenant_context as tc
        return tc.tenant_store(tenant_id)
    except Exception:
        return None


def _clear_org_cache() -> None:
    _ORG_CACHE.clear()


def get_org(tenant_id: Optional[str] = None) -> dict[str, str]:
    """Full deploying-org identity as a dict. PER-TENANT when multi-tenant is on
    (reads tenants.org_identity; display name = the tenant's own name); the global
    app_settings keys otherwise. `tenant_id` overrides the session tenant (super_user
    viewing another tenant). Missing keys fall back to placeholder defaults so the UI
    never renders an empty header."""
    ctx = _tenant_ctx(tenant_id)
    if ctx is not None:
        client, tid = ctx
        cached = _ORG_CACHE.get(tid)
        if cached and _now() - cached[0] < _ORG_TTL:
            return dict(cached[1])
        rows = None
        try:
            rows = (client.table("tenants").select("name, org_identity")
                    .eq("id", tid).limit(1).execute().data or [])
        except Exception:
            rows = None            # org_identity column missing / error → legacy below
        if rows:
            ident = rows[0].get("org_identity")
            ident = ident if isinstance(ident, dict) else {}
            out: dict[str, str] = {}
            for key, default in _ORG_DEFAULTS.items():
                v = ident.get(key)
                out[key] = v if v not in (None, "") else default
            # Display name = the tenant's canonical `tenants.name` (so the Organization
            # editor's "Organization name" field IS the tenant rename; see set_org).
            out["org_name"] = (rows[0].get("name") or ident.get("org_name")
                               or _ORG_DEFAULTS["org_name"])
            _ORG_CACHE[tid] = (_now(), dict(out))
            return out
        # rows is None (error) or [] (tenant not found) → fall through to legacy
    out = {}
    for key, default in _ORG_DEFAULTS.items():
        out[key] = get_setting(key, default) or default
    return out


def get_org_name(tenant_id: Optional[str] = None) -> str:
    """Convenience accessor — most pages just want the display name."""
    return get_org(tenant_id).get("org_name") or _ORG_DEFAULTS["org_name"]


def get_org_short(tenant_id: Optional[str] = None) -> str:
    """Short name for page titles / breadcrumbs (e.g. 'Acme BD')."""
    return get_org(tenant_id).get("org_short") or _ORG_DEFAULTS["org_short"]


def set_org(fields: dict[str, str], updated_by: Optional[str] = None,
            tenant_id: Optional[str] = None) -> None:
    """Persist one or more org-identity fields (partial upsert). PER-TENANT into
    tenants.org_identity when multi-tenant is on (super_user can target another tenant via
    `tenant_id`); the global app_settings keys otherwise. Unknown keys are ignored."""
    ctx = _tenant_ctx(tenant_id)
    if ctx is not None:
        client, tid = ctx
        column_ok = True
        rows: list = []
        try:
            rows = (client.table("tenants").select("org_identity")
                    .eq("id", tid).limit(1).execute().data or [])
        except Exception:
            column_ok = False      # org_identity column missing / DB down → legacy below
        if column_ok:
            ident = rows[0].get("org_identity") if rows else None
            ident = dict(ident) if isinstance(ident, dict) else {}
            new_name = None
            for key, value in fields.items():
                if key in _ORG_DEFAULTS:
                    ident[key] = value or ""
                    if key == "org_name":
                        new_name = (value or "").strip()
            # The write MUST land on the tenant record. Do NOT swallow a failure into the
            # legacy global store (that silently writes the wrong place and looks like
            # "it didn't persist") — let the caller see the real error (RLS / permission /
            # a service-role-vs-anon key issue).
            res = client.table("tenants").update({"org_identity": ident}).eq("id", tid).execute()
            if not (getattr(res, "data", None)):
                raise RuntimeError(
                    f"tenants.org_identity update for {tid} affected 0 rows — the write "
                    "did not persist (check RLS / that SUPABASE_KEY is the service-role key).")
            # "Organization name" IS the tenant rename: sync tenants.name too (best-effort;
            # a UNIQUE-name collision just leaves the canonical name unchanged).
            if new_name:
                try:
                    client.table("tenants").update({"name": new_name}).eq("id", tid).execute()
                except Exception:
                    pass
            _clear_org_cache()
            return
    for key, value in fields.items():
        if key in _ORG_DEFAULTS:
            set_setting(key, value or "", updated_by=updated_by)


# -----------------------------------------------------------------------------
# Org logo upload — bytes stored as base64 in app_settings (no filesystem
# dependency, survives Streamlit Cloud container restarts where local-file
# uploads would be wiped). For a typical 50-200 KB logo the row payload is
# fine; if the deploying org ever needs a 5MB hero-image-as-logo we'd move
# to Supabase Storage. Two keys are kept:
#   * org_logo_b64   — the file bytes, base64-encoded ASCII
#   * org_logo_mime  — the original mime type ("image/png", "image/jpeg", ...)
# The legacy `org_logo_url` field is still respected as a fallback so any
# install that already pasted a hosted URL keeps working without re-upload.
# -----------------------------------------------------------------------------
def _read_tenant_identity(client, tid) -> Optional[dict]:
    """The tenant's org_identity blob, or None on any error (missing column, etc.)."""
    try:
        rows = (client.table("tenants").select("org_identity")
                .eq("id", tid).limit(1).execute().data or [])
    except Exception:
        return None
    if not rows:
        return None
    v = rows[0].get("org_identity")
    return v if isinstance(v, dict) else {}


def _write_tenant_identity(client, tid, ident: dict) -> bool:
    try:
        client.table("tenants").update({"org_identity": ident}).eq("id", tid).execute()
        _clear_org_cache()
        return True
    except Exception:
        return False


def get_org_logo(tenant_id: Optional[str] = None) -> tuple[bytes | None, str | None]:
    """Return (image_bytes, mime_type) of the uploaded logo, or (None, None) if none.
    PER-TENANT (tenants.org_identity) when multi-tenant is on; the global app_settings
    keys otherwise. `tenant_id` overrides the session tenant."""
    import base64
    ctx = _tenant_ctx(tenant_id)
    if ctx is not None:
        client, tid = ctx
        ident = _read_tenant_identity(client, tid)
        if ident is not None:
            b64 = ident.get("org_logo_b64") or ""
            mime = ident.get("org_logo_mime") or "image/png"
            if not b64:
                return None, None
            try:
                return base64.b64decode(b64), (mime or "image/png")
            except (ValueError, TypeError):
                return None, None
        # ident is None (error / missing column) → fall through to legacy
    b64 = get_setting("org_logo_b64", "")
    mime = get_setting("org_logo_mime", "image/png")
    if not b64:
        return None, None
    try:
        return base64.b64decode(b64), (mime or "image/png")
    except (ValueError, TypeError):
        return None, None


def set_org_logo(file_bytes: bytes, mime: str, updated_by: Optional[str] = None,
                 tenant_id: Optional[str] = None) -> None:
    """Persist an uploaded logo — PER-TENANT (tenants.org_identity) when multi-tenant is
    on, else the global app_settings keys."""
    import base64
    if not file_bytes:
        return
    b64 = base64.b64encode(file_bytes).decode("ascii")
    ctx = _tenant_ctx(tenant_id)
    if ctx is not None:
        client, tid = ctx
        ident = _read_tenant_identity(client, tid)
        if ident is not None:
            ident = dict(ident)
            ident["org_logo_b64"] = b64
            ident["org_logo_mime"] = mime or "image/png"
            if _write_tenant_identity(client, tid, ident):
                return
        # fall through to legacy on error
    set_setting("org_logo_b64", b64, updated_by=updated_by)
    set_setting("org_logo_mime", mime or "image/png", updated_by=updated_by)


def clear_org_logo(updated_by: Optional[str] = None,
                   tenant_id: Optional[str] = None) -> None:
    """Remove the uploaded logo. Leaves the legacy `org_logo_url` alone."""
    ctx = _tenant_ctx(tenant_id)
    if ctx is not None:
        client, tid = ctx
        ident = _read_tenant_identity(client, tid)
        if ident is not None:
            ident = dict(ident)
            ident["org_logo_b64"] = ""
            ident["org_logo_mime"] = ""
            if _write_tenant_identity(client, tid, ident):
                return
        # fall through to legacy on error
    set_setting("org_logo_b64", "", updated_by=updated_by)
    set_setting("org_logo_mime", "", updated_by=updated_by)


# -----------------------------------------------------------------------------
# Currency overrides — full list stored as JSON in app_settings.currencies_json
# Each entry: {code, label, symbol, aliases: [..], usd_rate}
# When unset, dropdowns.load() falls back to config/dropdowns.yaml.
# -----------------------------------------------------------------------------
def get_currency_overrides() -> Optional[list]:
    import json
    raw = get_setting("currencies_json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except (ValueError, TypeError):
        pass
    return None


def set_currency_overrides(currencies: list, updated_by: Optional[str] = None) -> None:
    import json
    set_setting("currencies_json", json.dumps(currencies), updated_by=updated_by)


# -----------------------------------------------------------------------------
# Team members — real names live in app_settings (NOT in the public repo's
# config/dropdowns.yaml, which only carries 'Team Member 1..N' placeholders).
# Stored as a JSON list under `team_members_json`. dropdowns.load() folds this
# in (+ an automatic 'Other') so the Submit + Edit forms show the real roster.
# -----------------------------------------------------------------------------
def get_team_members() -> Optional[list]:
    import json
    raw = get_setting("team_members_json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            members = [str(x).strip() for x in data if str(x).strip()]
            return members or None
    except (ValueError, TypeError):
        pass
    return None


def set_team_members(members: list, updated_by: Optional[str] = None) -> None:
    """Persist the team roster. De-duplicates (case-insensitive), preserves
    order, drops blanks and any stray 'Other'/'All' tokens (added by the forms)."""
    import json
    seen: set[str] = set()
    cleaned: list[str] = []
    for m in members:
        name = str(m).strip()
        low = name.lower()
        if not name or low in ("other", "all") or low in seen:
            continue
        seen.add(low)
        cleaned.append(name)
    set_setting("team_members_json", json.dumps(cleaned), updated_by=updated_by)
