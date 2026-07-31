"""Phase B — the propose→review→apply queue for the SHARED central resources.

`donor_intel` + `donor_sources` are developer-Super-only to EDIT (Phase A / migration 079).
This module lets any NON-developer PROPOSE a field-level change that a developer-tenant
Super User APPROVES → auto-applies, or REJECTS.

Enforcement (matches the project posture — app-layer primary, RLS defense-in-depth):
  * proposer path  → `get_client()` (tenant-scoped; RLS restricts to own pending rows).
  * developer path → `service_client()` (RLS-bypassing), HARD-gated in Python on
    `permissions.is_developer_super`. A non-developer has no code path AND no SQL path to
    approve/apply/reject — even their own suggestion.

Fail-safe: if migration 080 hasn't run, every read returns empty/0 and writes raise a
friendly error — Phase-A read-only editing is unaffected.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from core import permissions
from db.supabase_client import get_client, service_client

TABLE = "resource_suggestions"

# donor_sources editable business columns (the add/edit dialogs' field set — admin.py).
_DS_EDITABLE = {"donor_name", "donor_code", "base_url", "rfp_listing_url",
                "scrape_method", "source_class", "access_model", "notes", "is_active"}

# Never proposable/appliable regardless of resource (system/audit columns).
_NEVER = {"id", "created_at", "updated_at", "created_by", "source_uid",
          "host", "donor_intel_id", "field_provenance"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_missing_table(exc: Exception) -> bool:
    s = str(exc).lower()
    return ("resource_suggestions" in s and (
        "does not exist" in s or "not exist" in s or "undefined table" in s
        or "could not find the table" in s or "42p01" in s or "pgrst205" in s))


def _resolve_uid(user: dict | None) -> Optional[str]:
    """The user's uuid — from the app_user dict, else looked up by email (mirror of
    auth.tenant_context._resolve_user_id, kept local to avoid a private import)."""
    if not user:
        return None
    uid = user.get("id")
    if uid:
        return str(uid)
    email = user.get("email")
    if not email:
        return None
    try:
        rows = (service_client().table("users").select("id")
                .eq("email", email).limit(1).execute().data or [])
        return str(rows[0]["id"]) if rows else None
    except Exception:
        return None


def _current_tenant_id() -> Optional[str]:
    try:
        from auth.tenant_context import current_tenant_id
        return current_tenant_id()
    except Exception:
        return None


def can_suggest(user: dict | None) -> bool:
    """A logged-in NON-developer (client-tenant member, or a single-tenant non-super
    admin) proposes; a developer-Super edits the row directly and never files a
    suggestion."""
    return bool(user) and not permissions.is_developer_super(user)


# ---------------------------------------------------------------------------
# Proposer path (get_client — tenant-scoped)
# ---------------------------------------------------------------------------
def _norm(v: Any) -> Any:
    """Normalize for equality: treat None/''/NaN alike; strip strings; bools as-is."""
    if v is None:
        return None
    if isinstance(v, float) and v != v:        # NaN
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return v


def diff_of(payload: dict, base: dict, *, allowed: set[str] | None = None) -> tuple[dict, dict]:
    """Return (proposed_diff, base_snapshot): only the keys whose normalized value differs
    from `base`. `allowed`, when given, restricts to real/editable columns."""
    diff, snap = {}, {}
    for k, v in (payload or {}).items():
        if k in _NEVER:
            continue
        if allowed is not None and k not in allowed:
            continue
        if _norm(v) != _norm((base or {}).get(k)):
            diff[k] = v
            snap[k] = (base or {}).get(k)
    return diff, snap


def create_suggestion(*, resource_type: str, target_id: str | None,
                      proposed_diff: dict, base_snapshot: dict | None,
                      rationale: str | None, target_label: str | None,
                      user: dict) -> dict:
    """File a PENDING proposal for the caller's own tenant. Proposer path → get_client()."""
    if resource_type not in ("donor_intel", "donor_sources"):
        raise ValueError(f"unknown resource_type {resource_type!r}")
    if not can_suggest(user):
        raise PermissionError("Only a non-developer may file a suggestion.")
    if not proposed_diff:
        raise ValueError("Nothing changed — there is nothing to suggest.")
    uid = _resolve_uid(user)
    if not uid:
        raise PermissionError("Could not resolve your account id.")
    row = {
        "resource_type": resource_type,
        "target_id": str(target_id) if target_id is not None else None,
        "target_label": (target_label or None),
        "proposed_diff": proposed_diff,
        "base_snapshot": (base_snapshot or {}),
        "proposer_user_id": uid,
        "proposer_email": user.get("email"),
        "tenant_id": _current_tenant_id(),      # None in single-tenant mode
        "rationale": (rationale or None),
        "status": "pending",
    }
    try:
        resp = get_client().table(TABLE).insert(row).execute()
    except Exception as exc:
        if _is_missing_table(exc):
            raise RuntimeError("Suggestions aren't available yet — run migration 080.") from exc
        raise
    data = getattr(resp, "data", None) or []
    return data[0] if data else row


