"""Reusable account-management sections.

Extracted from the old monolithic `app_pages/user.py` so the same logic
can power TWO surfaces after the 2026-06-07 nav redesign:

  * Profile page  (top-right user menu)   → my_profile + change_password
  * Settings page (admin only)            → user_access + manage_users

Each `render_*` function is self-contained and takes the current `user`
dict (+ a Supabase client where it writes), so the calling page just does:

    from views.account_sections import render_my_profile
    render_my_profile(user, sb)
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone

import bcrypt
import pandas as pd
import streamlit as st

from auth.authenticator import hash_password, clear_credentials_cache
from core import permissions
from core.geographies import COUNTRIES
from db.supabase_client import service_client

# App-owner / Super User contact for account & data requests (permanent deletion,
# reactivating a suspended org). Temporary personal address — single source of truth,
# also surfaced on the Help page.
ADMIN_CONTACT_EMAIL = "nsah.ben03@gmail.com"

# Sentinel option in the "Assign to tenant" picker: creates a personal ('individual'
# kind) tenant for the user instead of an organization. Individual tenants are PUBLIC —
# their activity is visible to all users (migration 078 + db.supabase_client scoping).
_INDIVIDUAL_TENANT_LABEL = "🧑 Individual — personal account (visible to all)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fetch_self(sb, email: str) -> dict:
    """Re-read the current user's row so the form reflects any out-of-band
    changes (e.g. admin updated the role) without needing a hard refresh."""
    res = (
        sb.table("users").select("*").eq("email", email).limit(1).execute()
    )
    return (res.data or [{}])[0]


def _verify_password(plain: str, stored_hash: str) -> bool:
    if not plain or not stored_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), stored_hash.encode("utf-8"))
    except Exception:
        return False


def _gen_temp_password(length: int = 12) -> str:
    """URL-safe random temp password (letters + digits). Used by admin
    'reset password' — user is forced to change it on next login."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ===========================================================================
# My Profile
# ===========================================================================
def render_my_profile(user: dict, sb) -> None:
    me = _fetch_self(sb, user["email"])
    st.subheader("My profile")
    st.caption(
        "Edit your own contact info, including email. **Role is "
        "read-only** — only an admin can change it. Changing your "
        "email also changes your login username on next session."
    )

    st.text_input("Role (read-only)", value=me.get("role") or "collaborator",
                  disabled=True, key="pf_role")

    with st.form("my_profile_form"):
        f1, f2 = st.columns(2)
        new_name = f1.text_input(
            "Full name", value=me.get("name") or "",
            help="Shown on the header, in reports, and on the team rota.")
        new_email = f2.text_input(
            "Email (login username)", value=me.get("email") or "",
            help="⚠ This is your login username. Changing it means you'll "
                 "need to log in with the new email on next session. Your "
                 "current cookie session continues until it expires (~8h).")
        f3, f4 = st.columns(2)
        new_phone = f3.text_input(
            "Phone", value=me.get("phone") or "",
            help="Include country code (e.g. +237 6XX XX XX XX).")
        new_title = f4.text_input(
            "Job title", value=me.get("job_title") or "",
            help="e.g. 'BD Coordinator', 'Senior Programme Manager'.")
        f5, f6 = st.columns(2)
        new_dept = f5.text_input(
            "Department", value=me.get("department") or "",
            help="e.g. 'Business Development', 'Programmes'.")
        new_program = f6.text_input(
            "Program areas", value=me.get("program") or "",
            help="Free-text, comma-separated. e.g. 'Vaccines, MCH, Malaria'. "
                 "Used by the Report to attribute scans by program focus.")
        # Location (migration 069). Country is a canonical dropdown so values stay
        # consistent for any downstream geo reporting.
        f7, f8 = st.columns(2)
        new_address = f7.text_input(
            "Address", value=me.get("address") or "",
            help="Street address / building.")
        new_city = f8.text_input(
            "City / Town", value=me.get("city") or "")
        f9, f10 = st.columns(2)
        new_state = f9.text_input(
            "State / Province / Region", value=me.get("state_region") or "")
        _country_opts = [""] + COUNTRIES
        _cur_country = me.get("country") or ""
        new_country = f10.selectbox(
            "Country", _country_opts,
            index=_country_opts.index(_cur_country) if _cur_country in _country_opts else 0)
        save = st.form_submit_button("💾 Save profile", type="primary")

    if save:
        errs: list[str] = []
        new_email_clean = (new_email or "").strip()
        if not new_email_clean or "@" not in new_email_clean:
            errs.append("Email must be a valid address.")
        if (not errs and new_email_clean.lower()
                != (me.get("email") or "").lower()):
            existing = (
                sb.table("users").select("email")
                .ilike("email", new_email_clean).limit(1)
                .execute().data or []
            )
            if existing:
                errs.append("Another user already has that email.")

        if errs:
            st.error("Please fix:\n\n- " + "\n- ".join(errs))
        else:
            try:
                sb.table("users").update({
                    "name":         (new_name or "").strip() or None,
                    "email":        new_email_clean,
                    "phone":        (new_phone or "").strip() or None,
                    "job_title":    (new_title or "").strip() or None,
                    "department":   (new_dept or "").strip() or None,
                    "program":      (new_program or "").strip() or None,
                    "address":      (new_address or "").strip() or None,
                    "city":         (new_city or "").strip() or None,
                    "state_region": (new_state or "").strip() or None,
                    "country":      (new_country or "").strip() or None,
                }).eq("email", user["email"]).execute()
                clear_credentials_cache()
                user["name"] = (new_name or "").strip() or user.get("name")
                user["email"] = new_email_clean
                st.session_state["app_user"] = user
                email_changed = (
                    new_email_clean.lower() != (me.get("email") or "").lower())
                st.toast(
                    "✅ Profile saved."
                    + (f" Login email is now {new_email_clean}."
                       if email_changed else ""),
                    icon="✅")
                st.rerun()
            except Exception as exc:
                st.error(f"Save failed: {exc}")

    st.divider()
    fc1, fc2, fc3 = st.columns(3)
    fc1.metric("Account created",
               (me.get("created_at") or "—").split("T")[0])
    fc2.metric("Last login",
               (me.get("last_login_at") or "—").split("T")[0]
               if me.get("last_login_at") else "—")
    fc3.metric("Password set",
               (me.get("password_changed_at") or "—").split("T")[0]
               if me.get("password_changed_at") else "—")

    # ── Danger zone — self-service account deletion ─────────────────────
    st.divider()
    with st.expander("⚠️ Danger zone — delete my account", expanded=False):
        st.caption(
            "Permanently removes your login. Your contributed records (meeting notes, "
            "engagements, submitted opportunities) **stay in the system** for institutional "
            "memory — only your ability to sign in is removed. This cannot be undone. To "
            "delete an entire **organization's** data, an admin must contact the Super "
            f"User ({ADMIN_CONTACT_EMAIL}).")

        _sole_super = False
        if permissions.is_super_user(user):
            try:
                _cnt = int((sb.table("users").select("id", count="exact")
                            .eq("role", "super_user").execute().count) or 0)
            except Exception:
                _cnt = 2  # fail-open — never block on a transient count error
            if _cnt <= 1:
                _sole_super = True
                st.warning(
                    "You're the **only Super User** — deleting your account would lock "
                    "everyone out of platform administration. Promote another Super User "
                    "first (Settings → Accounts → Users).")

        @st.dialog("Delete my account", width="medium")
        def _delete_me_dialog(_email=user.get("email"), _uid=me.get("id")):
            st.error(
                f"This permanently deletes **{_email}** and signs you out. Your records "
                "remain for institutional memory. This cannot be undone.")
            _typed = st.text_input(f"Type your email to confirm: {_email}",
                                   key="pf_del_confirm")
            dc1, dc2 = st.columns(2)
            if dc1.button(
                    "🗑 Permanently delete my account", type="primary", width='stretch',
                    disabled=(_typed or "").strip().lower() != (_email or "").lower(),
                    key="pf_del_go"):
                try:
                    if _uid:                       # drop memberships → no orphan rows
                        try:
                            service_client().table("tenant_memberships").delete().eq(
                                "user_id", _uid).execute()
                        except Exception:
                            pass
                    sb.table("users").delete().eq("email", _email).execute()
                    clear_credentials_cache()
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")
                    return
                st.session_state.clear()
                st.success("Your account has been deleted. You have been signed out.")
                st.stop()
            if dc2.button("Cancel", width='stretch', key="pf_del_cancel"):
                st.rerun()

        if not _sole_super:
            if st.button("🗑 Delete my account…", key="pf_delete_me"):
                st.session_state.pop("pf_del_confirm", None)
                _delete_me_dialog()


