"""Access matrix + role helpers.

Single source of truth for "who can do what". Imported by:
  * `app_pages/user.py` — to render the "My Access" read-only matrix
    and to gate the "Manage Users" tab to admin only.
  * `app_pages/admin.py` — already gates the whole page to admin via
    `user.role != "admin"`. The CSS nav-link hide is injected from
    `core/app_header.py` based on `is_admin()`.
  * Future: any page that needs fine-grained per-action gating.

Why a centralised matrix
------------------------
Before this module, role checks were sprinkled throughout pages as
ad-hoc `if user["role"] != "admin"` blocks. That meant the "My Access"
display on the User page had to duplicate that logic to stay in sync,
which always drifts. Now every check reads from `ACCESS_MATRIX` so the
matrix view and the actual gate share one definition.

Role model
----------
The DB has four role values: super_user / admin / reviewer / collaborator.
Hierarchy:
    super_user  — can do everything; only role that can manage other admins
    admin       — full app access; can manage reviewers + collaborators
                  only (cannot touch other admins or the super_user)
    reviewer    — same access as collaborator (kept distinct only because
                  the original schema split them; future use TBD)
    collaborator — regular user access

For access-matrix purposes we collapse reviewer + collaborator into a
single "user" column since their access is identical; super_user is
treated as admin in the matrix (admin = "can edit everything in this
app"), with the super-vs-admin distinction enforced only by the
`can_manage_user()` helper for user-administration operations.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Access matrix
# ---------------------------------------------------------------------------
# Each row is one access surface (page or page+tab). Values are the
# capability string for each role group:
#   "edit"      — full read + write
#   "view"      — read only
#   "view+add"  — read + create new, but cannot edit / delete existing
#   "trigger"   — read + invoke (e.g. Manual Scan: read history + start run)
#   "self"      — can edit OWN row only (My Profile / Change Password)
#   "all"       — admin-only "manage everyone" capability
#   "hidden"    — not visible in nav / tab list at all
#
# Display order in the User → My Access tab follows the dict insertion
# order, so group related pages together for readability.
ACCESS_MATRIX: dict[str, dict[str, str]] = {
    # ── Top-level pages ─────────────────────────────────────────────────
    "Home":                       {"admin": "edit",    "user": "edit"},
    "Pipelines":                  {"admin": "edit",    "user": "edit"},
    "Grants":                     {"admin": "edit",    "user": "edit"},
    "Actions":                    {"admin": "edit",    "user": "edit"},
    "Report":                     {"admin": "edit",    "user": "view"},
    "Donors":                     {"admin": "edit",    "user": "view"},
    "Profile → My Profile":       {"admin": "self",    "user": "self"},
    "Profile → Change Password":  {"admin": "self",    "user": "self"},

    # ── Settings page (top-right menu; admin / super_user only) ────────
    # Note: "Manage Users" capability differs WITHIN the admin column —
    # super_user can manage admins + other super_users; admin can
    # manage reviewers + collaborators only. The matrix column shows
    # "manage" for both since both see the tab; can_manage_user() is
    # the per-action gate.
    "Settings → Manage Users":    {"admin": "all",     "user": "hidden"},
    "Settings → User Access":     {"admin": "view",    "user": "hidden"},
    "Settings → Setup":           {"admin": "edit",    "user": "hidden"},
    "Settings → Records":         {"admin": "edit",    "user": "hidden"},
    "Settings → Sources":         {"admin": "edit",    "user": "view+add"},
    "Settings → Manual Scan":     {"admin": "trigger", "user": "hidden"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_super_user(user: dict[str, Any] | None) -> bool:
    """True for the super_user role only. Used to gate the most
    sensitive ops (managing other admins, role assignments above
    collaborator)."""
    return bool(user and user.get("role") == "super_user")


def is_admin(user: dict[str, Any] | None) -> bool:
    """True for admin OR super_user — the "can use the Admin panel"
    test. Super_user is admin + more, so it always answers yes here.
    For super-only checks use `is_super_user()` explicitly."""
    return bool(user and user.get("role") in ("admin", "super_user"))


def role_group(user: dict[str, Any] | None) -> str:
    """Map raw DB role → access-matrix column key. Super_user + admin
    collapse to 'admin'; reviewer + collaborator collapse to 'user'."""
    return "admin" if is_admin(user) else "user"


# ---------------------------------------------------------------------------
# Developer-vs-client tenant gates (cross-tenant "developer tasks")
# ---------------------------------------------------------------------------
# A SECOND authorization axis layered on top of role: some surfaces touch the
# SHARED, platform-wide resources (donor mapping, the Sources catalog + Blocked
# tokens, Run Extraction, Records → Verify/Reset, Learning data). Those are
# DEVELOPER tasks — restricted to members of a DEVELOPER / SYSTEM tenant
# (is_developer=true; e.g. RFPIS Inc, Taadom Digital PLC), no matter how
# privileged a CLIENT tenant's own admins/super_user are. A client tenant is
# confined to its OWN tenant-scoped data + settings.
#
# The tenant side of the check lives in auth.tenant_context (it needs the
# service client + session tenant). We import it lazily so permissions.py stays
# import-light and usable outside a Streamlit/tenant context (scripts, tests):
# a failure there means "no tenant context" → fall back to role-only gating,
# which is exactly the single-tenant behaviour (nothing is locked out).
def _active_tenant_is_developer() -> bool:
    try:
        from auth.tenant_context import active_tenant_is_developer
        return active_tenant_is_developer()
    except Exception:
        return True


def is_developer_super(user: dict[str, Any] | None) -> bool:
    """Super_user whose HOME tenant is a developer/system tenant. Gate for the
    most sensitive cross-tenant developer tasks: donor mapping + Sources catalog
    EDIT, Records → Verify/Reset."""
    return is_super_user(user) and _active_tenant_is_developer()


def is_developer_admin(user: dict[str, Any] | None) -> bool:
    """Admin OR super_user in a developer/system tenant. Gate for developer tasks
    an admin may also perform: Run Extraction, Sources → Blocked tokens."""
    return is_admin(user) and _active_tenant_is_developer()


def is_developer_member(user: dict[str, Any] | None) -> bool:
    """Any authenticated member of a developer/system tenant (read access to the
    Learning-data view)."""
    return bool(user) and _active_tenant_is_developer()


# Friendly, audience-facing labels for the raw DB role values. "collaborator"
# (the default for most teammates) reads as "Contributor" in the UI. Shown in
# the sidebar identity block + the Help page.
ROLE_LABELS: dict[str, str] = {
    "collaborator": "Contributor",
    "reviewer": "Reviewer",
    "admin": "Admin",
    "super_user": "Super User",
}


def role_label(user: dict[str, Any] | None) -> str:
    """Human-friendly label for the user's role (e.g. 'Contributor')."""
    raw = (user or {}).get("role")
    return ROLE_LABELS.get(raw, (raw or "—").title())