def list_mine(user: dict, *, resource_type: str | None = None,
              status: str | None = None) -> list[dict]:
    """The caller's own suggestions (proposer path → get_client, RLS-scoped + explicit
    proposer filter)."""
    uid = _resolve_uid(user)
    if not uid:
        return []
    try:
        q = (get_client().table(TABLE).select("*")
             .eq("proposer_user_id", uid).order("created_at", desc=True))
        if resource_type:
            q = q.eq("resource_type", resource_type)
        if status:
            q = q.eq("status", status)
        return q.execute().data or []
    except Exception as exc:
        if _is_missing_table(exc):
            return []
        raise


def withdraw(suggestion_id: str, user: dict) -> dict:
    """Proposer retracts their own still-pending suggestion (proposer path)."""
    uid = _resolve_uid(user)
    if not uid:
        raise PermissionError("Could not resolve your account id.")
    try:
        resp = (get_client().table(TABLE)
                .update({"status": "withdrawn"})
                .eq("id", suggestion_id).eq("proposer_user_id", uid)
                .eq("status", "pending").execute())
    except Exception as exc:
        if _is_missing_table(exc):
            raise RuntimeError("Suggestions aren't available yet — run migration 080.") from exc
        raise
    data = getattr(resp, "data", None) or []
    if not data:
        raise ValueError("Nothing withdrawn — the suggestion is not yours or no longer pending.")
    return data[0]


# ---------------------------------------------------------------------------
# Developer path (service_client — RLS-bypassing; HARD-gated on is_developer_super)
# ---------------------------------------------------------------------------
def _require_dev_super(user: dict | None) -> None:
    if not permissions.is_developer_super(user):
        raise PermissionError("Only a developer-tenant Super User may review suggestions.")


def list_pending(user: dict, *, resource_type: str | None = None) -> list[dict]:
    """ALL pending suggestions across ALL tenants (review inbox). Returns [] for a
    non-developer — never leaks cross-tenant."""
    if not permissions.is_developer_super(user):
        return []
    try:
        q = (service_client().table(TABLE).select("*")
             .eq("status", "pending").order("created_at", desc=True))
        if resource_type:
            q = q.eq("resource_type", resource_type)
        return q.execute().data or []
    except Exception as exc:
        if _is_missing_table(exc):
            return []
        raise


def pending_count(user: dict) -> int:
    """Count of pending suggestions (tab badge). 0 for a non-developer."""
    if not permissions.is_developer_super(user):
        return 0
    try:
        resp = (service_client().table(TABLE).select("id", count="exact")
                .eq("status", "pending").execute())
        return int(getattr(resp, "count", None) or 0)
    except Exception as exc:
        if _is_missing_table(exc):
            return 0
        raise


def _donor_intel_columns(svc) -> set[str]:
    """Live donor_intel column set (same 'filter to real columns' defense as donors.py)."""
    try:
        rows = svc.table("donor_intel").select("*").limit(1).execute().data or []
        if rows:
            return set(rows[0].keys()) | {"canonical_key"}
    except Exception:
        pass
    return {"canonical_key"}


def _allowed_for(resource_type: str, svc) -> set[str]:
    if resource_type == "donor_sources":
        return set(_DS_EDITABLE)
    return _donor_intel_columns(svc) - _NEVER


