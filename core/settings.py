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


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
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
    sb = get_client()
    sb.table("app_settings").upsert(
        {"key": key, "value": value, "updated_by": updated_by},
        on_conflict="key",
    ).execute()
    _CACHE[key] = (_now(), value)


def clear_cache() -> None:
    _CACHE.clear()


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
}


def get_org() -> dict[str, str]:
    """Full deploying-org profile as a dict. Missing keys fall back to
    placeholder defaults so the UI never renders an empty header."""
    out: dict[str, str] = {}
    for key, default in _ORG_DEFAULTS.items():
        out[key] = get_setting(key, default) or default
    return out


def get_org_name() -> str:
    """Convenience accessor — most pages just want the display name."""
    return get_setting("org_name", _ORG_DEFAULTS["org_name"]) or _ORG_DEFAULTS["org_name"]


def get_org_short() -> str:
    """Short name for page titles / breadcrumbs (e.g. 'Acme BD')."""
    return get_setting("org_short", _ORG_DEFAULTS["org_short"]) or _ORG_DEFAULTS["org_short"]


def set_org(fields: dict[str, str], updated_by: Optional[str] = None) -> None:
    """Persist one or more org-profile fields. Unknown keys are ignored
    silently to keep the upsert resilient to schema drift."""
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
def get_org_logo() -> tuple[bytes | None, str | None]:
    """Return (image_bytes, mime_type) of the uploaded logo, or (None, None)
    if nothing was uploaded. Decodes the base64 stored in app_settings."""
    import base64
    b64 = get_setting("org_logo_b64", "")
    mime = get_setting("org_logo_mime", "image/png")
    if not b64:
        return None, None
    try:
        return base64.b64decode(b64), (mime or "image/png")
    except (ValueError, TypeError):
        return None, None


def set_org_logo(file_bytes: bytes, mime: str,
                 updated_by: Optional[str] = None) -> None:
    """Persist an uploaded logo. Stores the base64 + mime in app_settings."""
    import base64
    if not file_bytes:
        return
    b64 = base64.b64encode(file_bytes).decode("ascii")
    set_setting("org_logo_b64", b64, updated_by=updated_by)
    set_setting("org_logo_mime", mime or "image/png", updated_by=updated_by)


def clear_org_logo(updated_by: Optional[str] = None) -> None:
    """Remove the uploaded logo. Leaves the legacy `org_logo_url` alone."""
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