# ===========================================================================
# Change Password
# ===========================================================================
def render_change_password(user: dict, sb) -> None:
    me = _fetch_self(sb, user["email"])
    st.subheader("Change password")
    st.caption(
        "You'll need your current password to confirm. New password "
        "must be at least 8 characters and include a mix of letters + "
        "digits.")

    with st.form("change_password_form", clear_on_submit=True):
        current_pw = st.text_input("Current password", type="password",
                                   key="pw_current")
        new_pw = st.text_input("New password", type="password", key="pw_new")
        confirm_pw = st.text_input("Confirm new password", type="password",
                                   key="pw_confirm")
        change = st.form_submit_button("🔐 Change password", type="primary")

    if change:
        errors: list[str] = []
        if not _verify_password(current_pw or "", me.get("password_hash") or ""):
            errors.append("Current password is incorrect.")
        if not new_pw or len(new_pw) < 8:
            errors.append("New password must be at least 8 characters.")
        if new_pw and (not any(c.isalpha() for c in new_pw)
                       or not any(c.isdigit() for c in new_pw)):
            errors.append("New password must include letters AND digits.")
        if new_pw != confirm_pw:
            errors.append("Confirm password does not match.")
        if new_pw and new_pw == current_pw:
            errors.append("New password must be different from current.")

        if errors:
            st.error("Please fix:\n\n- " + "\n- ".join(errors))
        else:
            try:
                sb.table("users").update({
                    "password_hash": hash_password(new_pw),
                    "password_changed_at": datetime.now(timezone.utc).isoformat(),
                    "must_change_password": False,
                }).eq("email", user["email"]).execute()
                clear_credentials_cache()
                st.success("Password changed. Future logins will use the "
                           "new password.")
            except Exception as exc:
                st.error(f"Change failed: {exc}")


# ===========================================================================
# User Access (read-only matrix) — admin-facing after the redesign
# ===========================================================================
def render_user_access(user: dict, target: dict | None = None) -> None:
    """Access reference for a user. When `target` is supplied (the row picked in the
    Users table) it shows THAT user's effective access — role policy plus any
    per-surface overrides — so 'Access' reads as the selected user's card; with no
    target it falls back to the current user's own access."""
    subject = target or user
    is_other = bool(target) and target.get("email") != user.get("email")
    who = subject.get("name") or subject.get("email") or "this user"
    st.markdown(f"#### 🔑 User Access Privileges — {who}" if is_other
                else "#### 🔑 User Access Privileges")
    st.caption(
        f"What **{who}** can see and do across the app, by role"
        + (" (with their per-user overrides applied)" if is_other else "")
        + ". Read-only — change it via **✏️ Edit → per-surface overrides** on the "
        "user selected above.")

    rg = permissions.role_group(subject)
    overrides = (subject.get("access_overrides")
                 if isinstance(subject.get("access_overrides"), dict) else {})
    rows = []
    for surface, role_caps in permissions.ACCESS_MATRIX.items():
        cap = role_caps.get(rg, "hidden")
        eff = overrides.get(surface)
        rows.append({
            "Surface": surface,
            "Effective access": str(eff) if eff else permissions.capability_label(cap),
            "Role default": permissions.capability_label(cap),
            "Admin access": permissions.capability_label(
                role_caps.get("admin", "hidden")),
        })
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)


# ===========================================================================
# Manage Users (admin / super_user only)
# ===========================================================================
def _set_many_status(svc, tids: list[str], status: str) -> None:
    """Bulk activate/suspend the given tenants (one round-trip via `in_`)."""
    if not tids:
        return
    try:
        svc.table("tenants").update({"status": status}).in_("id", tids).execute()
    except Exception as exc:
        st.error(f"Status change failed: {exc}")


def _set_many_tenant_blacklist(svc, tids: list[str], on: bool, by: str | None) -> None:
    """Blacklist (on → status='blacklisted') or restore (off → status='active') the
    given tenants, stamping the migration-077 audit trail. Falls back to a status-only
    update if the audit columns aren't present yet."""
    if not tids:
        return
    if on:
        payload = {"status": "blacklisted",
                   "blacklisted_at": datetime.now(timezone.utc).isoformat(),
                   "blacklisted_by": by}
    else:
        payload = {"status": "active", "blacklisted_at": None,
                   "blacklisted_by": None, "blacklist_reason": None}
    try:
        svc.table("tenants").update(payload).in_("id", tids).execute()
    except Exception:
        try:
            svc.table("tenants").update(
                {"status": "blacklisted" if on else "active"}).in_("id", tids).execute()
        except Exception as exc:
            st.error(f"Blacklist change failed: {exc}")


