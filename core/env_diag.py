"""Deployment environment diagnostics — "why does the published app behave differently?"

The code in this repo is one thing; the environment it runs in is another. A Streamlit
Cloud deploy and a laptop run the SAME `main` against a DIFFERENT set of inputs:

  * which Supabase project the secrets point at (URL) — a different project is a
    different database, with different tenants, memberships and roster edits;
  * which KIND of key `SUPABASE_KEY` is — a service-role key bypasses RLS, a
    publishable/anon key does not, so identity lookups that work locally return nothing;
  * whether `SUPABASE_JWT_SECRET` is READABLE — it is the multi-tenant master switch
    (`tenant_context.multitenant_enabled`); unreadable means every user silently shares
    one unscoped pool and the header falls back to the pre-multi-tenant org identity in
    `app_settings`;
  * which commit the host actually serves.

Every one of those is invisible from the outside: the app looks identical and simply
resolves a different tenant, or none. This module makes them visible FROM INSIDE the
deployment, so a prod-vs-local difference is read, not guessed.

SAFETY: no secret VALUE is ever returned or rendered. Secrets are reported as
present/absent, where they were found, their length, and a short sha256 fingerprint —
enough to compare two environments ("same fingerprint = same value") without disclosing
anything. Every field is independently best-effort: a diagnostics page must never be the
thing that breaks.
"""
from __future__ import annotations

import hashlib
import os
import platform
import sys
from typing import Any, Optional

_SECRET_NAMES = ("SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_JWT_SECRET")


def _fingerprint(value: str | None) -> Optional[str]:
    """First 8 hex of sha256 — comparable across environments, not reversible."""
    if not value:
        return None
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    except Exception:
        return None


def key_class(key: str | None) -> tuple[str, Optional[bool]]:
    """`(label, is_service_role)` for a Supabase API key, WITHOUT revealing it.

    Both key formats matter here. New-style keys are self-describing by prefix
    (`sb_secret_` vs `sb_publishable_`); legacy keys are JWTs whose `role` claim says
    which they are — read WITHOUT verification (we only want the claim, not trust).
    `is_service_role` is None when the format isn't recognised."""
    if not key:
        return "missing", None
    if key.startswith("sb_secret_"):
        return "service-role (sb_secret_...)", True
    if key.startswith("sb_publishable_"):
        return "publishable/anon (sb_publishable_...) - NOT service-role", False
    if key.startswith("eyJ"):
        role = None
        try:
            import base64
            import json
            payload = key.split(".")[1]
            payload += "=" * (-len(payload) % 4)          # restore stripped padding
            role = json.loads(base64.urlsafe_b64decode(payload)).get("role")
        except Exception:
            role = None
        if role:
            return f"legacy JWT key (role={role})", role == "service_role"
        return "legacy JWT key (role unreadable)", None
    return "unrecognised format", None


def project_ref(url: str | None) -> Optional[str]:
    """The Supabase project ref from its URL ('https://abc123.supabase.co' -> 'abc123').
    Not a secret (it rides on every request) and it is THE field that tells two
    deployments apart when they disagree about the data."""
    if not url:
        return None
    try:
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return host.split(".", 1)[0] or None
    except Exception:
        return None


def _git_commit() -> Optional[str]:
    """The deployed commit, read from `.git` (Streamlit Cloud serves a clone, so it is
    usually there). Falls back to `git rev-parse` and then to None — a missing commit is
    itself informative: it means the host isn't running from a checkout."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".git", "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            with open(os.path.join(root, ".git", ref), encoding="utf-8") as fh:
                return fh.read().strip()[:12]
        return head[:12]
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=5)
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def _pkg_version(name: str) -> Optional[str]:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def _secret_row(name: str) -> dict[str, Any]:
    """One secret's status — never its value."""
    try:
        from db.supabase_client import secret_lookup
        value, source = secret_lookup(name)
    except Exception as exc:
        return {"present": None, "source": None, "error": f"{type(exc).__name__}: {exc}"}
    row: dict[str, Any] = {
        "present": bool(value),
        "source": source,
        "length": len(value) if value else 0,
        "fingerprint": _fingerprint(value),
    }
    if name == "SUPABASE_URL":
        row["project_ref"] = project_ref(value)
        row.pop("fingerprint", None)          # the URL is not a secret; the ref is clearer
    if name == "SUPABASE_KEY":
        label, is_service = key_class(value)
        row["kind"] = label
        row["is_service_role"] = is_service
    return row


