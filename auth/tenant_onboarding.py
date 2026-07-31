"""Multi-tenant Phase 4 — first-login onboarding (signup-source-agnostic).

Routed by STATE, not by how the account was created: a logged-in user with NO active
tenant membership is sent here to create or join a tenant before any app page renders.
This works identically for today's super-admin-provisioned users AND future public
self-signup — no rework when signup opens.

Flow (a tenant = a the organisation country / global team):
  * pick your organization from a dropdown of the tenants that already exist;
  * SELECT an existing one → a PENDING membership request (admin approves, Phase 5);
  * or choose "Create a new organization" → the creator becomes its admin (active) and
    completes the org baseline profile in Settings.

Every DB call here runs on the RLS-BYPASSING service client (see
db.supabase_client.service_client): membership resolution and the tenant directory are
identity/bootstrap operations that must see the true rows before any tenant context
exists and must not be filtered by the tenant tables' RLS state. (Tenant DATA access
elsewhere stays on the tenant-scoped get_client(), so isolation is preserved.)

DORMANT unless multi-tenant is enabled (SUPABASE_JWT_SECRET set) AND the tenant tables
exist — otherwise `needs_onboarding` returns False and the single-tenant app is
untouched. Never raises into a page.
"""
from __future__ import annotations

import streamlit as st

from auth import tenant_context as tc
from db.supabase_client import service_client

# Sentinel row in the org dropdown for "none of these — create a new one".
_CREATE_NEW = "➕ Create a new organization…"


def _tenant_tables_ready() -> bool:
    """True once migration 067 has created the tenant tables (else Phase 4 stays off)."""
    try:
        service_client().table("tenants").select("id").limit(1).execute()
        return True
    except Exception:
        return False


def _all_memberships(user_id: str | None) -> list[dict]:
    """Every membership (any status) for the user, with the tenant name."""
    if not user_id:
        return []
    try:
        return (service_client().table("tenant_memberships")
                .select("tenant_id, role, status, tenants(name)")
                .eq("user_id", user_id).execute().data or [])
    except Exception:
        return []


def _list_tenants(limit: int = 200) -> list[dict]:
    """All ACTIVE tenants (id, name) for the onboarding dropdown, name-ordered.

    A plain full list, not a type-ahead: the previous free-text search silently returned
    [] whenever the query was filtered out by RLS, which read to the user as "no matches"."""
    try:
        return (service_client().table("tenants").select("id, name")
                .eq("status", "active").order("name").limit(limit).execute().data or [])
    except Exception:
        return []


def needs_onboarding(user: dict) -> bool:
    """True when the user must create/join a tenant before using the app. False (a
    no-op) whenever multi-tenant is off or the tenant tables aren't there yet.

    Never gates:
      * the super_user (platform owner; home tenant = RFPIS, seeded by migration 070);
      * anyone who already holds at least one ACTIVE membership;
      * a user whose id can't be resolved (don't trap them behind a broken gate)."""
    try:
        if not tc.multitenant_enabled() or not _tenant_tables_ready():
            return False
        if (user.get("role") or "").lower() == "super_user":
            return False
        uid = tc._resolve_user_id(user)
        if not uid:
            return False
        mems = _all_memberships(uid)
        return not any(m.get("status") == "active" for m in mems)
    except Exception:
        return False


def render_onboarding(user: dict) -> None:
    """Full-page onboarding gate. Renders the setup flow and st.stop()s so no app
    content shows until the user holds an ACTIVE tenant membership."""
    uid = tc._resolve_user_id(user)
    mems = _all_memberships(uid)
    pending = [m for m in mems if m.get("status") == "pending"]

    st.title("Set up your workspace")

    # Opt-in diagnostics (append ?onbdebug=1 to the URL) — surfaces the state the gate
    # decides on, WITHOUT swallowing errors, so a stuck gate can be debugged in-app.
    if _debug_enabled():
        _render_debug(user, uid, mems)

    if pending:
        names = ", ".join((m.get("tenants") or {}).get("name") or "your organization"
                           for m in pending)
        st.info(f"⏳ Your request to join **{names}** is awaiting an admin's approval. "
                "You'll get access as soon as it's approved — check back shortly.")
        if st.button("↻ Check again"):
            st.rerun()
        st.stop()

    st.caption(
        "Choose your organization — a the organisation country or global team "
        "(e.g. “the organisation Cameroon”, “the organisation Global Malaria Team”). Pick an existing one to "
        "request access, or create yours if it isn't listed yet.")

    tenants = _list_tenants()
    by_name = {t["name"]: t["id"] for t in tenants}
    options = list(by_name.keys()) + [_CREATE_NEW]

    choice = st.selectbox("Your organization", options, key="onb_org_choice",
                          index=None, placeholder="Select your organization…")

    if choice and choice != _CREATE_NEW:
        st.caption(f"Request access to **{choice}** — an admin approves before you see "
                   "any data.")
        if st.button("Request access", type="primary", key="onb_request"):
            _request_join(uid, by_name[choice], choice)

    elif choice == _CREATE_NEW:
        new_name = st.text_input("New organization name", key="onb_new_name",
                                 placeholder="e.g. the organisation Zimbabwe")
        typed = (new_name or "").strip()
        if typed:
            st.caption(f"You'll become the admin of **{typed}**.")
        if st.button(f"➕ Create organization", type="primary", key="onb_create",
                     disabled=not typed):
            _create_tenant(user, uid, typed)

    st.stop()


