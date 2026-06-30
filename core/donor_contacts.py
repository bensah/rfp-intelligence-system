"""Programmatic writer for the donor_contacts table (migration 022).

Calls carry contacts (e.g. UNGM notice Contacts tab → candidate['_contacts']); this
pushes them to the donor's one-to-many contact list so the donor CRM grows with every
scan. Links on donor_intel.canonical_key (resolved from the call's funder via
donor_intel.match_donor). Dedups on email (then name) so re-scans don't pile up
duplicates, and never overwrites a UI-added contact. Best-effort: never raises into a
scan.

Privacy note (mirrors migration 022): contacts here come from the OFFICIAL published
call/donor page (first-party), not mass-compiled — same standard as the manual list.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _key_for_funder(funder: Any) -> str | None:
    """donor_intel.canonical_key for a call's funder — via donor_enrich.ensure_donor, so a
    named, on-theme funder not yet in the matrix is conservatively auto-created (a stub) and
    the contacts then have a donor to attach to. None for a generic / uncreatable funder."""
    try:
        from core.donor_enrich import ensure_donor
        return ensure_donor(funder)
    except Exception:
        return None


def upsert_contacts(canonical_key: str, contacts: list[dict]) -> int:
    """Insert NEW contacts for a donor (dedup on lower(email), else lower(name)).
    Returns the count inserted. Best-effort — returns 0 on any error."""
    if not canonical_key or not contacts:
        return 0
    try:
        from db.supabase_client import get_client, safe_execute
        sb = get_client()
        existing = (safe_execute(
            sb.table("donor_contacts").select("email,contact_name")
            .eq("canonical_key", canonical_key)) or [])
        have_email = {(e.get("email") or "").strip().lower() for e in existing if e.get("email")}
        have_name = {(e.get("contact_name") or "").strip().lower() for e in existing if e.get("contact_name")}
        rows = []
        for c in contacts:
            email = (c.get("email") or "").strip()
            name = (c.get("name") or "").strip()
            el, nl = email.lower(), name.lower()
            if email and el in have_email:
                continue
            if not email and name and nl in have_name:
                continue
            if not (email or name):
                continue
            rows.append({
                "canonical_key": canonical_key,
                "contact_name": name or None,
                "role_title": c.get("role") or None,
                "email": email or None,
                "phone": c.get("phone") or None,
                "is_official": True,
                "notes": "Auto-extracted from a call notice.",
            })
            if email:
                have_email.add(el)
            if name:
                have_name.add(nl)
        if rows:
            sb.table("donor_contacts").insert(rows).execute()
        return len(rows)
    except Exception as exc:
        log.debug("donor_contacts upsert failed for %s: %s", canonical_key, exc)
        return 0


def push_from_candidate(candidate: dict) -> int:
    """Push a candidate's extracted ['_contacts'] to its donor's contact list.
    No-op (0) when the call has no contacts or the donor isn't in the intel matrix."""
    contacts = candidate.get("_contacts") or []
    if not contacts:
        return 0
    key = _key_for_funder(candidate.get("funding_agency"))
    if not key:
        return 0
    return upsert_contacts(key, contacts)
