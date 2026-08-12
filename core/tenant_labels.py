"""Tenant-personalised wording for UI copy.

"Entity" was our internal word for a tenant. It leaked into user-facing copy ("Strong fit for
this entity"), where it told the reader nothing — a tenant reading their own dashboard does not
think of themselves as an entity.

Naming the tenant is better than any generic noun, so copy carries a `{tenant}` placeholder and
this fills it in:

    fill("Strong fit for {tenant}.")   ->  "Strong fit for Country Team A."

There is one wrinkle worth stating. A tenant may be an ORGANISATION or a single INDIVIDUAL, which
is why "entity" was chosen in the first place. Naming the tenant sidesteps that entirely — a name
is accurate either way — so the generic fallbacks here are only reached when no name is
configured yet, and `settings` returns its "Your Organization" placeholder.
"""
from __future__ import annotations

from typing import Optional

from core import settings

# What `settings.get_org_name()` returns when a tenant has not set a display name. Substituting
# it verbatim gives "Strong fit for Your Organization", so it is treated as absent.
_PLACEHOLDERS = frozenset({"your organization", "your organisation", "org", ""})

# Used when the tenant has no name yet. Second person, because at that point the reader is
# almost certainly the person still setting the account up.
_FALLBACK = "your organization"


def tenant_name(tenant_id: Optional[str] = None) -> str:
    """The tenant's display name, or a generic second-person stand-in.

    Never raises: this is called from page copy, and a settings lookup failing must not take a
    page down over a caption.
    """
    try:
        name = (settings.get_org_name(tenant_id) or "").strip()
    except Exception:
        return _FALLBACK
    return _FALLBACK if name.lower() in _PLACEHOLDERS else name


def tenant_possessive(tenant_id: Optional[str] = None) -> str:
    """"Country Team A's" / "your organization's" — for "…{tenant_possessive} pipeline"."""
    name = tenant_name(tenant_id)
    if name == _FALLBACK:
        return f"{name}'s"
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def fill(text: str, tenant_id: Optional[str] = None) -> str:
    """Substitute `{tenant}` / `{tenant_possessive}` in UI copy.

    Plain `str.replace`, not `str.format`: copy contains literal braces and percent signs, and a
    caption is not worth a KeyError.
    """
    if not text:
        return text
    if "{tenant" not in text:
        return text
    return (text.replace("{tenant_possessive}", tenant_possessive(tenant_id))
                .replace("{tenant}", tenant_name(tenant_id)))