def snapshot(user: dict | None = None) -> dict[str, Any]:
    """Everything about THIS runtime that can make it disagree with another one running
    the same code. Every section is separately guarded — one broken probe never costs the
    others."""
    snap: dict[str, Any] = {}

    # -- What code is running ------------------------------------------------
    try:
        from core.app_header import APP_VERSION
    except Exception:
        APP_VERSION = "?"
    snap["build"] = {
        "app_version": APP_VERSION,
        "git_commit": _git_commit(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "streamlit": _pkg_version("streamlit"),
        "supabase": _pkg_version("supabase"),
        "pyjwt": _pkg_version("pyjwt"),
    }

    # -- What configuration it read, and from where --------------------------
    snap["secrets"] = {name: _secret_row(name) for name in _SECRET_NAMES}

    # -- Multi-tenant master switch + the tenant directory -------------------
    mt: dict[str, Any] = {}
    try:
        from auth import tenant_context as tc
        mt["multitenant_enabled"] = tc.multitenant_enabled()
    except Exception as exc:
        mt["multitenant_enabled"] = None
        mt["error"] = f"{type(exc).__name__}: {exc}"
    try:
        from db.supabase_client import service_client
        rows = (service_client().table("tenants")
                .select("id,name,slug,is_platform,status").order("name").execute().data or [])
        mt["tenant_count"] = len(rows)
        mt["tenants"] = [f"{r.get('name')} ({r.get('slug')})"
                         f"{' [platform]' if r.get('is_platform') else ''}" for r in rows]
    except Exception as exc:
        # NOT swallowed into a friendly blank: a failing tenants query IS the diagnosis
        # (wrong key, missing migration, unreachable project).
        mt["tenant_count"] = None
        mt["tenants_query_error"] = f"{type(exc).__name__}: {exc}"
    snap["multitenant"] = mt

    # -- This session's resolved identity ------------------------------------
    sess: dict[str, Any] = {}
    try:
        import streamlit as st  # type: ignore
        ss = st.session_state
        u = user or ss.get("app_user") or {}
        sess["role"] = u.get("role")
        sess["email"] = u.get("email")
        sess["tenant_id"] = ss.get("tenant_id")
        sess["tenant_name"] = ss.get("tenant_name")
        sess["su_view_tenant"] = ss.get("su_view_tenant")
        sess["su_view_name"] = ss.get("su_view_name")
        sess["tenant_jwt_minted"] = bool(ss.get("_tenant_jwt"))
    except Exception as exc:
        sess["error"] = f"{type(exc).__name__}: {exc}"
    try:
        from db.supabase_client import (_NO_TENANT_SENTINEL, _live_tenant_session,
                                        _tenant_scope_tid)
        tid = _tenant_scope_tid()
        sess["live_tenant_session"] = _live_tenant_session()
        sess["data_scope_tenant_id"] = tid
        sess["data_scope"] = ("UNSCOPED - every tenant's rows" if tid is None
                              else "NO ROWS (fail-closed sentinel)"
                              if tid == _NO_TENANT_SENTINEL else "scoped to one tenant")
    except Exception as exc:
        sess["data_scope"] = f"unknown ({type(exc).__name__}: {exc})"
    snap["session"] = sess

    # -- The membership rows the landing logic actually sees -----------------
    try:
        from auth import tenant_context as tc
        from db.supabase_client import service_client
        uid = tc._resolve_user_id(user or {}) if user else None
        snap["memberships"] = {"resolved_user_id": uid}
        if uid:
            rows = (service_client().table("tenant_memberships")
                    .select("tenant_id, role, status, tenants(name, slug, is_platform, status)")
                    .eq("user_id", uid).execute().data or [])
            snap["memberships"]["rows"] = rows
            snap["memberships"]["would_land_in"] = (
                (tc._default_membership(user or {}, tc.active_memberships(uid)) or {})
                .get("name") or "- no default (tenant-less)")
    except Exception as exc:
        snap.setdefault("memberships", {})["error"] = f"{type(exc).__name__}: {exc}"

    # -- Which org identity the header is showing, and from which store ------
    ident: dict[str, Any] = {}
    try:
        from auth import tenant_context as tc
        from core import settings as _settings
        ctx = tc.tenant_store()
        if ctx is not None:
            ident["source"] = f"per-tenant (tenants.org_identity for {ctx[1]})"
        elif tc.multitenant_enabled():
            ident["source"] = ("neutral defaults - multi-tenant is ON but NO tenant "
                               "resolved for this session")
        else:
            ident["source"] = ("LEGACY global app_settings - the pre-multi-tenant "
                               "identity, shared by every user of this deployment")
        ident["displayed_name"] = _settings.get_org_name()
    except Exception as exc:
        ident["error"] = f"{type(exc).__name__}: {exc}"
    snap["org_identity"] = ident

    return snap


def verdicts(snap: dict[str, Any]) -> list[tuple[str, str]]:
    """`(level, message)` conclusions drawn from a snapshot — the part a human should read
    first. Levels: 'error', 'warning', 'info', 'ok'."""
    out: list[tuple[str, str]] = []
    mt = snap.get("multitenant", {}) or {}
    sec = snap.get("secrets", {}) or {}
    sess = snap.get("session", {}) or {}
    enabled = mt.get("multitenant_enabled")
    count = mt.get("tenant_count")

    jwt_row = sec.get("SUPABASE_JWT_SECRET", {}) or {}
    if not jwt_row.get("present"):
        out.append(("error",
                    "SUPABASE_JWT_SECRET is NOT READABLE here. It is the multi-tenant "
                    "master switch: without it every user shares one unscoped pool and "
                    "the header shows the legacy app_settings org identity. If you did "
                    "set it, check it is at the TOP LEVEL of the secrets file (not "
                    "indented under a [section] header) and spelled exactly."))
    elif str(jwt_row.get("source") or "").startswith("st.secrets["):
        out.append(("info",
                    f"SUPABASE_JWT_SECRET was found in {jwt_row['source']} - a nested "
                    "section. It works, but move it to the top level to match the docs."))

    key_row = sec.get("SUPABASE_KEY", {}) or {}
    if key_row.get("is_service_role") is False:
        out.append(("error",
                    f"SUPABASE_KEY is a {key_row.get('kind')}. Identity lookups "
                    "(memberships, the tenant directory) run on the service client and "
                    "will return NOTHING under RLS - users land tenant-less and pages "
                    "read empty."))
    elif key_row.get("is_service_role") is None and key_row.get("present"):
        out.append(("warning", f"SUPABASE_KEY: {key_row.get('kind')} - could not confirm "
                               "it is the service-role key."))

    if enabled is False and isinstance(count, int) and count > 1:
        out.append(("error",
                    f"Multi-tenant mode is OFF while this database holds {count} tenants. "
                    "Every logged-in user reads and writes the UNSCOPED pool - tenant "
                    "isolation is not in effect in this deployment."))
    if enabled and sess.get("live_tenant_session") and not sess.get("tenant_id"):
        out.append(("error",
                    "Multi-tenant is ON but this session resolved NO tenant, so data "
                    "reads fail closed to zero rows. Check the memberships section below."))
    if sess.get("data_scope") == "UNSCOPED - every tenant's rows":
        if sess.get("live_tenant_session"):
            out.append(("warning", "This session's data reads are UNSCOPED."))
        else:
            # A script / cron run (env_report.py, the screening loop) legitimately has no
            # browser session, so no tenant resolves and unscoped IS correct there. Say so,
            # rather than raising an alarm about the reporting tool itself.
            out.append(("info", "No browser session here (CLI / cron), so no tenant "
                                "resolves and reads are unscoped - expected outside the "
                                "app. Compare the sections above, not this line."))
    if mt.get("tenants_query_error"):
        out.append(("error", f"The tenants table could not be read: "
                             f"{mt['tenants_query_error']}"))
    if not out:
        out.append(("ok", f"Multi-tenant is active and this session is scoped to "
                          f"{sess.get('tenant_name') or sess.get('tenant_id')}."))
    return out


# ---------------------------------------------------------------------------
# Streamlit surfaces
# ---------------------------------------------------------------------------

def render(user: dict | None = None) -> None:
    """The full diagnostics panel (Settings -> Accounts -> Deployment, and `?diag=1`).
    Super_user only — the caller gates, and this re-checks."""
    import streamlit as st  # type: ignore
    from core import permissions
    _u = user or st.session_state.get("app_user")
    if not permissions.is_super_user(_u):
        return
    snap = snapshot(_u)
    st.markdown("#### Deployment diagnostics")
    st.caption(
        "What THIS running instance read from its environment. Compare it with your "
        "machine (`python scripts/env_report.py`) to explain any behaviour that differs "
        "between local and the published app. No secret values are shown — a fingerprint "
        "is the first 8 hex of sha256, so identical fingerprints mean identical values.")
    for level, msg in verdicts(snap):
        {"error": st.error, "warning": st.warning,
         "info": st.info, "ok": st.success}.get(level, st.info)(msg)
    st.json(snap, expanded=True)


def render_if_requested(user: dict | None = None) -> None:
    """`?diag=1` renders the panel inline on ANY page, for a super_user only. Deliberately
    reachable outside Settings: the states worth diagnosing are the ones where the app is
    already misbehaving, and Settings may be one of the pages that is wrong."""
    import streamlit as st  # type: ignore
    try:
        want = str(st.query_params.get("diag") or "").lower() in ("1", "true", "yes")
    except Exception:
        return
    if want:
        render(user)


_TENANT_COUNT_CACHE: dict[str, Any] = {"at": 0.0, "n": None}
_TENANT_COUNT_TTL = 300.0


def _tenant_count() -> Optional[int]:
    """How many tenants this database holds (5-min cached, best-effort). Only consulted
    for the degradation banner, and only for an admin/super session."""
    import time
    now = time.time()
    if (now - _TENANT_COUNT_CACHE["at"]) < _TENANT_COUNT_TTL:
        return _TENANT_COUNT_CACHE["n"]
    try:
        from db.supabase_client import service_client
        rows = service_client().table("tenants").select("id").execute().data or []
        _TENANT_COUNT_CACHE["n"] = len(rows)
    except Exception:
        pass                                   # keep last-good; banner stays quiet
    _TENANT_COUNT_CACHE["at"] = now
    return _TENANT_COUNT_CACHE["n"]


def render_degradation_banner(user: dict | None = None) -> None:
    """A loud, persistent banner for the two states in which this deployment is silently
    NOT the app it appears to be:

      * multi-tenant OFF while the database holds more than one tenant — every user is
        reading one unscoped pool under the legacy org identity;
      * multi-tenant ON but the session resolved no tenant — pages fail closed to empty.

    Shown to admins and super_users only (a collaborator can do nothing with it), and
    never on the public pages (no user). This is the safeguard the wrong-tenant landing
    needed: the failure had no symptom beyond a name in the header."""
    import streamlit as st  # type: ignore
    try:
        from core import permissions
        u = user or st.session_state.get("app_user") or {}
        if not permissions.is_admin(u):
            return
        from auth import tenant_context as tc
        enabled = tc.multitenant_enabled()
        if not enabled:
            n = _tenant_count()
            if isinstance(n, int) and n > 1:
                st.error(
                    f"⚠️ **Multi-tenant mode is OFF in this deployment** while the "
                    f"database holds {n} tenants. Every signed-in user is reading the "
                    f"unscoped pool, and the organisation name above is the legacy "
                    f"pre-multi-tenant identity — not your tenant. `SUPABASE_JWT_SECRET` "
                    f"is missing or unreadable here. Open `?diag=1` for details.")
            return
        if not st.session_state.get("tenant_id"):
            st.error(
                "⚠️ **No tenant resolved for your session.** Multi-tenant mode is on, so "
                "every page will read as empty until this resolves. Open `?diag=1` for "
                "details.")
    except Exception:
        return
