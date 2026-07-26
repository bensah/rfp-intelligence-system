"""Multi-tenant Phase 4 — first-login onboarding (signup-source-agnostic).

Routed by STATE, not by how the account was created: a logged-in user with NO active
tenant membership is sent here to create or join a tenant before any app page renders.
This works identically for today's super-admin-provisioned users AND the future Taadom
self-signup — no rework when signup opens.

Flow (a tenant = a CHAI country / global team):
  * type the organization name → matching existing tenants appear;
  * SELECT an existing one → a PENDING membership request (admin approves, Phase 5);
  * or CREATE a new one → the creator becomes its admin (active) and completes the org
    baseline profile in Settings.

DORMANT unless multi-tenant is enabled (SUPABASE_JWT_SECRET set) AND the tenant tables
exist — otherwise `needs_onboarding` returns False and the single-tenant app is
untouched. Never raises into a page.
"""
from __future__ import annotations

import streamlit as st

from auth import tenant_context as tc
from db.supabase_client import get_client


def _tenant_tables_ready() -> bool:
    """True once migration 067 has created the tenant tables (else Phase 4 stays off)."""
    try:
        get_client().table("tenants").select("id").limit(1).execute()
        return True
    except Exception:
        return False


def _all_memberships(user_id: str | None) -> list[dict]:
    """Every membership (any status) for the user, with the tenant name."""
    if not user_id:
        return []
    try:
        return (get_client().table("tenant_memberships")
                .select("tenant_id, role, status, tenants(name)")
                .eq("user_id", user_id).execute().data or [])
    except Exception:
        return []


def _search_tenants(q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2:
        return []
    try:
        return (get_client().table("tenants").select("id, name")
                .ilike("name", f"%{q}%").order("name").limit(limit).execute().data or [])
    except Exception:
        return []


def needs_onboarding(user: dict) -> bool:
    """True when the user must create/join a tenant before using the app. False (a
    no-op) whenever multi-tenant is off or the tenant tables aren't there yet."""
    try:
        if not tc.multitenant_enabled() or not _tenant_tables_ready():
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

    if pending:
        names = ", ".join((m.get("tenants") or {}).get("name") or "your organization"
                           for m in pending)
        st.info(f"⏳ Your request to join **{names}** is awaiting an admin's approval. "
                "You'll get access as soon as it's approved — check back shortly.")
        if st.button("↻ Check again"):
            st.rerun()
        st.stop()

    st.caption(
        "Choose your organization — a CHAI country or global team "
        "(e.g. “CHAI Cameroon”, “CHAI Global Malaria Team”). Start typing to find an "
        "existing one, or create yours if it isn't listed yet.")

    q = st.text_input("Your organization", key="onb_org_query",
                      placeholder="e.g. CHAI Cameroon")
    matches = _search_tenants(q)
    typed = (q or "").strip()
    exact = next((m for m in matches
                  if (m.get("name") or "").strip().lower() == typed.lower()), None)

    if matches:
        opts = {m["name"]: m["id"] for m in matches}
        chosen = st.radio("Matching organizations — select to request access",
                          list(opts.keys()), key="onb_pick")
        if st.button("Request access", type="primary", key="onb_request"):
            _request_join(uid, opts[chosen], chosen)

    if typed and not exact:
        if matches:
            st.divider()
        st.caption(f"Not listed? Create it — you'll become the admin of **{typed}**.")
        if st.button(f"➕ Create “{typed}”", key="onb_create",
                     type=("secondary" if matches else "primary")):
            _create_tenant(user, uid, typed)

    st.stop()


def _request_join(uid: str, tenant_id: str, name: str) -> None:
    try:
        sb = get_client()
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
    sb = get_client()
    try:
        dupe = (sb.table("tenants").select("id").ilike("name", name)
                .limit(1).execute().data or [])
        if dupe:
            st.warning("That organization already exists — find it above and request "
                       "access instead.")
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
