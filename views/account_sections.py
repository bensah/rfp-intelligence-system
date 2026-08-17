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

import difflib
import os
import re
import secrets
import string
import time
import unicodedata
from datetime import datetime, timezone

import bcrypt
import pandas as pd
import streamlit as st

from auth.authenticator import hash_password, clear_credentials_cache
from core import permissions
from core.geographies import COUNTRIES
from db.supabase_client import service_client

# App-owner / Super User contact for account & data requests (permanent deletion,
# reactivating a suspended org). Single source of truth (also surfaced on the Help page);
# set ADMIN_CONTACT_EMAIL in the environment to override the placeholder.
ADMIN_CONTACT_EMAIL = os.environ.get("ADMIN_CONTACT_EMAIL", "admin@example.org")

# Sentinel option in the "Assign to tenant" picker: creates a personal ('individual'
# kind) tenant for the user instead of an organization. Individual tenants are PUBLIC —
# their activity is visible to all users (migration 078 + db.supabase_client scoping).
# Shown wherever a tenant is picked. The long "— personal account (visible to
# all)" suffix was a mouthful in a dropdown beside real tenant names; the Tenant
# type question now carries that explanation (owner 2026-08-10).
_INDIVIDUAL_TENANT_LABEL = "🧑 Individual"
# Sentinel first option — "create a new one" rather than an existing tenant.
_NEW_TENANT_LABEL = "➕ Create new…"


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


def _forget_declared_areas() -> None:
    """Drop the cached "areas our users declared" set after a user record changes.

    MUST-2 reads those declarations through a 60-second cache, so without this an admin
    would add a colleague, look at a call, and see the old strategic-fit verdict — with no
    way to tell whether the declaration had taken effect or simply had no bearing.
    """
    try:
        from core.user_program_areas import clear_cache
        clear_cache()
    except Exception:
        pass
    try:                                  # the profile carries the derived key
        from core.org_profile import _clear_profile_cache
        _clear_profile_cache()
    except Exception:
        pass


def _program_areas_field(label, current, key, *, container=None, help="",
                         disabled=False) -> str | None:
    """The programme-areas control, and the string that goes in `users.program`.

    These three fields (my profile, add user, edit user) were free text, which put
    hand-typed vocabulary next to the graded taxonomy the rest of the app matches on: a
    colleague could type "TD" or "malaria control" and nothing downstream could line it up
    with a call's themes. Same list everywhere now, which is what lets a declaration count
    as evidence of expertise in MUST-2 (see core.user_program_areas).

    Returns a comma-joined string of CANONICAL keys, because `users.program` is a text
    column and is read as free text by the Report and the user table. Canonical keys
    survive `program_area_classifier.expand()` unchanged, so no reader needs to know the
    values are now controlled.
    """
    from core.program_area_select import program_area_multiselect
    picked = program_area_multiselect(label, current, key, container=container,
                                      help=help, disabled=disabled)
    return ", ".join(picked) or None