def _slug_key(name: str, svc) -> str:
    """Slug a de-duplicated canonical_key from a donor name (mirror donors.py:1621-1625)."""
    base = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_") or "donor"
    try:
        existing = {r.get("canonical_key") for r in
                    (svc.table("donor_intel").select("canonical_key").execute().data or [])}
    except Exception:
        existing = set()
    key, i = base, 2
    while key in existing:
        key, i = f"{base}_{i}", i + 1
    return key


def get(suggestion_id: str, user: dict) -> dict | None:
    """Fetch one suggestion (developer path)."""
    _require_dev_super(user)
    try:
        rows = (service_client().table(TABLE).select("*")
                .eq("id", suggestion_id).limit(1).execute().data or [])
        return rows[0] if rows else None
    except Exception as exc:
        if _is_missing_table(exc):
            return None
        raise


def _live_target(rtype: str, target_id: str | None, svc) -> dict | None:
    """The CURRENT target row (or None), used to show the reviewer the REAL row a proposal
    will write — a proposer-typed target_label must never stand in for it."""
    if not target_id:
        return None
    key_col = "canonical_key" if rtype == "donor_intel" else "id"
    try:
        rows = (svc.table(rtype).select("*").eq(key_col, target_id)
                .limit(1).execute().data or [])
        return rows[0] if rows else None
    except Exception:
        return None


def resolve_target(sug: dict, user: dict) -> dict:
    """Re-resolve a suggestion's REAL target for the review UI (developer path). Returns
    {is_add, exists, real_label, target_id, mismatch}. `mismatch` is True when the
    proposer-typed target_label diverges from the live row's actual name — a spoof signal."""
    _require_dev_super(user)
    rtype = sug.get("resource_type")
    tid = sug.get("target_id")
    if not tid:
        return {"is_add": True, "exists": False, "real_label": None,
                "target_id": None, "mismatch": False}
    row = _live_target(rtype, tid, service_client())
    if row is None:
        return {"is_add": False, "exists": False, "real_label": None,
                "target_id": tid, "mismatch": False}
    real = (row.get("donor") if rtype == "donor_intel" else row.get("donor_name")) or tid
    typed = (sug.get("target_label") or "").strip().lower()
    mismatch = bool(typed) and typed != str(real).strip().lower()
    return {"is_add": False, "exists": True, "real_label": real,
            "target_id": tid, "mismatch": mismatch}