def render_manage_tenants(user: dict, sb) -> None:
    """Settings → Tenants (SUPER USER only). Tenants = organizations registered to the
    platform (a the organisation country / global team now; external orgs later). A management TABLE
    with per-row "Open ↗" links to each tenant's Organization page + multi-row select for
    bulk Suspend / Activate. Renaming is done in the Organization editor (the "Organization
    name" field), not here. Deletion is intentionally omitted — suspend instead, so a
    tenant's data is never orphaned.

    Tenant + membership rows are read/written on the RLS-BYPASSING service client (these
    are privileged platform-admin operations; the passed-in `sb` would be the caller's
    tenant-scoped client and hit RLS on the tenant tables — the 42501 create failure)."""
    if not permissions.is_super_user(user):
        st.error("Tenants are managed by the Super User only.")
        return
    svc = service_client()

    st.subheader("Tenants")
    st.caption(
        "Tenants registered to the platform — **🏢 organizations** (isolated data) or "
        "**🧑 individuals** (personal accounts whose activity is visible to all users). "
        "Users belong to a tenant via membership. Suspend — don't delete — to retire a "
        "tenant without orphaning its records. **Click a tenant name to open its "
        "Organization page** (view / edit that tenant's identity + profile). Funding "
        "opportunities are shared platform-wide and screened against each tenant's own "
        "preferences.")

    # ── Add tenant ──────────────────────────────────────────────────────
    with st.form("add_tenant_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([3, 1.2, 1])
        t_name = c1.text_input(
            "New tenant name",
            placeholder="e.g. the organisation Zimbabwe, the organisation Programme Team, Example Tenant")
        t_kind = c2.selectbox(
            "Type", ["Organization", "Individual"], key="add_tenant_kind",
            help="Organization = a normal org tenant. Individual = a personal account "
                 "whose activity is visible to all users.")
        add = c3.form_submit_button("➕ Add tenant", type="primary", width='stretch')
    if add:
        nm = (t_name or "").strip()
        if not nm:
            st.warning("Enter a tenant name.")
        else:
            _kind = "individual" if t_kind == "Individual" else "organization"
            try:
                dupe = (svc.table("tenants").select("id").ilike("name", nm)
                        .limit(1).execute().data or [])
                if dupe:
                    st.warning("A tenant with that name already exists.")
                else:
                    try:
                        svc.table("tenants").insert(
                            {"name": nm, "kind": _kind, "status": "active",
                             "created_by": user.get("id")}).execute()
                    except Exception:
                        # Pre-migration-078 fallback (no `kind` column) → org tenant.
                        svc.table("tenants").insert(
                            {"name": nm, "status": "active",
                             "created_by": user.get("id")}).execute()
                    st.success(f"Created {_kind} “{nm}”.")
                    st.rerun()
            except Exception as exc:
                st.error(f"Create failed: {exc}")

    # ── Management table ─────────────────────────────────────────────────
    # Try selecting `kind` (migration 078); fall back to the pre-078 column set so the
    # table still loads before the migration is applied.
    tenants, _last_exc = None, None
    for _sel in ("id,name,slug,status,kind,created_at,org_profile",
                 "id,name,slug,status,created_at,org_profile"):
        try:
            tenants = (svc.table("tenants").select(_sel)
                       .order("name").execute().data or [])
            break
        except Exception as exc:
            _last_exc = exc
            tenants = None
    if tenants is None:
        st.error(f"Couldn't load tenants: {_last_exc}")
        return
    # Blacklisted tenants (migration 077) are managed under the Blacklisted tab — hide
    # them here so this table only shows live (active / suspended) organizations.
    tenants = [t for t in tenants if (t.get("status") or "active") != "blacklisted"]
    if not tenants:
        st.info("No tenants yet.")
        return

    try:
        mems = (svc.table("tenant_memberships").select("tenant_id,status")
                .execute().data or [])
    except Exception:
        mems = []
    from collections import Counter
    active_ct = Counter(m["tenant_id"] for m in mems if m.get("status") == "active")
    pending_ct = Counter(m["tenant_id"] for m in mems if m.get("status") == "pending")

    st.markdown("---")
    # One row per tenant. The Organization NAME is itself the entry link (opens the
    # tenant's Organization page in a NEW TAB). That page — and every page navigated
    # from it — runs in a STICKY super_user 'view-as' of the tenant: scoped across all
    # pages and reflected in the browser URL (?tenant=<slug>) until Return to my account.
    # The `&label=<name>` suffix on the href lets the LinkColumn show the tenant NAME as
    # the clickable text (regex-extracted); the app reads only ?tenant= and ignores label.
    # Tick rows to Suspend / Activate in bulk.
    _rows = []
    for t in tenants:
        tid = t["id"]
        prof = t.get("org_profile") if isinstance(t.get("org_profile"), dict) else {}
        _key = t.get("slug") or tid
        _name = t.get("name") or "—"
        _rows.append({
            "id": tid,
            "slug": _key,
            "name": _name,
            "Organization": f"/organization?tenant={_key}&label={_name}",
            "Kind": "🧑 Individual" if t.get("kind") == "individual" else "🏢 Org",
            "_status": (t.get("status") or "active"),
            "Status": "🟢 Active" if (t.get("status") or "active") == "active"
                      else "⏸ Suspended",
            "Members": int(active_ct.get(tid, 0)),
            "Pending": int(pending_ct.get(tid, 0)),
            "Profile": "set" if prof else "empty",
            "Created": (t.get("created_at") or "")[:10],
        })
    _df = pd.DataFrame(_rows)

    _sel = st.dataframe(
        _df[["Organization", "Kind", "Status", "Members", "Pending", "Profile",
             "Created"]],
        hide_index=True, width="stretch", key="tenants_table",
        selection_mode="multi-row", on_select="rerun",
        column_config={
            "Organization": st.column_config.LinkColumn(
                "Organization", display_text=r"label=(.+)$", width="large",
                help="Click a tenant name to open its Organization page in a new tab. "
                     "That tab runs as a sticky super_user 'view-as' of the tenant — "
                     "scoped across every page and shown in the URL (?tenant=…) until "
                     "you Return to your account (banner up top)."),
            "Kind": st.column_config.TextColumn(
                "Kind", width="small",
                help="🏢 Org = a normal organization tenant. 🧑 Individual = a personal "
                     "account whose activity is visible to all users."),
            "Members": st.column_config.NumberColumn("Members", width="small"),
            "Pending": st.column_config.NumberColumn("Pending", width="small"),
        })

    _picked = (getattr(_sel, "selection", None) or {}).get("rows") or []
    _sel_rows = [_rows[i] for i in _picked if 0 <= i < len(_rows)]
    _sel_ids = [r["id"] for r in _sel_rows]

    if not _sel_ids:
        st.caption("**Click a tenant name** to open its Organization page in a new tab "
                   "(super_user view-as). Tick one or more rows to **Suspend** / "
                   "**Activate** them.")
        return

    st.markdown(f"**{len(_sel_ids)} selected:** "
                + ", ".join(f"`{r['name']}`" for r in _sel_rows))
    # Enable each action only when it would actually change something for the selection:
    # Suspend needs an active tenant; Activate needs a suspended one. (Blacklisted tenants
    # aren't in this table — they're reactivated from the Blacklisted tab.)
    _statuses = {r.get("_status", "active") for r in _sel_rows}
    _can_suspend = any(s == "active" for s in _statuses)
    _can_activate = any(s != "active" for s in _statuses)
    b1, b2, b3, _sp = st.columns([1.3, 1.3, 1.5, 3.9])
    if b1.button("⏸ Suspend", width="stretch", key="tn_bulk_suspend",
                 disabled=not _can_suspend,
                 help=None if _can_suspend
                 else "All selected tenants are already suspended."):
        _set_many_status(svc, _sel_ids, "suspended"); st.rerun()
    if b2.button("🟢 Activate", type="primary", width="stretch",
                 key="tn_bulk_activate", disabled=not _can_activate,
                 help=None if _can_activate
                 else "All selected tenants are already active."):
        _set_many_status(svc, _sel_ids, "active"); st.rerun()
    if b3.button("🚫 Blacklist", width="stretch", key="tn_bulk_blacklist",
                 help="Hard-block: members of a blacklisted tenant lose all access. "
                      "Manage / undo under the Blacklisted tab."):
        _set_many_tenant_blacklist(svc, _sel_ids, True, user.get("email")); st.rerun()


def render_org_suspend(user: dict, sb) -> None:
    """Admin self-service (Settings → Setup): suspend — never delete — THIS org's tenant
    account. Suspending pauses auto-scans and retires the account while KEEPING every
    record for later retrieval. Reactivation and any permanent deletion are Super-User-only
    (out-of-band, see the Help page). Shown to a tenant admin, not the super_user (who
    manages every tenant from Accounts → Tenants)."""
    try:
        from auth import tenant_context as tc
        if not tc.multitenant_enabled():
            return
    except Exception:
        return
    if not permissions.is_admin(user) or permissions.is_super_user(user):
        return
    try:
        tid = tc.current_tenant_id()
    except Exception:
        tid = None
    if not tid:
        return

    svc = service_client()
    try:
        _t = (svc.table("tenants").select("name,status").eq("id", tid).limit(1)
              .execute().data or [{}])[0]
    except Exception:
        _t = {}
    _name = _t.get("name") or "your organization"
    _status = _t.get("status") or "active"

    st.divider()
    with st.expander("⚠️ Danger zone — suspend this organization's account",
                     expanded=False):
        if _status == "suspended":
            st.warning(
                f"**{_name}** is currently **suspended** — auto-scans are paused and the "
                "account is retired. Reactivation is handled by the Super User (app "
                f"developer) only — contact **{ADMIN_CONTACT_EMAIL}**.")
            return
        if _status == "blacklisted":
            st.error(f"**{_name}** is blocked by the platform. Contact "
                     f"**{ADMIN_CONTACT_EMAIL}**.")
            return
        st.caption(
            "Suspends the whole organization account: **auto-scans stop** and the org is "
            "retired. **Records are kept** for institutional memory — nothing is deleted "
            "here. Reactivating a suspended org, or a **permanent deletion**, is done by "
            f"the Super User (app developer) only — contact **{ADMIN_CONTACT_EMAIL}**.")

        @st.dialog(f"Suspend {_name}", width="medium")
        def _suspend_org_dialog(_svc=svc, _tid=tid, _nm=_name):
            st.error(
                f"Suspending **{_nm}** pauses auto-scans and retires the account. Records "
                "are preserved and nothing is deleted. Reactivation is Super-User-only "
                f"(contact {ADMIN_CONTACT_EMAIL}). Continue?")
            _typed = st.text_input(
                f"Type the organization name to confirm: {_nm}", key="org_susp_confirm")
            c1, c2 = st.columns(2)
            if c1.button("⏸ Suspend organization", type="primary", width='stretch',
                         disabled=(_typed or "").strip().lower() != (_nm or "").strip().lower(),
                         key="org_susp_go"):
                try:
                    _svc.table("tenants").update(
                        {"status": "suspended"}).eq("id", _tid).execute()
                except Exception as exc:
                    st.error(f"Suspend failed: {exc}")
                    return
                st.success(f"“{_nm}” suspended. Auto-scans are paused; contact the Super "
                           "User to reactivate.")
                st.rerun()
            if c2.button("Cancel", width='stretch', key="org_susp_cancel"):
                st.rerun()

        if st.button("⏸ Suspend organization…", key="org_suspend_open"):
            st.session_state.pop("org_susp_confirm", None)
            _suspend_org_dialog()


def render_blacklisted(user: dict, sb) -> None:
    """Settings → Accounts → Blacklisted (SUPER USER only). One place to see every
    hard-blocked user and tenant (migration 077) and lift the block. Blacklisting itself
    happens from the Users / Tenants tabs; this tab is the register + the undo."""
    if not permissions.is_super_user(user):
        st.error("The blacklist is managed by the Super User only.")
        return
    svc = service_client()

    st.subheader("Blacklisted")
    st.caption(
        "Hard-blocked **users** (can't log in or sign up) and **tenants** (their members "
        "lose all access) — stronger than deactivate / suspend. Select rows and "
        "**Remove from blacklist** to restore. Add to the blacklist from the Users / "
        "Tenants tabs.")

    # ── Blacklisted users ────────────────────────────────────────────────
    st.markdown("#### 👤 Users")
    try:
        _bu = (svc.table("users")
               .select("email,name,role,blacklisted_at,blacklisted_by,blacklist_reason")
               .eq("is_blacklisted", True)
               .order("blacklisted_at", desc=True).execute().data or [])
    except Exception:
        _bu = []
        st.caption("No blacklisted users (or migration 077 isn't applied yet).")
    if _bu:
        _udf = pd.DataFrame(_bu)
        _ucols = [c for c in ["email", "name", "role", "blacklisted_at",
                              "blacklisted_by", "blacklist_reason"] if c in _udf.columns]
        _usel = st.dataframe(
            _udf[_ucols], hide_index=True, width="stretch",
            selection_mode="multi-row", on_select="rerun", key="bl_users_table",
            column_config={"blacklisted_at": "Blacklisted", "blacklisted_by": "By",
                           "blacklist_reason": "Reason"})
        _upick = (getattr(_usel, "selection", None) or {}).get("rows") or []
        _u_emails = [_bu[i]["email"] for i in _upick if 0 <= i < len(_bu)]
        if _u_emails and st.button(
                f"♻ Remove {len(_u_emails)} user(s) from blacklist",
                type="primary", key="bl_users_restore"):
            try:
                svc.table("users").update(
                    {"is_blacklisted": False, "blacklisted_at": None,
                     "blacklisted_by": None, "blacklist_reason": None}
                ).in_("email", _u_emails).execute()
                clear_credentials_cache()
                st.toast(f"♻ Restored {len(_u_emails)} user(s)", icon="♻")
                st.rerun()
            except Exception as exc:
                st.error(f"Restore failed: {exc}")
    elif not _bu:
        st.caption("No blacklisted users.")

    st.divider()

    # ── Blacklisted tenants ──────────────────────────────────────────────
    st.markdown("#### 🏢 Tenants")
    try:
        _bt = (svc.table("tenants")
               .select("id,name,slug,blacklisted_at,blacklisted_by,blacklist_reason")
               .eq("status", "blacklisted").order("name").execute().data or [])
    except Exception:
        _bt = []
    if _bt:
        _tdf = pd.DataFrame(_bt)
        _tcols = [c for c in ["name", "slug", "blacklisted_at", "blacklisted_by",
                              "blacklist_reason"] if c in _tdf.columns]
        _tsel = st.dataframe(
            _tdf[_tcols], hide_index=True, width="stretch",
            selection_mode="multi-row", on_select="rerun", key="bl_tenants_table",
            column_config={"blacklisted_at": "Blacklisted", "blacklisted_by": "By",
                           "blacklist_reason": "Reason"})
        _tpick = (getattr(_tsel, "selection", None) or {}).get("rows") or []
        _t_ids = [_bt[i]["id"] for i in _tpick if 0 <= i < len(_bt)]
        if _t_ids and st.button(
                f"♻ Remove {len(_t_ids)} tenant(s) from blacklist",
                type="primary", key="bl_tenants_restore"):
            _set_many_tenant_blacklist(svc, _t_ids, False, user.get("email"))
            st.rerun()
    else:
        st.caption("No blacklisted tenants.")


def render_manage_users(user: dict, sb) -> None:
    # ─── Add User modal ────────────────────────────────────────────────
    @st.dialog("Add a new user", width="large")
    def _add_user_dialog():
        st.caption(
            "Creates the account immediately with a 12-char temp password "
            "shown on save. Share the temp password out-of-band (Signal / "
            "verbal — never email).")
        from auth import tenant_context as tc
        _mt = tc.multitenant_enabled()
        _is_super = permissions.is_super_user(user)
        d_tenant_name = None
        _tenant_opts: dict[str, str] = {}
        if _mt and _is_super:
            try:
                _tenant_opts = {t["name"]: t["id"] for t in
                                (service_client().table("tenants").select("id,name")
                                 .eq("status", "active").order("name")
                                 .execute().data or [])}
            except Exception:
                _tenant_opts = {}
        with st.form("add_user_dialog_form", clear_on_submit=False):
            dc1, dc2 = st.columns(2)
            d_email = dc1.text_input(
                "Email *", help="Used as the login username.", key="adu_email")
            d_name = dc2.text_input("Full name *", key="adu_name")
            dc3, dc4 = st.columns(2)
            d_role = dc3.selectbox(
                "Role", permissions.assignable_roles(user) or ["collaborator"],
                index=0, key="adu_role")
            d_dept = dc4.text_input("Department", key="adu_dept")
            d_program = st.text_input(
                "Program areas", help="e.g. 'Vaccines, MCH, Malaria'",
                key="adu_program")
            if _mt and _is_super:
                # "Individual" first, then existing orgs. Individual → a personal,
                # PUBLIC tenant for this user (not an organization).
                _tenant_choices = [_INDIVIDUAL_TENANT_LABEL] + list(_tenant_opts.keys())
                try:
                    d_tenant_name = st.selectbox(
                        "Assign to tenant", _tenant_choices,
                        index=None, accept_new_options=True, key="adu_tenant",
                        placeholder="Individual · an organization · or type a new org name…",
                        help="Where this user belongs. Pick **Individual** for a personal "
                             "account (its activity is visible to all), pick an existing "
                             "organization, or TYPE A NEW ORG NAME and press Enter to "
                             "create it on save. (Admins add users to their OWN tenant "
                             "automatically.)")
                except TypeError:
                    # Older Streamlit without accept_new_options → plain picker; a new
                    # org tenant can still be created from Settings → Accounts → Tenants.
                    d_tenant_name = st.selectbox(
                        "Assign to tenant",
                        _tenant_choices or ["(no tenants yet)"],
                        key="adu_tenant",
                        help="Where this user belongs (Individual = personal account, "
                             "visible to all).")
            bc1, bc2 = st.columns([1, 1])
            save = bc1.form_submit_button(
                "➕ Create user", type="primary", width='stretch')
            cancel = bc2.form_submit_button("Cancel", width='stretch')

        if cancel:
            st.rerun()

        if save:
            errs: list[str] = []
            if not d_email or "@" not in d_email:
                errs.append("Valid email is required.")
            if not d_name:
                errs.append("Full name is required.")
            if not errs:
                existing = sb.table("users").select("email") \
                    .eq("email", d_email.strip()).limit(1).execute().data or []
                if existing:
                    errs.append("A user with this email already exists.")
            if errs:
                st.error("Please fix:\n\n- " + "\n- ".join(errs))
                return

            try:
                temp = _gen_temp_password(12)
                _ins = sb.table("users").insert({
                    "email": d_email.strip(),
                    "name": d_name.strip(),
                    "role": d_role,
                    "department": (d_dept or "").strip() or None,
                    "program": (d_program or "").strip() or None,
                    "password_hash": hash_password(temp),
                    "must_change_password": True,
                    "password_changed_at": datetime.now(timezone.utc).isoformat(),
                    "is_active": True,
                }).execute()
                new_uid = (_ins.data or [{}])[0].get("id")
                clear_credentials_cache()
                # Multi-tenant: associate the new user with a tenant. An admin adds
                # users to ITS OWN active tenant automatically; a super_user picks an
                # existing tenant OR types a new one (created here). (No-op single-tenant.)
                if _mt and new_uid:
                    _tid = None
                    if _is_super and d_tenant_name == _INDIVIDUAL_TENANT_LABEL:
                        # Individual → a personal, PUBLIC ('individual' kind) tenant for
                        # THIS user. Name it after them; append the email if that name is
                        # already taken (tenants.name is unique).
                        _ind_name = (d_name.strip() or d_email.strip())
                        try:
                            _svc = service_client()
                            _dupe = (_svc.table("tenants").select("id")
                                     .ilike("name", _ind_name).limit(1).execute().data or [])
                            if _dupe:
                                _ind_name = f"{_ind_name} — {d_email.strip()}"
                            _created = (_svc.table("tenants").insert(
                                {"name": _ind_name, "kind": "individual",
                                 "status": "active",
                                 "created_by": user.get("id")}).execute().data or [])
                            _tid = _created[0]["id"] if _created else None
                            if _tid:
                                st.toast(f"Created individual account “{_ind_name}”.",
                                         icon="🧑")
                        except Exception as _cexc:
                            st.warning(f"Couldn't create the individual account "
                                       f"(did you run migration 078?): {_cexc}")
                    elif _is_super and d_tenant_name:
                        _nm = str(d_tenant_name).strip()
                        _tid = _tenant_opts.get(d_tenant_name)
                        if not _tid and _nm and _nm != "(no tenants yet)":
                            # A newly-typed organization → create it (idempotent by name),
                            # then assign the user to it.
                            try:
                                _svc = service_client()
                                _dupe = (_svc.table("tenants").select("id")
                                         .ilike("name", _nm).limit(1).execute().data or [])
                                if _dupe:
                                    _tid = _dupe[0]["id"]
                                else:
                                    _created = (_svc.table("tenants").insert(
                                        {"name": _nm, "status": "active",
                                         "created_by": user.get("id")}).execute().data or [])
                                    _tid = _created[0]["id"] if _created else None
                                    if _tid:
                                        st.toast(f"Created tenant “{_nm}”.", icon="🏢")
                            except Exception as _cexc:
                                st.warning(f"Couldn't create tenant “{_nm}”: {_cexc}")
                    else:
                        _tid = tc.current_tenant_id()
                    if _tid:
                        try:
                            service_client().table("tenant_memberships").insert({
                                "tenant_id": _tid, "user_id": new_uid, "role": d_role,
                                "status": "active",
                                "decided_at": datetime.now(timezone.utc).isoformat(),
                            }).execute()
                        except Exception as _mexc:
                            st.warning(f"User created, but tenant assignment failed: {_mexc}")
            except Exception as exc:
                st.error(f"Create failed: {exc}")
                return
            try:
                from core.user_emails import (
                    send_welcome_email, MailerNotConfigured)
                send_welcome_email(to_email=d_email.strip(),
                                   to_name=d_name.strip(), temp_password=temp)
                st.success(
                    f"✅ Created **{d_email}**. Temp password emailed "
                    f"directly — they'll be forced to change it on first "
                    f"login.")
            except MailerNotConfigured:
                st.warning(
                    "Account created, but email service is not configured "
                    "(RESEND_API_KEY / RESEND_FROM_EMAIL missing from env). "
                    "Temp password shown below for out-of-band delivery — "
                    "copy it now.")
                st.code(temp)
            except Exception as exc:
                st.warning(
                    f"Account created, but email send failed ({exc}). Temp "
                    f"password shown below — share out-of-band.")
                st.code(temp)

    # ─── Header row ─────────────────────────────────────────────────────
    _hcol_text, _hcol_btn = st.columns([5, 1])
    with _hcol_text:
        st.subheader("Manage users")
        st.caption(
            "Add new teammates, change roles, deactivate accounts, or issue "
            "a temporary password. **Super User** can manage admins; admins "
            "can manage reviewers + collaborators only.")
    with _hcol_btn:
        st.markdown("<div style='padding-top:1.6rem'></div>",
                    unsafe_allow_html=True)
        if st.button("➕ Add User", type="primary", width='stretch',
                     key="mu_add_user_top"):
            _add_user_dialog()

    # ─── Pending approvals banner ───────────────────────────────────────
    # The whole page needs the DB; if the first query can't reach Supabase
    # (transient httpx.ConnectError / network blip), degrade with a clear
    # message instead of crashing the entire app with a raw traceback.
    try:
        pending_signups = (
            sb.table("users").select("email,name,created_at")
            .eq("is_active", False).order("created_at", desc=True)
            .execute().data or [])
    except Exception as exc:
        st.error(
            "⚠ Couldn't reach the database (connection error). Check your "
            "network / Supabase status, then refresh this page.")
        st.caption(f"Details: {type(exc).__name__}")
        return
    try:
        pending_resets = (
            sb.table("password_reset_requests")
            .select("id,email,requested_at").eq("status", "pending")
            .order("requested_at", desc=True).execute().data or [])
    except Exception:
        pending_resets = []
        st.info(
            "💡 Run **migration 016** to enable self-service password reset "
            "requests (`db/migrations/016_super_user_and_password_resets."
            "sql`).")
    if pending_signups or pending_resets:
        with st.container(border=True):
            if pending_signups:
                st.warning(
                    f"📬 **{len(pending_signups)} pending sign-up approval(s)** "
                    f"— set role + flip Active in the editor below: "
                    + ", ".join(f"`{u['email']}`" for u in pending_signups[:5])
                    + (" …" if len(pending_signups) > 5 else ""))
            if pending_resets:
                st.info(
                    f"🔐 **{len(pending_resets)} password-reset request(s)** — "
                    f"pick the user below and click Reset password to issue a "
                    f"temp password: "
                    + ", ".join(f"`{r['email']}`" for r in pending_resets[:5])
                    + (" …" if len(pending_resets) > 5 else ""))

    # ─── Existing users table ───────────────────────────────────────────
    try:
        all_users = (
            sb.table("users")
            .select("id,email,name,role,department,job_title,phone,program,"
                    "is_active,last_login_at,password_changed_at,"
                    "must_change_password,created_at")
            .order("created_at").execute().data or [])
    except Exception as exc:
        st.error(
            "⚠ Couldn't reach the database (connection error). Check your "
            "network / Supabase status, then refresh this page.")
        st.caption(f"Details: {type(exc).__name__}")
        return

    # Blacklisted users (migration 077) are hard-blocked and managed under the
    # Blacklisted tab — hide them here so Manage Users only lists live accounts.
    all_users = [u for u in all_users if not u.get("is_blacklisted")]

    # Multi-tenant: a non-super admin manages only users in ITS OWN tenant; the
    # super_user sees everyone. (No-op in single-tenant mode / for super_user.)
    try:
        from auth import tenant_context as tc
        if tc.multitenant_enabled() and not permissions.is_super_user(user):
            _tid = tc.current_tenant_id()
            if _tid:
                _member_ids = {m.get("user_id") for m in
                               (service_client().table("tenant_memberships").select("user_id")
                                .eq("tenant_id", _tid).execute().data or [])}
                all_users = [u for u in all_users if u.get("id") in _member_ids]
    except Exception:
        pass

    # ── Per-user tenant names (multi-tenant) — powers the Tenant column + filter ──
    from auth import tenant_context as _tc
    try:
        _mt = _tc.multitenant_enabled()
    except Exception:
        _mt = False
    _is_super = permissions.is_super_user(user)
    _user_tenants: dict = {}
    if _mt:
        try:
            from collections import defaultdict as _dd
            _mrows = (service_client().table("tenant_memberships")
                      .select("user_id, status, tenants(name)").execute().data or [])
            _tmp = _dd(list)
            for _m in _mrows:
                if _m.get("status") in ("active", "pending"):
                    _tn = (_m.get("tenants") or {}).get("name")
                    if _tn:
                        _tmp[_m.get("user_id")].append(
                            _tn if _m.get("status") == "active" else f"{_tn} (pending)")
            _user_tenants = {uid: ", ".join(sorted(set(v))) for uid, v in _tmp.items()}
        except Exception:
            _user_tenants = {}

    # ── Filters — find a user fast (name/email/dept search + role/status/tenant) ──
    _total = len(all_users)
    _roles = sorted({(u.get("role") or "").strip() for u in all_users if u.get("role")})
    _widths = [3, 1.2, 1.2] + ([1.6] if (_mt and _is_super) else [])
    _fc = st.columns(_widths)
    _q = _fc[0].text_input(
        "Search users", key="mu_q", label_visibility="collapsed",
        placeholder="🔍 Search name, email, department…")
    _role_f = _fc[1].selectbox("Role filter", ["All roles"] + _roles, key="mu_role_f",
                               label_visibility="collapsed")
    _status_f = _fc[2].selectbox("Status filter", ["All statuses", "Active", "Inactive"],
                                 key="mu_status_f", label_visibility="collapsed")
    _tenant_f = "All tenants"
    if _mt and _is_super:
        _tvals = sorted({v for v in _user_tenants.values() if v})
        _tenant_f = _fc[3].selectbox("Tenant filter", ["All tenants"] + _tvals,
                                     key="mu_tenant_f", label_visibility="collapsed")

    def _match(u: dict) -> bool:
        if _q and _q.strip():
            _blob = " ".join(str(u.get(k) or "") for k in
                             ("email", "name", "department", "program", "job_title")).lower()
            if _q.strip().lower() not in _blob:
                return False
        if _role_f != "All roles" and (u.get("role") or "") != _role_f:
            return False
        if _status_f == "Active" and not u.get("is_active"):
            return False
        if _status_f == "Inactive" and u.get("is_active"):
            return False
        if (_mt and _is_super and _tenant_f != "All tenants"
                and _user_tenants.get(u.get("id"), "") != _tenant_f):
            return False
        return True

    all_users = [u for u in all_users if _match(u)]

    if not all_users:
        st.info("No users match your search." if _total
                else "No users in the database yet.")
        return

    df_u = pd.DataFrame(all_users)
    disp = df_u.copy()
    for col in ("last_login_at", "password_changed_at", "created_at"):
        disp[col] = disp[col].fillna("").astype(str).str[:10]
    disp["Force PW reset"] = disp["must_change_password"] \
        .fillna(False).map(lambda v: "⚠ Yes" if bool(v) else "—")
    disp["Status"] = disp["is_active"].map(
        lambda v: "🟢 Active" if v else "⏸ Inactive")
    if _mt:
        disp["Tenant"] = df_u["id"].map(lambda uid: _user_tenants.get(uid) or "—")
    disp_cols = (["email", "name", "role"] + (["Tenant"] if _mt else [])
                 + ["Status", "department", "program", "last_login_at",
                    "password_changed_at", "Force PW reset"])
    sel = st.dataframe(
        disp[disp_cols], width='stretch', hide_index=True,
        selection_mode="single-row", on_select="rerun", key="mu_table_sel",
        column_config={
            "last_login_at": "Last login",
            "password_changed_at": "Password set",
        })
    st.caption(f"Showing {len(all_users)} of {_total} user(s).")

    tgt: dict | None = None
    picked_rows = (getattr(sel, "selection", None) or {}).get("rows") or []
    if picked_rows:
        picked_idx = picked_rows[0]
        if 0 <= picked_idx < len(disp):
            picked_email = disp.iloc[picked_idx]["email"]
            tgt = next((u for u in all_users
                        if u.get("email") == picked_email), None)

    if not tgt:
        st.caption(
            "👆 Click a row to select a user, then use **Edit**, **Reset "
            "password**, or **Delete**.")
        return

    is_self = tgt.get("email") == user.get("email")
    can_manage = permissions.can_manage_user(user, tgt)
    target_email = tgt.get("email")

    if is_self:
        st.warning(
            f"🔒 Selected **your own** account (`{target_email}`). You can "
            f"edit your profile + access overrides; role, active, reset, and "
            f"delete are locked.")
    elif can_manage:
        st.success(
            f"✓ Selected **{target_email}** (role: `{tgt.get('role')}`). "
            f"Full edit + reset + delete available.")
    else:
        st.error(
            f"🚫 Selected **{target_email}** — outside your management scope. "
            f"No actions available.")

    # ─── Edit modal ─────────────────────────────────────────────────────
    @st.dialog(f"Edit user — {target_email}", width="large")
    def _edit_dialog(_tgt=tgt, _is_self=is_self, _can_manage=can_manage,
                     _target_email=target_email):
        role_editable = _can_manage and not _is_self
        profile_editable = _can_manage or _is_self
        assignable = permissions.assignable_roles(user)
        current_role = _tgt.get("role") or "collaborator"
        role_options = list(dict.fromkeys(assignable + [current_role]))

        with st.form("edit_user_modal_form"):
            f1, f2 = st.columns(2)
            e_name = f1.text_input(
                "Full name", value=_tgt.get("name") or "",
                disabled=not profile_editable)
            e_email = f2.text_input(
                "Email (login username)", value=_tgt.get("email") or "",
                disabled=not profile_editable,
                help="⚠ Changing this changes the user's LOGIN username on "
                     "next session.")
            f3, f4 = st.columns(2)
            e_phone = f3.text_input(
                "Phone", value=_tgt.get("phone") or "",
                disabled=not profile_editable)
            e_title = f4.text_input(
                "Job title", value=_tgt.get("job_title") or "",
                disabled=not profile_editable)
            f5, f6 = st.columns(2)
            e_dept = f5.text_input(
                "Department", value=_tgt.get("department") or "",
                disabled=not profile_editable)
            e_program = f6.text_input(
                "Program areas", value=_tgt.get("program") or "",
                disabled=not profile_editable,
                help="Free-text, comma-separated.")
            st.markdown("**Access**")
            f7, f8 = st.columns(2)
            e_role = f7.selectbox(
                "Role", role_options,
                index=role_options.index(current_role),
                disabled=not role_editable)
            e_active = f8.checkbox(
                "Account active", value=bool(_tgt.get("is_active")),
                disabled=not role_editable)

            st.markdown("**Per-surface access overrides**")
            st.caption(
                "Default = role policy. Pick anything else to override that "
                "surface for THIS user only.")
            current_overrides = _tgt.get("access_overrides") or {}
            if not isinstance(current_overrides, dict):
                current_overrides = {}
            rg_target = permissions.role_group(_tgt)
            new_overrides: dict[str, str] = {}
            for surface, role_caps in permissions.ACCESS_MATRIX.items():
                default_cap = role_caps.get(rg_target, "hidden")
                current_choice = current_overrides.get(
                    surface, "Use role default")
                if current_choice not in permissions.OVERRIDE_OPTIONS:
                    current_choice = "Use role default"
                oc1, oc2 = st.columns([3, 2])
                oc1.markdown(
                    f"`{surface}` · default: "
                    f"_{permissions.capability_label(default_cap)}_")
                pick = oc2.selectbox(
                    "ov", permissions.OVERRIDE_OPTIONS,
                    index=permissions.OVERRIDE_OPTIONS.index(current_choice),
                    key=f"mu_ov_{surface}", label_visibility="collapsed",
                    disabled=not role_editable)
                if pick != "Use role default":
                    new_overrides[surface] = pick

            bc1, bc2 = st.columns([1, 1])
            save = bc1.form_submit_button(
                "💾 Save changes", type="primary", width='stretch',
                disabled=not profile_editable)
            cancel = bc2.form_submit_button("Cancel", width='stretch')

        if cancel:
            st.rerun()

        if save and profile_editable:
            errs: list[str] = []
            new_email_clean = (e_email or "").strip()
            if not new_email_clean or "@" not in new_email_clean:
                errs.append("Email must be a valid address.")
            if new_email_clean.lower() != _target_email.lower():
                collision = [u for u in all_users
                             if (u.get("email") or "").lower()
                             == new_email_clean.lower()]
                if collision:
                    errs.append("Another user already has that email.")
            if errs:
                st.error("Please fix:\n\n- " + "\n- ".join(errs))
                return

            payload: dict = {
                "name":       (e_name or "").strip() or None,
                "email":      new_email_clean,
                "phone":      (e_phone or "").strip() or None,
                "job_title":  (e_title or "").strip() or None,
                "department": (e_dept or "").strip() or None,
                "program":    (e_program or "").strip() or None,
            }
            was_inactive = not bool(_tgt.get("is_active"))
            will_be_active = role_editable and bool(e_active)
            approval_email_needed = was_inactive and will_be_active
            if role_editable:
                payload["role"] = e_role
                payload["is_active"] = bool(e_active)
                payload["access_overrides"] = new_overrides
            try:
                sb.table("users").update(payload) \
                    .eq("email", _target_email).execute()
                saved_overrides = "access_overrides" in payload
            except Exception as exc:
                err_str = str(exc).lower()
                missing_overrides = ("access_overrides" in err_str
                                     or "pgrst204" in err_str)
                if missing_overrides and "access_overrides" in payload:
                    payload.pop("access_overrides", None)
                    try:
                        sb.table("users").update(payload) \
                            .eq("email", _target_email).execute()
                        st.warning(
                            "Profile + role saved, but **access overrides "
                            "were not persisted** — `users.access_overrides` "
                            "column is missing. Apply **migration 017** "
                            "(`db/migrations/017_users_access_overrides.sql`) "
                            "to enable per-user overrides.")
                        saved_overrides = False
                    except Exception as exc2:
                        st.error(f"Save failed: {exc2}")
                        return
                else:
                    st.error(f"Save failed: {exc}")
                    return

            clear_credentials_cache()
            if _is_self:
                user["name"] = payload.get("name") or user.get("name")
                user["email"] = new_email_clean
                st.session_state["app_user"] = user
            if approval_email_needed:
                try:
                    from core.user_emails import send_account_approved_email
                    send_account_approved_email(
                        to_email=new_email_clean,
                        to_name=payload.get("name") or "")
                except Exception:
                    pass
            if saved_overrides or "access_overrides" not in \
                    permissions.ACCESS_MATRIX:
                st.toast(f"✅ Updated {_target_email}", icon="✅")
            st.rerun()

    # ─── Reset Password modal ───────────────────────────────────────────
    @st.dialog(f"Reset password — {target_email}", width="medium")
    def _reset_dialog(_target_email=target_email, _tgt=tgt):
        st.warning(
            f"This will generate a 12-character temporary password for "
            f"**{_target_email}**, flip their `must_change_password` flag, "
            f"and **email them the temp password directly**. They'll be "
            f"forced to pick a new one on next login.")
        rc1, rc2 = st.columns([1, 1])
        confirm = rc1.button("🔄 Reset + email", type="primary",
                             width='stretch', key="reset_confirm_btn")
        if rc2.button("Cancel", width='stretch', key="reset_cancel_btn"):
            st.rerun()
        if confirm:
            try:
                temp = _gen_temp_password(12)
                sb.table("users").update({
                    "password_hash": hash_password(temp),
                    "must_change_password": True,
                    "password_changed_at": datetime.now(timezone.utc).isoformat(),
                }).eq("email", _target_email).execute()
                try:
                    sb.table("password_reset_requests").update({
                        "status": "handled",
                        "handled_by": user.get("email"),
                        "handled_at": datetime.now(timezone.utc).isoformat(),
                    }).eq("email", _target_email).eq("status", "pending").execute()
                except Exception:
                    pass
                clear_credentials_cache()
            except Exception as exc:
                st.error(f"Reset failed: {exc}")
                return
            try:
                from core.user_emails import (
                    send_password_reset_email, MailerNotConfigured)
                send_password_reset_email(
                    to_email=_target_email, to_name=_tgt.get("name"),
                    temp_password=temp)
                st.success(
                    f"✅ New temp password emailed to **{_target_email}**. "
                    f"They'll be forced to set their own password on next "
                    f"login.")
            except MailerNotConfigured:
                st.warning(
                    "Email service not configured — temp password shown "
                    "below for out-of-band delivery only this once.")
                st.code(temp)
            except Exception as exc:
                st.warning(
                    f"Reset succeeded but email failed ({exc}). Temp password "
                    f"shown below — share out-of-band.")
                st.code(temp)

    # ─── Delete modal ───────────────────────────────────────────────────
    @st.dialog(f"Delete user — {target_email}", width="medium")
    def _delete_dialog(_target_email=target_email):
        st.error(
            f"⚠ This **permanently deletes** the account `{_target_email}`. "
            f"Their meeting notes, engagements, and submitted RFPs remain in "
            f"the DB (foreign keys are soft) but they can no longer log in. "
            f"This action cannot be undone.")
        st.caption(
            "Prefer **deactivating** (Edit → uncheck Account active) if you "
            "might restore the user later — deactivation is reversible.")
        typed = st.text_input(
            f"Type the email exactly to confirm: `{_target_email}`",
            key="del_confirm_text")
        dc1, dc2 = st.columns([1, 1])
        # The button is intentionally NOT gated with `disabled=`. A text_input only
        # commits its value on blur/Enter, and a disabled button swallows the very
        # click that would blur (commit) the field — so typing the email then clicking
        # never enabled it. Keep the button live and validate the match ON CLICK: the
        # click blurs the field, so `typed` holds the committed value at this point.
        confirm = dc1.button(
            "🗑 Permanently delete", type="primary", width='stretch',
            key="del_confirm_btn")
        if dc2.button("Cancel", width='stretch', key="del_cancel_btn"):
            st.rerun()
        if confirm:
            if typed.strip().lower() != (_target_email or "").strip().lower():
                st.warning("Type the email exactly as shown to confirm the deletion.")
            else:
                try:
                    sb.table("users").delete().eq("email", _target_email).execute()
                    clear_credentials_cache()
                    st.toast(f"🗑 Deleted {_target_email}", icon="🗑️")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")

    # ─── Blacklist modal (migration 077 — hard block) ───────────────────
    @st.dialog(f"Blacklist user — {target_email}", width="medium")
    def _blacklist_dialog(_target_email=target_email):
        st.error(
            f"🚫 Blacklisting **{_target_email}** blocks them from logging in "
            f"immediately — a hard block, stronger than deactivating. They also "
            f"can't sign up again while blacklisted. Reversible any time from the "
            f"**Blacklisted** tab.")
        reason = st.text_input("Reason (optional)", key="bl_reason_user")
        bc1, bc2 = st.columns([1, 1])
        if bc1.button("🚫 Blacklist", type="primary", width='stretch',
                      key="bl_confirm_user"):
            try:
                sb.table("users").update({
                    "is_blacklisted": True,
                    "blacklisted_at": datetime.now(timezone.utc).isoformat(),
                    "blacklisted_by": user.get("email"),
                    "blacklist_reason": (reason or "").strip() or None,
                }).eq("email", _target_email).execute()
            except Exception as exc:
                st.error(f"Blacklist failed (did you run migration 077?): {exc}")
                return
            clear_credentials_cache()
            st.toast(f"🚫 Blacklisted {_target_email}", icon="🚫")
            st.rerun()
        if bc2.button("Cancel", width='stretch', key="bl_cancel_user"):
            st.rerun()

    # ─── Action buttons row ─────────────────────────────────────────────
    ab1, ab2, ab3, ab4, _spacer = st.columns([1, 1.4, 1, 1.2, 3.4])
    if ab1.button("✏️ Edit", width='stretch', key="mu_btn_edit"):
        _edit_dialog()
    if ab2.button("🔄 Reset password", width='stretch',
                  key="mu_btn_reset", disabled=not can_manage,
                  help=None if can_manage
                  else "You can't reset this user's password."):
        _reset_dialog()
    if ab3.button("🗑 Delete", width='stretch', key="mu_btn_delete",
                  disabled=is_self or not can_manage,
                  help="You can't delete yourself." if is_self
                  else (None if can_manage else "You can't delete this user.")):
        # Clear any confirmation text left over from a previous open so the
        # field always starts empty (and the button starts disabled).
        st.session_state.pop("del_confirm_text", None)
        _delete_dialog()
    if ab4.button("🚫 Blacklist", width='stretch', key="mu_btn_blacklist",
                  disabled=is_self or not can_manage,
                  help=("You can't blacklist yourself." if is_self else
                        (None if can_manage else
                         "Outside your management scope."))):
        st.session_state.pop("bl_reason_user", None)
        _blacklist_dialog()

    return tgt
