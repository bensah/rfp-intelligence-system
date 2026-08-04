"""Cross-tenant analytics (read-only) for the super_user + a system-discovery counter.

Two audiences:
  * `system_discovery_stats()` — the SHARED discovery crawl (system-wide, tenant_id NULL
    in scan_logs) + the shared extracted-store size. Safe for ANY user to see (it's the
    common catalog everyone screens), surfaced as a small counter on the Report page.
  * `tenant_activity()` / `user_stats()` — app-wide, ACROSS tenants. SUPER_USER ONLY, for
    the Settings → Analytics dashboard.

All reads use the RLS-bypassing service client (a privileged platform-admin view), never
the tenant-scoped get_client(), so the numbers are genuinely cross-tenant. Best-effort:
every query is guarded so a missing column / table degrades gracefully to zeros.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _svc():
    from db.supabase_client import service_client
    return service_client()


def _count(table: str, **eq) -> int:
    """Exact row count for a table, optionally filtered by equality. 0 on any error.
    Selects "*" (not "id") because some shared tables — e.g. extracted_solicitations —
    have no `id` column (their PK is `uid`), which made the count silently return 0."""
    try:
        q = _svc().table(table).select("*", count="exact")
        for k, v in eq.items():
            q = q.eq(k, v)
        return q.limit(1).execute().count or 0
    except Exception as exc:
        log.debug("analytics count(%s) failed: %s", table, exc)
        return 0


def system_discovery_stats() -> dict[str, Any]:
    """System-wide discovery totals (visible to all). SYSTEM runs = scan_logs rows with
    no tenant_id (the Friday --extract-only crawl); TENANT-specific screening runs are
    excluded. Also the shared extracted-catalog size."""
    out = {"runs": 0, "found": 0, "rejected": 0, "catalog": 0, "last_run": None}
    # Filter to SYSTEM runs (tenant_id IS NULL) SERVER-side, and PAGINATE.
    # Previously this fetched `.limit(5000)` unfiltered and dropped tenant rows in Python.
    # Two problems: (1) PostgREST caps a response at 1000 rows, so `limit(5000)` silently
    # returned only the newest 1000 and the totals were UNDER-COUNTED; (2) it shipped every
    # tenant row across the wire just to discard it (measured 1.66s vs 0.35s filtered).
    # Paginating keeps the totals correct as scan_logs grows.
    rows: list[dict] = []
    _page, _start = 1000, 0
    while True:
        try:
            chunk = (_svc().table("scan_logs")
                     .select("scan_date,rfps_found,rfps_new,rfps_rejected,tenant_id,triggered_by")
                     .is_("tenant_id", "null")
                     .order("scan_date", desc=True)
                     .range(_start, _start + _page - 1).execute().data or [])
        except Exception:
            break
        rows.extend(chunk)
        if len(chunk) < _page:
            break
        _start += _page
        if _start >= 50_000:            # hard stop; never spin forever
            break
    seen_runs = set()
    for r in rows:
        if r.get("tenant_id"):
            continue                       # a tenant's own screening run — not "system"
        out["found"] += int(r.get("rfps_found") or 0)
        out["rejected"] += int(r.get("rfps_rejected") or 0)
        # scan_logs is one row per SOURCE per run; approximate distinct runs by scan_date.
        d = str(r.get("scan_date") or "")[:16]
        if d:
            seen_runs.add(d)
        if out["last_run"] is None and r.get("scan_date"):
            out["last_run"] = r.get("scan_date")
    out["runs"] = len(seen_runs)
    out["catalog"] = _count("extracted_solicitations")
    return out


def tenant_activity() -> list[dict[str, Any]]:
    """Per-tenant rollup (super_user): name, status, platform flag, active/pending members,
    rfp_submissions count, last screening run. Cross-tenant via the service client."""
    try:
        tenants = (_svc().table("tenants")
                   .select("id,name,status,is_platform,created_at")
                   .order("name").execute().data or [])
    except Exception:
        try:
            tenants = (_svc().table("tenants").select("id,name,status,created_at")
                       .order("name").execute().data or [])
        except Exception as exc:
            log.debug("analytics tenants failed: %s", exc)
            return []
    try:
        mems = (_svc().table("tenant_memberships").select("tenant_id,status")
                .execute().data or [])
    except Exception:
        mems = []
    from collections import Counter
    active_ct = Counter(m["tenant_id"] for m in mems if m.get("status") == "active")
    pending_ct = Counter(m["tenant_id"] for m in mems if m.get("status") == "pending")

    # rfp_submissions per tenant + last screening run (small #tenants → per-tenant query).
    try:
        from core.scan_pipeline import MATCH_RUN_LABEL
    except Exception:
        MATCH_RUN_LABEL = "🎯 Find my matches"
    out: list[dict[str, Any]] = []
    for t in tenants:
        tid = t.get("id")
        last_screen = None
        try:
            _sc = (_svc().table("scan_logs").select("scan_date")
                   .eq("tenant_id", tid).eq("source", MATCH_RUN_LABEL)
                   .order("scan_date", desc=True).limit(1).execute().data or [])
            last_screen = _sc[0]["scan_date"] if _sc else None
        except Exception:
            last_screen = None
        out.append({
            "id": tid,
            "name": t.get("name"),
            "status": t.get("status") or "active",
            "is_platform": bool(t.get("is_platform")),
            "members": int(active_ct.get(tid, 0)),
            "pending": int(pending_ct.get(tid, 0)),
            "rfps": _count("rfp_submissions", tenant_id=tid),
            "last_screen": last_screen,
            "created": (t.get("created_at") or "")[:10],
        })
    return out


def user_stats() -> dict[str, Any]:
    """App-wide user rollup (super_user): totals + by-role + active/inactive."""
    try:
        users = _svc().table("users").select("role,is_active").execute().data or []
    except Exception as exc:
        log.debug("analytics users failed: %s", exc)
        users = []
    from collections import Counter
    by_role = Counter((u.get("role") or "unknown") for u in users)
    active = sum(1 for u in users if u.get("is_active"))
    return {
        "total": len(users),
        "active": active,
        "inactive": len(users) - active,
        "by_role": dict(by_role),
    }
