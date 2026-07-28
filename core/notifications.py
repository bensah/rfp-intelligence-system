"""Org-wide activity feed for the header notification bell.

Aggregates recent activity from existing tables (no new event-write wiring):
  * scan_logs        — auto/manual scan runs ("5 new · 40 found")
  * rfp_submissions  — newly added opportunities (manual entries; auto-scan
                       bulk adds are already summarised by the scan_logs row)

Plus a synthetic, pinned "next scheduled auto-scan" computed from the GitHub
Actions cron (Fridays 06:00 UTC). The feed is the SAME for everyone in the org;
only the unread marker is per-user (stored in app_settings).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pandas as pd
import streamlit as st

from core import settings
from db.supabase_client import get_client, safe_execute

# app_settings key holding {email: iso8601} of each user's last "mark read".
_SEEN_KEY = "notifications_seen"

# Auto-scan cron from .github/workflows/scan.yml: "0 6 * * 5" → Fri 06:00 UTC.
_SCAN_WEEKDAY = 4  # Mon=0 … Fri=4
_SCAN_HOUR_UTC = 6


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(v) -> datetime | None:
    if not v:
        return None
    try:
        ts = pd.to_datetime(v, utc=True, errors="coerce")
        return None if pd.isna(ts) else ts.to_pydatetime()
    except Exception:
        return None


def next_scheduled_scan(now: datetime | None = None) -> datetime:
    """Next Friday 06:00 UTC (the auto-scan cron)."""
    now = now or _now()
    days_ahead = (_SCAN_WEEKDAY - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=_SCAN_HOUR_UTC, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def relative_time(ts: datetime | None, now: datetime | None = None) -> str:
    """Compact 'in 3d' / '2h ago' style label."""
    if ts is None:
        return ""
    now = now or _now()
    delta = ts - now
    future = delta.total_seconds() > 0
    secs = abs(delta.total_seconds())
    if secs < 60:
        core = "just now" if not future else "soon"
        return core
    mins = secs / 60
    if mins < 60:
        unit = f"{int(mins)}m"
    elif mins < 60 * 24:
        unit = f"{int(mins // 60)}h"
    else:
        unit = f"{int(mins // (60 * 24))}d"
    return f"in {unit}" if future else f"{unit} ago"


@st.cache_data(ttl=60, show_spinner=False)
def recent_feed(scope_tid: str | None = None, is_super: bool = False,
                limit_scans: int = 12, limit_rfps: int = 15) -> list[dict]:
    """Activity items for the current viewer, newest first. Cached 60s PER
    (scope_tid, is_super) so one tenant's feed is never served to another.

    Scoping (multi-tenant):
      * scan runs (scan_logs): tenant_id NULL = SYSTEM-WIDE (the discovery auto-scan) →
        shown to everyone; tenant_id set (eligibility / "Find my matches" screening) →
        shown only to that tenant (super_user sees all).
      * new opportunities (rfp_submissions): the get_client() wrapper already scopes the
        read to the viewer's tenant (super_user / single-tenant → all).

    Each item: {ts, icon, title, detail, page}.
    """
    sb = get_client()
    items: list[dict] = []

    # ── Scan runs ───────────────────────────────────────────────────────
    # Select "*" (resilient if scan_logs.tenant_id / migration 074 isn't applied yet),
    # then filter in Python: system-wide rows (no tenant_id) are shown to all; a
    # tenant-stamped row is shown only to that tenant (super_user sees everything).
    try:
        _raw = (safe_execute(
            sb.table("scan_logs").select("*")
            .order("scan_date", desc=True).limit(limit_scans * 4)).data or [])
    except Exception:
        _raw = []
    scans = []
    for s in _raw:
        _stid = s.get("tenant_id")
        if is_super or not _stid or (scope_tid and str(_stid) == str(scope_tid)):
            scans.append(s)
        if len(scans) >= limit_scans:
            break
    _kind = {"cron": "Auto-scan", "manual": "Manual scan",
             "startup": "Startup scan", "test": "Test scan"}
    for s in scans:
        new = s.get("rfps_new") or 0
        found = s.get("rfps_found") or 0
        if s.get("errors"):
            icon, detail = "⚠️", "completed with errors"
        else:
            icon, detail = "🔎", f"{new} new · {found} found"
        items.append({
            "ts": _parse(s.get("scan_date")), "icon": icon,
            "title": f"{_kind.get(s.get('triggered_by'), 'Scan')} completed",
            "detail": detail, "page": "app_pages/report.py",
        })

    # ── Newly added opportunities (manual entries) ──────────────────────
    # Auto-scan bulk inserts are already represented by the scan_logs row
    # above, so we surface individual rows only for non-auto sources to
    # avoid flooding the feed after every Friday scan.
    try:
        rfps = (safe_execute(
            sb.table("rfp_submissions")
            .select("opportunity_title,source,search_date,submitted_by")
            .order("search_date", desc=True).limit(limit_rfps)).data or [])
    except Exception:
        rfps = []
    for r in rfps:
        if (r.get("source") or "").lower() == "auto":
            continue
        title = (r.get("opportunity_title") or "Untitled opportunity").strip()
        who = (r.get("submitted_by") or "").strip()
        items.append({
            "ts": _parse(r.get("search_date")), "icon": "📥",
            "title": "New opportunity",
            "detail": (f"{title[:64]}" + (f" — {who}" if who else "")),
            "page": "app_pages/pipelines.py",
        })

    items = [it for it in items if it["ts"] is not None]
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


# ---------------------------------------------------------------------------
# Per-user unread tracking (stored in app_settings; cached in session)
# ---------------------------------------------------------------------------
def _seen_map() -> dict:
    raw = settings.get_setting(_SEEN_KEY)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def last_seen(email: str) -> datetime | None:
    """The user's last 'mark all read' time. Read from the DB once per
    session, then cached in session_state to keep the header cheap."""
    if "_notif_last_seen" not in st.session_state:
        st.session_state["_notif_last_seen"] = _parse(_seen_map().get(email))
    return st.session_state["_notif_last_seen"]


def mark_all_read(email: str) -> None:
    ts = _now()
    st.session_state["_notif_last_seen"] = ts
    try:
        m = _seen_map()
        m[email] = ts.isoformat()
        settings.set_setting(_SEEN_KEY, json.dumps(m), updated_by=email)
    except Exception:
        pass  # non-fatal — the badge still clears for this session


def unread_count(feed: list[dict], seen: datetime | None) -> int:
    if seen is None:
        return len(feed)
    return sum(1 for it in feed if it["ts"] and it["ts"] > seen)