def approve(suggestion_id: str, user: dict) -> dict:
    """Approve AND auto-apply the proposed diff onto the target row (one developer action).

    Developer path → service_client() throughout. Uses an OPTIMISTIC CLAIM (flip
    pending→applied guarded on status='pending' BEFORE the resource write, verify one row
    claimed) so a double-click / two reviewers can't apply twice; reverts to pending if the
    resource write fails. For an EDIT the target is re-resolved live and must EXIST (never
    resurrects a deleted row via upsert). Returns {status, target_id, stale_fields,
    invalid_fields}."""
    _require_dev_super(user)
    svc = service_client()
    try:
        rows = svc.table(TABLE).select("*").eq("id", suggestion_id).limit(1).execute().data or []
    except Exception as exc:
        if _is_missing_table(exc):
            raise RuntimeError("Suggestions aren't available yet — run migration 080.") from exc
        raise
    if not rows:
        raise ValueError("Suggestion not found.")
    sug = rows[0]
    if sug.get("status") != "pending":
        raise ValueError(f"Already {sug.get('status')} — cannot approve again.")

    rtype = sug["resource_type"]
    diff = dict(sug.get("proposed_diff") or {})
    base = dict(sug.get("base_snapshot") or {})
    allowed = _allowed_for(rtype, svc)
    invalid_fields = sorted(k for k in diff if k not in allowed)
    diff = {k: v for k, v in diff.items() if k in allowed}
    if not diff:
        raise ValueError("Nothing to apply — no valid fields in the proposal.")
    target_id = sug.get("target_id")
    key_col = "canonical_key" if rtype == "donor_intel" else "id"

    # An EDIT must target a row that still exists — re-resolve against the LIVE row (its
    # identity, not the proposer-typed label, is authoritative) and compute staleness from
    # THAT, never from the proposer-supplied base_snapshot alone.
    cur0: dict = {}
    if target_id:
        cur = (svc.table(rtype).select("*").eq(key_col, target_id)
               .limit(1).execute().data or [])
        if not cur:
            raise ValueError(
                "Target no longer exists — the record was deleted or renamed. Reject this "
                "suggestion, or re-file it as an add.")
        cur0 = cur[0]
    stale_fields = [k for k in diff
                    if k in base and _norm(cur0.get(k)) != _norm(base.get(k))]

    uid = _resolve_uid(user)
    # ── ATOMIC CLAIM: flip pending→applied FIRST, guarded on status still being pending.
    # If zero rows come back, another approve won the race → abort before any write.
    claim = (svc.table(TABLE).update({
        "status": "applied", "reviewer_user_id": uid,
        "reviewer_email": user.get("email"),
        "decided_at": _now(), "applied_at": _now(),
    }).eq("id", suggestion_id).eq("status", "pending").execute())
    if not (getattr(claim, "data", None) or []):
        raise ValueError("Already being reviewed — nothing applied.")

    # ── Now perform the resource write. On failure, REVERT the claim to pending.
    try:
        if rtype == "donor_intel":
            if target_id:                                    # edit: UPDATE only (no resurrect)
                svc.table("donor_intel").update(diff).eq(key_col, target_id).execute()
            else:                                            # add: insert a fresh keyed row
                name = diff.get("donor") or sug.get("target_label") or "donor"
                target_id = _slug_key(name, svc)
                svc.table("donor_intel").insert({**diff, key_col: target_id}).execute()
            try:
                from core import donor_intel as _di
                _di.clear_cache()
            except Exception:
                pass
        else:                                                # donor_sources
            if target_id:
                svc.table("donor_sources").update(diff).eq("id", target_id).execute()
            else:
                resp = svc.table("donor_sources").insert(
                    {**diff, "created_by": sug.get("proposer_email")}).execute()
                data = getattr(resp, "data", None) or []
                if data and data[0].get("id"):
                    target_id = str(data[0]["id"])
    except Exception:
        # roll the claim back so the proposal can be retried, then surface the error
        try:
            svc.table(TABLE).update({
                "status": "pending", "reviewer_user_id": None, "reviewer_email": None,
                "decided_at": None, "applied_at": None,
            }).eq("id", suggestion_id).execute()
        except Exception:
            pass
        raise

    # record WHERE it landed without mutating the immutable proposer target_id
    svc.table(TABLE).update({"applied_target_id": target_id}).eq("id", suggestion_id).execute()

    try:                                     # sources use st.cache_data in the UI
        import streamlit as st
        st.cache_data.clear()
    except Exception:
        pass

    return {"status": "applied", "target_id": target_id,
            "stale_fields": stale_fields, "invalid_fields": invalid_fields}


def reject(suggestion_id: str, user: dict, *, note: str | None = None) -> dict:
    """Reject a pending suggestion (developer path). No resource write."""
    _require_dev_super(user)
    svc = service_client()
    try:
        rows = (svc.table(TABLE).select("status")
                .eq("id", suggestion_id).limit(1).execute().data or [])
    except Exception as exc:
        if _is_missing_table(exc):
            raise RuntimeError("Suggestions aren't available yet — run migration 080.") from exc
        raise
    if not rows:
        raise ValueError("Suggestion not found.")
    if rows[0].get("status") != "pending":
        raise ValueError(f"Already {rows[0].get('status')} — cannot reject.")
    # Guard the transition on status too (belt-and-braces vs a concurrent approve).
    resp = svc.table(TABLE).update({
        "status": "rejected",
        "reviewer_user_id": _resolve_uid(user),
        "reviewer_email": user.get("email"),
        "review_note": (note or None),
        "decided_at": _now(),
    }).eq("id", suggestion_id).eq("status", "pending").execute()
    data = getattr(resp, "data", None) or []
    if not data:
        raise ValueError("Already decided — nothing rejected.")
    return data[0]