# ---------------------------------------------------------------------------
# User-administration policy (who can edit whom)
# ---------------------------------------------------------------------------
# Separate from the page/tab access matrix above because this is a
# RELATIONAL check (actor × target) rather than just (actor × surface).
#
# Rules (2026-07-26: admin elevated to full user management within its scope):
#   super_user → can manage anyone, including other super_users and itself.
#                Reserved for the platform owner / developer.
#   admin      → can manage admins, reviewers and collaborators (create, edit,
#                delete, assign roles up to ADMIN). CANNOT touch the super_user
#                (that role stays platform-owner only), and cannot demote itself
#                (self-lockout guard). In multi-tenant mode an admin only ever
#                sees/manages users in its OWN tenant (scoped in the UI + RLS).
#   others     → cannot manage anyone.
#
# Returning True authorises role + is_active + reset-password + delete actions.
# Editing one's OWN profile fields (name / phone / etc.) is governed by
# the My Profile tab, not this helper.
_MANAGEABLE_BY: dict[str, set[str]] = {
    "super_user": {"super_user", "admin", "reviewer", "collaborator"},
    "admin":      {"admin", "reviewer", "collaborator"},
}


def can_manage_user(actor: dict[str, Any] | None,
                     target: dict[str, Any] | None) -> bool:
    """True when `actor` is allowed to change `target`'s role / active
    flag / password. False for self (super_user is the only role that
    can elevate / demote itself, via a separate explicit override on
    the page)."""
    if not actor or not target:
        return False
    actor_role = actor.get("role") or ""
    target_role = target.get("role") or ""
    allowed_targets = _MANAGEABLE_BY.get(actor_role, set())
    if target_role not in allowed_targets:
        return False
    # Self-lockout guard for admins (super_user is the only role that
    # can intentionally demote itself, and the UI requires an extra
    # confirmation for that path).
    if actor_role == "admin" and actor.get("email") == target.get("email"):
        return False
    return True


def assignable_roles(actor: dict[str, Any] | None) -> list[str]:
    """Which roles can `actor` assign to others?
       super_user → any of the four
       admin      → collaborator + reviewer + admin (NOT super_user)
       others     → []
    Ordered from least to most privileged for display."""
    if is_super_user(actor):
        return ["collaborator", "reviewer", "admin", "super_user"]
    if is_admin(actor):
        return ["collaborator", "reviewer", "admin"]
    return []


def access(user: dict[str, Any] | None, surface: str) -> str:
    """Lookup the capability string for `surface` for this user.

    Resolution order:
      1. `user.access_overrides[surface]` if present (per-user override
         set by admin/super in Manage Users → Access overrides).
      2. `ACCESS_MATRIX[surface][role_group(user)]` — the role default.
      3. 'hidden' — fail closed if the surface isn't in the matrix.

    Per-user overrides let admin/super grant extra access to specific
    individuals (e.g. give one collaborator edit access to Reports)
    or revoke access (e.g. hide Pipeline from a particular reviewer).
    """
    if user:
        overrides = user.get("access_overrides") or {}
        if isinstance(overrides, dict) and surface in overrides:
            cap = overrides.get(surface)
            if cap:
                return cap
    row = ACCESS_MATRIX.get(surface)
    if not row:
        return "hidden"
    return row.get(role_group(user), "hidden")


# Capability values the UI lets an admin pick when setting an
# override. Includes the "Use default" sentinel as the first option;
# saving with that selected removes the override for that surface.
OVERRIDE_OPTIONS = [
    "Use role default",
    "edit",
    "view",
    "view+add",
    "trigger",
    "hidden",
]


def can_view(user: dict[str, Any] | None, surface: str) -> bool:
    """True when the user can see `surface` at all (anything except
    hidden)."""
    return access(user, surface) != "hidden"


def can_edit(user: dict[str, Any] | None, surface: str) -> bool:
    """True when the user can mutate state in `surface`."""
    return access(user, surface) in {"edit", "all", "self", "trigger",
                                       "view+add"}


def capability_label(cap: str) -> str:
    """Human-readable label for the matrix display in My Access."""
    return {
        "edit":     "✓ View & edit",
        "view":     "👁 View only",
        "view+add": "👁 View + add (no edit)",
        "trigger":  "✓ View & trigger",
        "self":     "✓ Edit own only",
        "all":      "✓ Manage all users",
        "hidden":   "🚫 No access",
    }.get(cap, cap)
