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
                    "name":       (new_name or "").strip() or None,
                    "email":      new_email_clean,
                    "phone":      (new_phone or "").strip() or None,
                    "job_title":  (new_title or "").strip() or None,
                    "department": (new_dept or "").strip() or None,
                    "program":    (new_program or "").strip() or None,
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
def render_user_access(user: dict) -> None:
    st.subheader("User access")
    st.caption(
        "What each role can see and do across the app. Read-only "
        "reference — change a specific user's access on the Manage Users "
        "tab (Edit → per-surface overrides).")

    rg = permissions.role_group(user)
    rows = []
    for surface, role_caps in permissions.ACCESS_MATRIX.items():
        cap = role_caps.get(rg, "hidden")
        rows.append({
            "Surface": surface,
            "Your access": permissions.capability_label(cap),
            "Admin access": permissions.capability_label(
                role_caps.get("admin", "hidden")),
        })
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)


# ===========================================================================
# Manage Users (admin / super_user only)
# ===========================================================================
def render_manage_users(user: dict, sb) -> None:
    # ─── Add User modal ────────────────────────────────────────────────
    @st.dialog("Add a new user", width="large")
    def _add_user_dialog():
        st.caption(
            "Creates the account immediately with a 12-char temp password "
            "shown on save. Share the temp password out-of-band (Signal / "
            "verbal — never email).")
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
                sb.table("users").insert({
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
                clear_credentials_cache()
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
    df_u = pd.DataFrame(all_users)

    if df_u.empty:
        st.info("No users in the database yet.")
        return

    disp = df_u.copy()
    for col in ("last_login_at", "password_changed_at", "created_at"):
        disp[col] = disp[col].fillna("").astype(str).str[:10]
    disp["Force PW reset"] = disp["must_change_password"] \
        .fillna(False).map(lambda v: "⚠ Yes" if bool(v) else "—")
    disp["Active"] = disp["is_active"].map(
        lambda v: "✓" if v else "⏸ pending/inactive")
    disp_cols = ["email", "name", "role", "Active", "department", "program",
                 "last_login_at", "password_changed_at", "Force PW reset"]
    sel = st.dataframe(
        disp[disp_cols], width='stretch', hide_index=True,
        selection_mode="single-row", on_select="rerun", key="mu_table_sel",
        column_config={
            "last_login_at": "Last login",
            "password_changed_at": "Password set",
        })

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
        confirm = dc1.button(
            "🗑 Permanently delete", type="primary", width='stretch',
            disabled=(typed.strip().lower() != _target_email.lower()),
            key="del_confirm_btn")
        if dc2.button("Cancel", width='stretch', key="del_cancel_btn"):
            st.rerun()
        if confirm:
            try:
                sb.table("users").delete().eq("email", _target_email).execute()
                clear_credentials_cache()
                st.toast(f"🗑 Deleted {_target_email}", icon="🗑️")
                st.rerun()
            except Exception as exc:
                st.error(f"Delete failed: {exc}")

    # ─── Action buttons row ─────────────────────────────────────────────
    ab1, ab2, ab3, _spacer = st.columns([1, 1.4, 1, 4])
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
