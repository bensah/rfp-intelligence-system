"""Database backup export — the super_user failsafe + the scheduled off-site copy.

Streamlit-free so the Settings UI (Records → Reset → Backup) and the headless cron
(scripts/run_backup.py → OneDrive) share ONE implementation. Reads cross-tenant on the
RLS-bypassing service client, paginated past PostgREST's ~1000-row cap, into a ZIP of
CSVs + a manifest.json.

Two modes:
  * FULL          — every row of every table (the manual UI download, and the first
                    scheduled run before any watermark exists).
  * INCREMENTAL   — only rows newer than the last successful backup (`since` watermark)
                    for the timestamped tables, PLUS a full copy of the small
                    no-timestamp tables (users/org_identity/tenant_memberships) so a
                    restore always has current config. If NOTHING is new it returns
                    None → the caller skips the backup ("only back up when there's new
                    data"). The Supabase Free plan has no managed backups, so this is
                    the failsafe. It's a data snapshot (CSV, no password hashes), not a
                    byte-for-byte restore.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone

# Timestamped tables → the column(s) that mark a row as new/changed. When both exist we
# match EITHER (new rows via created_at + edits that bump updated_at). Incremental export
# filters each of these on `> since`.
TABLE_TS: dict[str, list[str]] = {
    "rfp_submissions": ["updated_at", "created_at"],
    "extracted_solicitations": ["updated_at", "created_at"],
    "rfp_seen": ["created_at"],
    "scan_logs": ["scan_date"],
    "scan_decisions": ["created_at"],
    "meeting_logs": ["created_at"],
    "meeting_schedule": ["created_at"],
    "engagement_logs": ["created_at"],
    "active_grants": ["updated_at", "created_at"],
    "narrative_logs": ["created_at"],
    "donor_sources": ["updated_at", "created_at"],
    "donor_intel": ["updated_at"],
    "donor_contacts": ["updated_at", "created_at"],
    "scan_blacklist": ["created_at"],
    "app_settings": ["updated_at"],
    "tenants": ["updated_at", "created_at"],
    "tenant_settings": ["updated_at"],
}
# Small tables with no reliable change timestamp → always exported in FULL (tiny, and it
# keeps current users/roles/blacklist + org identity in every backup). They do NOT count
# toward the "is there new data?" decision — new opportunities/scans drive that.
FULL_ALWAYS: list[str] = ["users", "org_identity", "tenant_memberships"]

# Union, for reference / external callers.
BACKUP_TABLES: list[str] = list(TABLE_TS) + FULL_ALWAYS

_WATERMARK_KEY = "backup_watermark"


# ── Watermark (last successful backup) — stored in app_settings (global) ──────
def get_watermark() -> str | None:
    """ISO timestamp of the last successful backup, or None (→ full backup). Best-effort:
    any error returns None, so a lookup failure just yields a safe full backup."""
    try:
        from db.supabase_client import service_client
        rows = (service_client().table("app_settings").select("value")
                .eq("key", _WATERMARK_KEY).limit(1).execute().data or [])
        return rows[0]["value"] if rows else None
    except Exception:
        return None


def set_watermark(iso: str) -> None:
    """Persist the watermark (call ONLY after a backup is durably stored)."""
    from db.supabase_client import service_client
    service_client().table("app_settings").upsert(
        {"key": _WATERMARK_KEY, "value": iso}, on_conflict="key").execute()


# ── Filenames ─────────────────────────────────────────────────────────────────
def _stamp(iso: str | None) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", iso or "")
    if not m:
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    g = m.groups()
    return f"{g[0]}{g[1]}{g[2]}-{g[3]}{g[4]}{g[5]}"


def backup_filename(mode: str = "full", since: str | None = None,
                    until: str | None = None) -> str:
    u = _stamp(until) if until else datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # "opportunova_" — the public-facing platform name (opportunova.com); this DB is the
    # funding vertical today. the second tenant is the company, not the app.
    if mode == "incremental" and since:
        return f"opportunova_incr_{_stamp(since)}_to_{u}.zip"
    return f"opportunova_full_{u}.zip"


# ── Export ──────────────────────────────────────────────────────────────────
def _fetch(svc, table: str, *, cols: list[str] | None = None,
           since: str | None = None) -> list[dict]:
    """All rows of `table` (paginated). When `since` + `cols` are given, only rows where
    ANY of `cols` is > since (PostgREST or-filter)."""
    rows, start, page = [], 0, 1000
    while True:
        q = svc.table(table).select("*")
        if since and cols:
            q = q.or_(",".join(f"{c}.gt.{since}" for c in cols))
        chunk = q.range(start, start + page - 1).execute().data or []
        rows.extend(chunk)
        if len(chunk) < page:
            break
        start += page
    return rows


def build_incremental_backup(since: str | None) -> tuple[bytes | None, dict, str]:
    """Build a backup ZIP. Returns (zip_bytes | None, manifest, until_iso).

    zip_bytes is None ONLY when `since` is set (not a first/full run) AND no timestamped
    table had new rows — i.e. nothing to back up, so the caller skips. `until_iso` is the
    run-start time to persist as the next watermark after a durable store."""
    import pandas as pd
    from db.supabase_client import service_client

    svc = service_client()
    until = datetime.now(timezone.utc).isoformat()
    mode = "incremental" if since else "full"
    manifest: dict = {
        "generated_at": until, "mode": mode, "since": since, "until": until,
        "note": ("CSV data snapshot (no password hashes). Incremental backups hold only "
                 "rows changed since `since` for the bulk tables, plus a full copy of "
                 "users/org_identity/tenant_memberships. Restore = the first full backup "
                 "+ every later increment."),
        "tables": {},
    }
    table_rows: dict[str, list[dict]] = {}
    new_count = 0
    for t, cols in TABLE_TS.items():
        try:
            rows = _fetch(svc, t, cols=cols, since=since)
            table_rows[t] = rows
            manifest["tables"][t] = len(rows)
            new_count += len(rows)
        except Exception as exc:
            manifest["tables"][t] = f"skipped: {type(exc).__name__}"
    for t in FULL_ALWAYS:
        try:
            rows = _fetch(svc, t)
            table_rows[t] = rows
            manifest["tables"][t] = f"{len(rows)} (full)"
        except Exception as exc:
            manifest["tables"][t] = f"skipped: {type(exc).__name__}"
    manifest["new_rows"] = new_count

    if since and new_count == 0:
        return None, manifest, until               # nothing new → skip

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t, rows in table_rows.items():
            df = pd.DataFrame(rows)
            if t == "users" and "password_hash" in df.columns:
                df = df.drop(columns=["password_hash"])
            z.writestr(f"{t}.csv", df.to_csv(index=False))
        z.writestr("manifest.json", json.dumps(manifest, indent=2, default=str))
    return buf.getvalue(), manifest, until


def build_backup_zip() -> tuple[bytes, dict]:
    """FULL snapshot (all rows) — used by the manual UI download. Never returns None."""
    data, manifest, _ = build_incremental_backup(None)
    return data or b"", manifest