def _debug_enabled() -> bool:
    """True when the URL carries ?onbdebug=1 (opt-in onboarding diagnostics)."""
    try:
        v = st.query_params.get("onbdebug")
    except Exception:
        return False
    return str(v).lower() in ("1", "true", "yes")


def _render_debug(user: dict, uid: str | None, mems: list[dict]) -> None:
    """Show the resolved gate inputs and re-run the key queries WITHOUT swallowing
    exceptions, so silent failures (RLS, connectivity, missing migration) are visible."""
    with st.expander("🔎 Onboarding diagnostics", expanded=True):
        st.write({
            "multitenant_enabled": tc.multitenant_enabled(),
            "tenant_tables_ready": _tenant_tables_ready(),
            "role": user.get("role"),
            "resolved_user_id": uid,
            "app_user_has_id": bool(user.get("id")),
            "memberships": mems,
            "active_membership_count":
                sum(1 for m in mems if m.get("status") == "active"),
        })
        st.caption("Raw queries (service client, errors NOT swallowed):")
        try:
            rows = (service_client().table("tenant_memberships")
                    .select("tenant_id, role, status, tenants(name)")
                    .eq("user_id", uid).execute().data)
            st.success(f"tenant_memberships → {rows}")
        except Exception as exc:
            st.error(f"tenant_memberships query FAILED: {type(exc).__name__}: {exc}")
        try:
            rows = (service_client().table("tenants")
                    .select("id, name, status").order("name").execute().data)
            st.success(f"tenants → {rows}")
        except Exception as exc:
            st.error(f"tenants query FAILED: {type(exc).__name__}: {exc}")


def _request_join(uid: str, tenant_id: str, name: str) -> None:
    try:
        sb = service_client()
        # idempotent: don't stack duplicate requests
        existing = (sb.table("tenant_memberships").select("id, status")
                    .eq("user_id", uid).eq("tenant_id", tenant_id).limit(1)
                    .execute().data or [])
        if existing:
            st.info(f"You already have a {existing[0].get('status')} membership for {name}.")
        else:
            sb.table("tenant_memberships").insert({
                "tenant_id": tenant_id, "user_id": uid,
                "role": "collaborator", "status": "pending"}).execute()
            st.success(f"✅ Request sent to join **{name}**. Awaiting admin approval.")
        st.rerun()
    except Exception as exc:
        st.error(f"Couldn't send the request: {exc}")


def _create_tenant(user: dict, uid: str, name: str) -> None:
    sb = service_client()
    try:
        dupe = (sb.table("tenants").select("id").ilike("name", name)
                .limit(1).execute().data or [])
        if dupe:
            st.warning("That organization already exists — pick it from the list and "
                       "request access instead.")
            return
        created = (sb.table("tenants").insert(
            {"name": name, "created_by": uid, "status": "active"}).execute().data or [])
        if not created:
            st.error("Couldn't create the organization (no row returned).")
            return
        tid = created[0]["id"]
        sb.table("tenant_memberships").insert({
            "tenant_id": tid, "user_id": uid, "role": "admin", "status": "active"}).execute()
        # switch this session into the new tenant (mints the tenant JWT)
        tc.set_active_tenant(user, tid, role="admin", name=name)
        st.success(f"✅ Created **{name}** — you're its admin. Finish your organization "
                   "profile in **Settings → Setup** to sharpen eligibility screening.")
        st.rerun()
    except Exception as exc:
        st.error(f"Couldn't create the organization: {exc}")