def _gen_temp_password(length: int = 12) -> str:
    """URL-safe random string (letters + digits).

    No longer a password anybody is told. Both account creation and admin reset now email
    a one-time link, but the users row still needs a password_hash, so it gets one nobody
    knows: generated here, hashed, and discarded. That is what makes a new account
    unreachable until its activation link is used - there is no interim credential to
    intercept.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# How long a finished dialog stays on screen before dismissing itself. Long
# enough to read a two-line confirmation, short enough that it is quicker
# than reaching for the X.
_DIALOG_CLOSE_SECONDS = 4


def _auto_close_dialog(seconds: int = _DIALOG_CLOSE_SECONDS) -> None:
    """Dismiss the surrounding st.dialog once its result has been read.

    st.rerun() is what closes a dialog — the delete modal already relies on
    that. The delay is the entire point: rerunning immediately would wipe
    the confirmation before anyone could read it, while leaving the dialog
    up makes every successful action cost a second click on Cancel or X.
    The rerun also refreshes the page underneath, so the user table shows
    the change that was just made.

    Call this ONLY on a success path. Where a fallback has printed a
    one-time link on screen the dialog must stay open — closing it would
    destroy the only copy of a credential that cannot be reissued without
    invalidating it.
    """
    st.caption(f"Closing in {seconds} seconds…")
    time.sleep(seconds)
    st.rerun()


# ===========================================================================
# My Profile
# ===========================================================================
def render_my_profile(user: dict, sb) -> None:
    me = _fetch_self(sb, user["email"])
    # Persistent success banner: set just before st.rerun() on a successful save so
    # the confirmation survives the rerun (a plain st.success would be wiped by it,
    # and st.toast fades in a few seconds — easy to miss).
    _flash = st.session_state.pop("pf_profile_flash", None)
    if _flash:
        st.success(_flash)
    st.subheader("Update your profile")
    st.caption(
        "Edit your own contact info, including email. **Role is "
        "read-only** — only an admin can change it. Changing your "
        "email also changes your login username on next session."
    )

    st.text_input("Role (read-only)", value=me.get("role") or "collaborator",
                  disabled=True, key="pf_role")

    # Outside the form, like the other programme-area pickers — a multiselect inside
    # st.form swallows the click on the submit button (see the Add-user dialog).
    new_program = _program_areas_field(
        "Program areas", me.get("program"), "me_program",
        help="Pick from the shared programme-area list. What you record here is treated "
             "as evidence of your organisation's expertise in MUST-2, and the Report uses "
             "it to attribute scans by programme focus.")

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
                service_client().table("users").select("email")
                .ilike("email", new_email_clean).limit(1)
                .execute().data or []
            )
            if existing:
                errs.append("Another user already has that email.")

        if errs:
            st.error("Please fix:\n\n- " + "\n- ".join(errs))
        else:
            try:
                # Self-edit of the user's OWN row. Use the service client (RLS-bypassing) so
                # the write is reliable — the tenant-scoped `authenticated` client can READ
                # users (the form populates) but its UPDATE on users is silently blocked
                # (0 rows, no error) = "looks saved but nothing changed". Scoped to THIS user
                # by the email filter, so no escalation. Verify a row actually came back.
                resp = (service_client().table("users").update({
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
                }).eq("email", user["email"]).execute())
                if not getattr(resp, "data", None):
                    st.error("Save didn't update your record (no matching row). "
                             "Please retry, or contact an admin if it persists.")
                else:
                    clear_credentials_cache()
                    _forget_declared_areas()
                    user["name"] = (new_name or "").strip() or user.get("name")
                    user["email"] = new_email_clean
                    st.session_state["app_user"] = user
                    email_changed = (
                        new_email_clean.lower() != (me.get("email") or "").lower())
                    _msg = ("✅ Profile saved successfully."
                            + (f" Your login email is now **{new_email_clean}** — "
                               "use it to sign in next session."
                               if email_changed else ""))
                    st.session_state["pf_profile_flash"] = _msg
                    st.toast(_msg, icon="✅")
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
                            from auth import tenant_context as _tc_inv
                            _tc_inv.clear_membership_cache(_uid)
                        except Exception:
                            pass
                    service_client().table("users").delete().eq("email", _email).execute()
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
                # Service client (RLS-bypassing) + verify: the tenant-scoped `authenticated`
                # client's UPDATE on users is silently blocked, which would let a password
                # "change" no-op while the OLD password still works — a real security trap.
                resp = (service_client().table("users").update({
                    "password_hash": hash_password(new_pw),
                    "password_changed_at": datetime.now(timezone.utc).isoformat(),
                    "must_change_password": False,
                }).eq("email", user["email"]).execute())
                if not getattr(resp, "data", None):
                    st.error("Password change didn't take — no matching record. "
                             "Please retry, or contact an admin.")
                else:
                    clear_credentials_cache()
                    st.toast("✅ Password changed.", icon="✅")
                    st.success("✅ Password changed successfully. Future logins "
                               "will use your new password.")
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


def _set_many_developer(svc, tids: list[str], on: bool) -> None:
    """Flag/unflag the given tenants as DEVELOPER/system tenants (migration 079). Members
    of a developer tenant may perform cross-tenant developer tasks (donor mapping, Sources
    catalog/blocked tokens, Run Extraction, Records Verify/Reset, Learning data)."""
    if not tids:
        return
    try:
        svc.table("tenants").update({"is_developer": on}).in_("id", tids).execute()
    except Exception as exc:
        st.error(f"Developer-flag change failed (did you run migration 079?): {exc}")


def _norm_tenant_name(s: str | None) -> str:
    """Normalize a tenant name for dedup: NFKC + casefold (folds full-width/compatibility/
    case variants so 'ＲＦＰ'/'Rfp'/'RFP' compare equal — the exact-dup block can't be
    bypassed by those unicode tricks), then non-alphanumeric runs → single space, trimmed.
    'RFP Intelligence  Inc.,' and 'rfp intelligence inc' compare equal. (Cross-script
    homoglyphs are a residual risk — the DB unique(name) + super approval are the backstop.)"""
    s = unicodedata.normalize("NFKC", (s or "")).casefold()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _tenant_dedup_matches(name: str, all_tenants: list[dict]) -> tuple[list[dict], list[dict]]:
    """(exact, similar) existing tenants for `name`. Exact = same normalized name (hard
    block). Similar = substring either way OR SequenceMatcher ratio ≥ 0.72 (flag, allow
    override). Pure-Python (difflib) — no deps, no LLM."""
    nn = _norm_tenant_name(name)
    if not nn:
        return [], []
    exact, similar = [], []
    for t in all_tenants:
        tn = _norm_tenant_name(t.get("name"))
        if not tn:
            continue
        if tn == nn:
            exact.append(t)
            continue
        if nn in tn or tn in nn or difflib.SequenceMatcher(None, nn, tn).ratio() >= 0.72:
            similar.append(t)
    return exact, similar


def _render_add_tenant(user: dict, svc, *, is_super: bool) -> None:
    """Add-a-tenant with a live typeahead + name deduplicator. As the user types, matching
    existing tenants surface so they can spot a duplicate; an exact (normalized) match is
    hard-blocked; a similar name is flagged but can be overridden ("genuinely different").
    A super_user's add goes live (active); an admin's add is a REQUEST (pending) that a
    super_user approves, and the admin is auto-added as its first member (R4/R5/R6)."""
    st.markdown("**➕ Add a tenant**" if is_super else "**➕ Request a new tenant**")
    c1, c2 = st.columns([3.4, 1.2])
    nm = c1.text_input(
        "New tenant name", key="add_tenant_name",
        placeholder="Start typing — matching existing tenants appear below…")
    _kind_label = c2.selectbox("Type", ["Organization", "Individual"], key="add_tenant_kind",
                               help="Organization = a normal org tenant. Individual = a "
                                    "personal account whose activity is visible to all users.")
    try:
        _all = (svc.table("tenants").select("id,name,slug,status")
                .order("name").execute().data or [])
    except Exception:
        _all = []
    exact, similar = _tenant_dedup_matches(nm, _all)

    # Typeahead: surface matching NAMES so the user can spot a duplicate (the box's whole
    # point) — but NEVER reveal a tenant's operational status to the caller, and never list
    # blacklisted tenants. Require ≥3 chars so single letters can't enumerate the platform.
    if len(nm.strip()) >= 3:
        _shown = [t for t in (exact + similar) if t.get("status") != "blacklisted"]
        if _shown:
            st.caption("Matching existing tenants — is yours already here?")
            for t in _shown[:8]:
                st.markdown(f"- **{t.get('name')}**")
        else:
            st.caption("No matching tenant — you can add it as new.")

    if exact:
        st.warning("A tenant with that name already exists — pick it instead of creating a "
                   "duplicate.")
        return
    if similar:
        st.info("⚠ Similar tenant name(s) exist above. Only add if yours is **genuinely "
                "different** — otherwise use the existing one.")

    _kind = "individual" if _kind_label == "Individual" else "organization"
    _label = (("➕ Add anyway (new)" if similar else "➕ Add tenant") if is_super else
              ("➕ Create anyway (new)" if similar else "➕ Create tenant"))
    if st.button(_label, type="primary", key="add_tenant_submit", disabled=not nm.strip()):
        from auth import tenant_context as _tc
        _status = "active" if is_super else "pending"
        _now = datetime.now(timezone.utc).isoformat()
        _payload = {"name": nm.strip(), "kind": _kind, "status": _status,
                    "slug": _tc.make_tenant_slug(nm.strip()),
                    "created_by": user.get("id"), "requested_by": user.get("id")}
        try:
            _created = svc.table("tenants").insert(_payload).execute().data or []
        except Exception:
            try:                                        # pre-migration fallback
                _created = (svc.table("tenants").insert(
                    {"name": nm.strip(), "status": _status,
                     "created_by": user.get("id")}).execute().data or [])
            except Exception as _exc:
                st.error(f"Create failed: {_exc}")
                return
        _tid = _created[0]["id"] if _created else None
        # Auto-add the creator as the first member — EXCEPT a super_user (who uses view-as).
        if _tid and not is_super:
            try:
                svc.table("tenant_memberships").insert({
                    "tenant_id": _tid, "user_id": user.get("id"),
                    "role": "admin", "status": "active", "decided_at": _now}).execute()
                _tc.clear_membership_cache(user.get("id"))
            except Exception:
                pass
        st.session_state.pop("add_tenant_name", None)
        if is_super:
            st.success(f"Created “{nm.strip()}”.")
        else:
            st.success(f"Tenant “{nm.strip()}” is being created, pending approval. You're "
                       "its first member and will get access once it's approved.")
        st.rerun()


def _render_pending_approvals(user: dict, svc) -> None:
    """Super_user approval queue for admin-requested (pending) tenants. Approve → active
    (+ audit); Reject → delete the request (it has no data yet; the creator membership
    cascades)."""
    try:
        pend = (svc.table("tenants")
                .select("id,name,slug,kind,created_at,requested_by")
                .eq("status", "pending").order("created_at").execute().data or [])
    except Exception:
        pend = []
    if not pend:
        return
    st.markdown(f"**🕓 {len(pend)} tenant request(s) awaiting your approval**")
    for t in pend:
        c1, c2, c3 = st.columns([4.5, 1.1, 1.1])
        _kind = "🧑 Individual" if t.get("kind") == "individual" else "🏢 Org"
        c1.markdown(f"**{t.get('name')}** · {_kind} · requested {(t.get('created_at') or '')[:10]}")
        if c2.button("✓ Approve", type="primary", key=f"tn_appr_{t['id']}"):
            try:
                # Guard on status='pending' so a raced/duplicate click can't re-approve.
                svc.table("tenants").update({
                    "status": "active", "approved_by": user.get("email"),
                    "approved_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", t["id"]).eq("status", "pending").execute()
                st.toast(f"Approved {t.get('name')}", icon="✓")
            except Exception as exc:
                st.error(f"Approve failed: {exc}")
            st.rerun()
        if c3.button("✗ Reject", key=f"tn_rej_{t['id']}",
                     help="Delete this request (no data yet)."):
            try:
                # Guard on status='pending' — NEVER delete a tenant that was approved in a
                # race (that would cascade-delete its members).
                svc.table("tenants").delete().eq("id", t["id"]) \
                   .eq("status", "pending").execute()
                st.toast(f"Rejected {t.get('name')}", icon="✗")
            except Exception as exc:
                st.error(f"Reject failed: {exc}")
            st.rerun()
    st.divider()


def render_manage_tenants(user: dict, sb, *, can_manage: bool | None = None) -> None:
    """Settings → Tenants. Tenants = organizations/individuals registered to the platform.

    `can_manage` (default = is_super_user): the SUPER USER gets the full management table
    (add / suspend / activate / blacklist / developer-toggle) with per-row view-as links.
    A non-super user gets a VIEW-ONLY list scoped to the tenants they belong to — no add
    form, no action buttons, no view-as link. Renaming is done in the Organization editor;
    deletion is intentionally omitted — suspend/blacklist instead, so data is never orphaned.

    Tenant + membership rows are read/written on the RLS-BYPASSING service client (these
    are privileged platform-admin operations; the passed-in `sb` would be the caller's
    tenant-scoped client and hit RLS on the tenant tables — the 42501 create failure)."""
    if can_manage is None:
        can_manage = permissions.is_super_user(user)
    svc = service_client()

    # Non-super users get a VIEW-ONLY list scoped to the tenants they belong to (R1/R2).
    _my_tids: set[str] | None = None
    if not can_manage:
        try:
            from auth.tenant_context import active_memberships
            _uid = user.get("id")
            if not _uid:
                from auth.tenant_context import _resolve_user_id
                _uid = _resolve_user_id(user)
            _my_tids = {m["tenant_id"] for m in (active_memberships(_uid) if _uid else [])}
            # + their OWN pending requests — active_memberships drops pending tenants, but
            # a requester should still see their request tracked in this list (R6).
            if _uid:
                try:
                    _req = (svc.table("tenants").select("id")
                            .eq("requested_by", _uid).eq("status", "pending")
                            .execute().data or [])
                    _my_tids |= {r["id"] for r in _req if r.get("id")}
                except Exception:
                    pass
        except Exception:
            _my_tids = set()

    st.subheader("Tenants")
    if can_manage:
        st.caption(
            "Tenants registered to the platform — **🏢 organizations** (isolated data) or "
            "**🧑 individuals** (personal accounts whose activity is visible to all users). "
            "Users belong to a tenant via membership. Suspend — don't delete — to retire a "
            "tenant without orphaning its records. **Click a tenant name to open its "
            "Organization page** (view / edit that tenant's identity + profile). Funding "
            "opportunities are shared platform-wide and screened against each tenant's own "
            "preferences.")
    else:
        st.caption("The tenants you belong to. View-only — adding, suspending and approving "
                   "tenants is handled by the Super User.")

    # ── Add / request a tenant. Any ADMIN may request (→ pending, a super_user approves
    # after due diligence); the super_user adds directly (→ active). Live typeahead + name
    # deduplicator are in _render_add_tenant. Collaborators (non-admin) get no add form. ──
    if permissions.is_admin(user):
        _render_add_tenant(user, svc, is_super=can_manage)
    if can_manage:
        _render_pending_approvals(user, svc)

    # ── Management table ─────────────────────────────────────────────────
    # Try selecting `is_developer` (migration 079) + `kind` (078); fall back column-set by
    # column-set so the table still loads before either migration is applied.
    tenants, _last_exc = None, None
    for _sel in ("id,name,slug,status,kind,is_developer,created_at,org_profile",
                 "id,name,slug,status,kind,created_at,org_profile",
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
    if can_manage:                      # pending live in the approval queue above, not here
        tenants = [t for t in tenants if (t.get("status") or "active") != "pending"]
    if _my_tids is not None:            # non-super: only the tenants they belong to (R2)
        tenants = [t for t in tenants if t.get("id") in _my_tids]
    if not tenants:
        st.info("You don't belong to any tenants yet." if not can_manage
                else "No tenants yet.")
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
            "Organization": (f"/organization?tenant={_key}&label={_name}"
                             if can_manage else _name),
            "Kind": "🧑 Individual" if t.get("kind") == "individual" else "🏢 Org",
            "Dev": "🛠 Dev" if t.get("is_developer") else "🙂 Regular",
            "_is_dev": bool(t.get("is_developer")),
            "_status": (t.get("status") or "active"),
            "Status": {"active": "🟢 Active", "suspended": "⏸ Suspended",
                       "pending": "🕓 Pending"}.get(t.get("status") or "active",
                                                     "⏸ Suspended"),
            "Members": int(active_ct.get(tid, 0)),
            "Pending": int(pending_ct.get(tid, 0)),
            "Profile": "set" if prof else "empty",
            "Created": (t.get("created_at") or "")[:10],
        })
    _df = pd.DataFrame(_rows)

    _org_col = (
        st.column_config.LinkColumn(
            "Organization", display_text=r"label=(.+)$", width="large",
            help="Click a tenant name to open its Organization page in a new tab. "
                 "That tab runs as a sticky super_user 'view-as' of the tenant — "
                 "scoped across every page and shown in the URL (?tenant=…) until "
                 "you Return to your account (banner up top).")
        if can_manage else
        st.column_config.TextColumn("Organization", width="large"))
    _sel = st.dataframe(
        _df[["Organization", "Kind", "Dev", "Status", "Members", "Pending", "Profile",
             "Created"]],
        hide_index=True, width="stretch", key="tenants_table",
        selection_mode="multi-row", on_select="rerun" if can_manage else "ignore",
        column_config={
            "Organization": _org_col,
            "Kind": st.column_config.TextColumn(
                "Kind", width="small",
                help="🏢 Org = a normal organization tenant. 🧑 Individual = a personal "
                     "account whose activity is visible to all users."),
            "Dev": st.column_config.TextColumn(
                "Dev", width="small",
                help="🛠 Dev = a DEVELOPER / system tenant: its "
                     "members may perform cross-tenant developer tasks (donor mapping, "
                     "Sources catalog & blocked tokens, Run Extraction, Records "
                     "Verify/Reset, Learning data). 🙂 Regular = a client tenant, "
                     "confined to its own data."),
            "Members": st.column_config.NumberColumn("Members", width="small"),
            "Pending": st.column_config.NumberColumn("Pending", width="small"),
        })

    if not can_manage:
        return                          # view-only — no selection, no action buttons

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

    # Developer-tenant toggle. A developer/system tenant unlocks the
    # cross-tenant developer tasks for its members; a client tenant stays confined to its
    # own data. Enable each action only when it would change something for the selection.
    _dev_states = {bool(r.get("_is_dev")) for r in _sel_rows}
    _can_mark_dev = any(d is False for d in _dev_states)
    _can_unmark_dev = any(d is True for d in _dev_states)
    d1, d2, _dsp = st.columns([1.6, 1.9, 4.5])
    if d1.button("🛠 Mark developer", width="stretch", key="tn_bulk_mark_dev",
                 disabled=not _can_mark_dev,
                 help="Grant developer-tenant status (cross-tenant developer tasks)."
                 if _can_mark_dev else "All selected tenants are already developer tenants."):
        _set_many_developer(svc, _sel_ids, True); st.rerun()
    if d2.button("Remove developer", width="stretch", key="tn_bulk_unmark_dev",
                 disabled=not _can_unmark_dev,
                 help="Revoke developer-tenant status."
                 if _can_unmark_dev else "None of the selected tenants are developer tenants."):
        _set_many_developer(svc, _sel_ids, False); st.rerun()


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


def render_blacklisted(user: dict, sb, *, can_manage: bool | None = None) -> None:
    """Settings → Accounts → Blacklisted. The register of hard-blocked users + tenants
    (migration 077) and the undo. `can_manage` (default = is_super_user): the SUPER USER
    sees the full platform-wide register with Remove-from-blacklist actions. A non-super
    user sees only a view-only note — the platform-wide list (with member emails + reasons)
    is never exposed to a non-super, and they can't be an active member of a blacklisted
    tenant anyway, so there is nothing tenant-scoped to show them."""
    if can_manage is None:
        can_manage = permissions.is_super_user(user)
    svc = service_client()

    st.subheader("Blacklisted")
    if not can_manage:
        st.caption("Hard-blocking (users can't sign in; a blacklisted tenant blocks all "
                   "member access) is managed by the Super User. You have no access to any "
                   "blacklisted tenant, so none appear here.")
        st.info("Nothing to show.")
        return
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
                _forget_declared_areas()
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
            "Creates the account and emails a one-time activation link. No password "
            "is generated or sent — the account cannot be used until the recipient "
            "follows the link and chooses one. The link works once and expires in "
            "7 days.")
        from auth import tenant_context as tc
        _mt = tc.multitenant_enabled()
        _is_super = permissions.is_super_user(user)
        d_tenant_name = None
        d_tenant_kind = None
        d_new_org = ""
        # Tenants split by KIND so the picker can be filtered (owner 2026-08-10): an
        # individual's personal account has no business appearing in a list beside the
        # organisation tenants — they answer different questions.
        _tenant_opts: dict[str, str] = {}
        _by_kind: dict[str, dict[str, str]] = {"individual": {}, "organization": {}}
        if _mt and _is_super:
            try:
                for t in (service_client().table("tenants").select("id,name,kind")
                          .eq("status", "active").order("name").execute().data or []):
                    _tenant_opts[t["name"]] = t["id"]
                    _k = ("individual" if str(t.get("kind") or "").strip().lower()
                          == "individual" else "organization")
                    _by_kind[_k][t["name"]] = t["id"]
            except Exception:
                _tenant_opts = {}

        # TENANT TYPE first, because the tenant list below is filtered by it. This dialog
        # used to wrap the rest in st.form, where a widget does not publish its value until
        # submit, so a radio in there could not refilter the list — it would catch up one
        # submit late. The form is gone now (see below) and every widget reruns the dialog,
        # so the ordering is all that matters.
        if _mt and _is_super:
            d_tenant_kind = st.radio(
                "Tenant type", ["Organization", "Individual"],
                horizontal=True, key="adu_kind",
                help="**Organization** — the user joins a team account. "
                     "**Individual** — a personal account, whose activity is visible "
                     "to all. This chooses which accounts the next question lists.")

        # NO st.form HERE, and Program areas keeps its place in the layout.
        #
        # A multiselect inside st.form swallows the click on the submit button - the first
        # click returns save=False and the dialog just sits there. This file already
        # recorded that failure from a selectbox with accept_new_options in this same form.
        # Hoisting the picker above the form dodged it but moved the field out of its row,
        # which is a worse trade: the fix should not rearrange the form to work.
        #
        # Plain widgets with an ordinary st.button have neither problem, and the pattern is
        # already used for the same reason in auth/authenticator.py's set-password screen.
        # The cost is that each widget reruns the dialog instead of batching until submit -
        # which the Tenant type radio above already does deliberately - and Enter no longer
        # submits.
        dc1, dc2 = st.columns(2)
        d_email = dc1.text_input(
            "Email *", help="Used as the login username.", key="adu_email")
        d_name = dc2.text_input("Full name *", key="adu_name")
        dc3, dc4 = st.columns(2)
        d_role = dc3.selectbox(
            "Role", permissions.assignable_roles(user) or ["collaborator"],
            index=0, key="adu_role")
        d_dept = dc4.text_input("Department", key="adu_dept")
        d_program = _program_areas_field(
            "Program areas", None, "adu_program",
            help="Pick from the shared programme-area list — the same vocabulary calls "
                 "and funders are classified with, so this person's areas can count as "
                 "evidence of expertise in MUST-2.")
        if _mt and _is_super:
            # The tenant list is FILTERED by the type chosen above (owner
            # 2026-08-10). Previously "Individual" sat in the SAME list as the
            # organizations, so a personal account appeared beside the tenant
            # organizations as though it were one of them.
            _is_ind = d_tenant_kind == "Individual"
            _pool = _by_kind["individual" if _is_ind else "organization"]
            # A PLAIN selectbox. `accept_new_options=True` was used here to let a
            # super user type a new org name, but inside a form it SWALLOWS THE
            # FIRST CLICK on the submit button — reproduced in a browser: click one
            # returns save=False and the dialog just sits there, click two works.
            # That is the "Create user does nothing" report. A new organization is
            # now named in its own text box, which has no such behaviour.
            _label = ("Assign to individual account" if _is_ind
                      else "Assign to organization")
            d_tenant_name = st.selectbox(
                _label, [_NEW_TENANT_LABEL] + list(_pool.keys()),
                index=0, key="adu_tenant",
                help="Only accounts of the selected tenant type are listed. "
                     "(Admins add users to their OWN tenant automatically.)")
            d_new_org = st.text_input(
                "New individual account name" if _is_ind
                else "New organization name",
                key="adu_new_org",
                placeholder=("Leave blank unless you picked “"
                             + _NEW_TENANT_LABEL + "”"),
                help="Used only when the picker above is set to “"
                     + _NEW_TENANT_LABEL + "”. For an individual account, leave "
                     "blank to name it after the user.")
        bc1, bc2 = st.columns([1, 1])
        save = bc1.button("➕ Create user", type="primary", width='stretch',
                          key="adu_save")
        cancel = bc2.button("Cancel", width='stretch', key="adu_cancel")

        if cancel:
            st.rerun()

        if save:
            errs: list[str] = []
            if not d_email or "@" not in d_email:
                errs.append("Valid email is required.")
            if not d_name:
                errs.append("Full name is required.")
            if not errs:
                # Guarded: this ran OUTSIDE the try below, so a transient read failure
                # escaped the handler entirely and the click looked like it had done
                # nothing at all.
                try:
                    existing = (sb.table("users").select("email")
                                .eq("email", d_email.strip()).limit(1)
                                .execute().data or [])
                except Exception as _dexc:
                    st.error(f"Couldn't check for an existing user: {_dexc}")
                    return
                if existing:
                    errs.append("A user with this email already exists.")
            if errs:
                st.error("Please fix:\n\n- " + "\n- ".join(errs))
                return

            try:
                # No password is chosen for the user, and none is emailed.
                # The row still needs a hash, so it gets one nobody knows -
                # 32 bytes of urandom, hashed and discarded. The account is
                # unreachable until the invite link is used, which is the
                # point: there is no interim credential to intercept.
                temp = _gen_temp_password(48)
                _ins = service_client().table("users").insert({
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
                _forget_declared_areas()
                # Multi-tenant: associate the new user with a tenant. An admin adds
                # users to ITS OWN active tenant automatically; a super_user picks an
                # existing tenant OR types a new one (created here). (No-op single-tenant.)
                if _mt and new_uid:
                    _tid = None
                    _want_new = (_is_super
                                 and str(d_tenant_name or "") == _NEW_TENANT_LABEL)
                    _ind = d_tenant_kind == "Individual"
                    if _want_new:
                        # Create the tenant of the chosen KIND. An individual account
                        # defaults to the user's own name; tenants.name is unique, so a
                        # clash is disambiguated with the email.
                        _nm = (str(d_new_org or "").strip()
                               or (d_name.strip() or d_email.strip() if _ind else ""))
                        if not _nm:
                            st.warning("Pick an organization, or type a name for the new "
                                       "one — the user was created without a tenant.")
                        else:
                            try:
                                _svc = service_client()
                                _dupe = (_svc.table("tenants").select("id")
                                         .ilike("name", _nm).limit(1).execute().data or [])
                                if _dupe and _ind:
                                    _nm = f"{_nm} — {d_email.strip()}"
                                    _dupe = []
                                if _dupe:
                                    _tid = _dupe[0]["id"]
                                else:
                                    _created = (_svc.table("tenants").insert(
                                        {"name": _nm,
                                         "kind": "individual" if _ind else "organization",
                                         "status": "active",
                                         "created_by": user.get("id")}).execute().data or [])
                                    _tid = _created[0]["id"] if _created else None
                                    if _tid:
                                        st.toast(f"Created {'individual account' if _ind else 'tenant'}"
                                                 f" “{_nm}”.", icon="🧑" if _ind else "🏢")
                            except Exception as _cexc:
                                st.warning(f"Couldn't create “{_nm}”: {_cexc}")
                    elif _is_super and d_tenant_name:
                        _tid = _tenant_opts.get(str(d_tenant_name).strip())
                    else:
                        _tid = tc.current_tenant_id()
                    if _tid:
                        try:
                            service_client().table("tenant_memberships").insert({
                                "tenant_id": _tid, "user_id": new_uid, "role": d_role,
                                "status": "active",
                                "decided_at": datetime.now(timezone.utc).isoformat(),
                            }).execute()
                            tc.clear_membership_cache(new_uid)
                        except Exception as _mexc:
                            st.warning(f"User created, but tenant assignment failed: {_mexc}")
            except Exception as exc:
                st.error(f"Create failed: {exc}")
                return
            # Minting the link and sending it are separate steps with
            # separate failures. If the link cannot be issued there is
            # nothing to fall back to; if only the send fails, the link is
            # in hand and can go out-of-band, so it must be built before
            # the send is attempted.
            try:
                from core.password_tokens import (
                    PURPOSE_INVITE, issue_token, build_link)
                from core.user_emails import _app_url
                _raw, _ = issue_token(
                    user_id=new_uid,
                    purpose=PURPOSE_INVITE,
                    created_by=user.get("email"),
                )
                _setup_link = build_link(_app_url(), _raw)
            except Exception as exc:
                st.error(
                    f"Account created, but no activation link could be issued "
                    f"({exc}). Use **Reset password** on the user to send "
                    f"one — the account cannot be logged into until then.")
                return
            try:
                from core.user_emails import (
                    send_welcome_email, MailerNotConfigured)
                send_welcome_email(
                    to_email=d_email.strip(),
                    to_name=d_name.strip(),
                    setup_link=_setup_link,
                )
                st.success(
                    f"✅ Created **{d_email}**. A one-time activation link has "
                    f"been emailed — it expires in 7 days, and they choose "
                    f"their own password.")
                _auto_close_dialog()
            except MailerNotConfigured:
                st.warning(
                    "Account created, but email service is not configured "
                    "(RESEND_API_KEY / RESEND_FROM_EMAIL missing from env). "
                    "Share the one-time activation link below out-of-band "
                    "(Signal / verbal) — it expires in 7 days.")
                st.code(_setup_link)
            except Exception as exc:
                st.warning(
                    f"Account created, but email send failed ({exc}). Share "
                    f"the one-time activation link below out-of-band — it expires "
                    f"in 7 days.")
                st.code(_setup_link)

    # ─── Header row ─────────────────────────────────────────────────────
    _hcol_text, _hcol_btn = st.columns([5, 1])
    with _hcol_text:
        st.subheader("Manage users")
        st.caption(
            "Add new teammates, change roles, deactivate accounts, or send "
            "a password-reset link. **Super User** can manage admins; admins "
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
                    f"pick the user below and click Reset password to email a "
                    f"one-time link: "
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
    for col in ("password_changed_at", "created_at"):
        disp[col] = disp[col].fillna("").astype(str).str[:10]
    # Last login shows date + time (to the minute), not just the date.
    disp["last_login_at"] = (disp["last_login_at"].fillna("").astype(str)
                             .str[:16].str.replace("T", " ", regex=False))
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
        # Per-surface access OVERRIDES are editable whenever the profile is (incl. self):
        # the self-lockout guard only protects role / active / reset / delete (so a user
        # can't demote or deactivate themselves), NOT the surface overrides — which is what
        # the self-edit banner already promises. Overrides can't grant super/developer-task
        # powers (those gates are role+tenant, not override-driven), so self-editing them is
        # safe and lets a super_user tune access from the UI, persisted to users.access_overrides.
        overrides_editable = profile_editable
        assignable = permissions.assignable_roles(user)
        current_role = _tgt.get("role") or "collaborator"
        role_options = list(dict.fromkeys(assignable + [current_role]))

        # Outside the form — see the Add-user dialog for why.
        e_program = _program_areas_field(
            "Program areas", _tgt.get("program"), f"edit_program_{_tgt.get('id')}",
            disabled=not profile_editable,
            help="Pick from the shared programme-area list.")

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
                # Cap the choices by the ACTOR's own capability on this surface — an admin
                # can't grant a user more than the admin holds (an existing higher grant is
                # preserved for display so it can be kept or lowered, never raised).
                _opts = permissions.assignable_override_options(user, surface, current_choice)
                oc1, oc2 = st.columns([3, 2])
                oc1.markdown(
                    f"`{surface}` · default: "
                    f"_{permissions.capability_label(default_cap)}_")
                pick = oc2.selectbox(
                    "ov", _opts, index=_opts.index(current_choice),
                    key=f"mu_ov_{surface}", label_visibility="collapsed",
                    disabled=not overrides_editable)
                if pick != permissions.USE_DEFAULT:
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
            # Persist overrides whenever they were editable — including a self-edit, where
            # role/active stay locked but the surface overrides are the user's to tune.
            if overrides_editable:
                # Server-side ceiling (defense in depth vs the filtered dropdown): never let
                # the actor grant a capability beyond their own on a surface.
                _bad = [s for s, cap in new_overrides.items()
                        if not permissions.can_assign_override(
                            user, s, cap, current_overrides.get(s))]
                if _bad:
                    st.error("You can't grant access beyond your own on: "
                             + ", ".join(f"`{s}`" for s in _bad))
                    return
                payload["access_overrides"] = new_overrides
            try:
                service_client().table("users").update(payload) \
                    .eq("email", _target_email).execute()
                saved_overrides = "access_overrides" in payload
            except Exception as exc:
                err_str = str(exc).lower()
                missing_overrides = ("access_overrides" in err_str
                                     or "pgrst204" in err_str)
                if missing_overrides and "access_overrides" in payload:
                    payload.pop("access_overrides", None)
                    try:
                        service_client().table("users").update(payload) \
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
            _forget_declared_areas()
            if _is_self:
                user["name"] = payload.get("name") or user.get("name")
                user["email"] = new_email_clean
                # Reflect a self-edit of the surface overrides in-session so it takes
                # effect on the next rerun (not only after re-login).
                if "access_overrides" in payload:
                    user["access_overrides"] = payload["access_overrides"]
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
            f"This emails **{_target_email}** a **one-time link** that lets "
            f"them choose a new password. The link expires in 2 hours and "
            f"works once. Their current password keeps working until they "
            f"use it, so this does not lock them out.")
        rc1, rc2 = st.columns([1, 1])
        confirm = rc1.button("🔄 Reset + email", type="primary",
                             width='stretch', key="reset_confirm_btn")
        if rc2.button("Cancel", width='stretch', key="reset_cancel_btn"):
            st.rerun()
        if confirm:
            try:
                # The existing password is deliberately left working until
                # the link is used. Overwriting it here locks the user out
                # the moment an admin clicks Reset - before the email has
                # even arrived - and if the mail bounces they are stranded
                # with no way in. The link, once used, replaces it.
                service_client().table("users").update({
                    "must_change_password": True,
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
                _forget_declared_areas()
            except Exception as exc:
                st.error(f"Reset failed: {exc}")
                return
            # As with the invite: build the link first, so a send failure
            # still leaves something to hand over out-of-band. There is no
            # temporary password to fall back on any more — the link is the
            # only credential this flow produces.
            try:
                from core.password_tokens import (
                    PURPOSE_RESET, issue_token, build_link)
                from core.user_emails import _app_url
                _raw, _ = issue_token(
                    user_id=_tgt.get("id"),
                    purpose=PURPOSE_RESET,
                    created_by=user.get("email"),
                )
                _reset_link = build_link(_app_url(), _raw)
            except Exception as exc:
                st.error(f"Could not issue a reset link: {exc}")
                return
            try:
                from core.user_emails import (
                    send_password_reset_email, MailerNotConfigured)
                send_password_reset_email(
                    to_email=_target_email, to_name=_tgt.get("name"),
                    reset_link=_reset_link)
                st.success(
                    f"✅ A one-time reset link has been emailed to "
                    f"**{_target_email}**. It expires in 2 hours. Their "
                    f"current password keeps working until they use it.")
                _auto_close_dialog()
            except MailerNotConfigured:
                st.warning(
                    "Email service not configured — share the one-time reset "
                    "link below out-of-band (Signal / verbal). It expires in "
                    "2 hours.")
                st.code(_reset_link)
            except Exception as exc:
                st.warning(
                    f"Reset started but email failed ({exc}). Share the "
                    f"one-time reset link below out-of-band — it expires in "
                    f"2 hours.")
                st.code(_reset_link)

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
                    service_client().table("users").delete().eq("email", _target_email).execute()
                    clear_credentials_cache()
                    _forget_declared_areas()
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
                service_client().table("users").update({
                    "is_blacklisted": True,
                    "blacklisted_at": datetime.now(timezone.utc).isoformat(),
                    "blacklisted_by": user.get("email"),
                    "blacklist_reason": (reason or "").strip() or None,
                }).eq("email", _target_email).execute()
            except Exception as exc:
                st.error(f"Blacklist failed (did you run migration 077?): {exc}")
                return
            clear_credentials_cache()
            _forget_declared_areas()
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
