"""Excel → Supabase sync, callable from the Streamlit app.

Resolves the source workbook in this order:
  1. This TENANT's uploaded workbook (multi-tenant only)
  2. EXCEL_SOURCE_PATH environment variable / Streamlit secret
  3. Local copy at the repo root (any *.xlsx — gitignored), SINGLE-TENANT ONLY

The tenant step exists because of a real cross-tenant leak. An uploaded workbook used to
be written to the repo root and resolved with a glob over that same directory — one
filesystem path shared by the whole deployment. So a workbook uploaded by one organisation
appeared in every other tenant's Settings, named after its owner, and any admin pressing
"Sync Excel" would have imported that organisation's pipeline into their own. Uploads now
land under `.workbooks/<tenant-id>/` and are resolved only for the tenant that owns them.

The repo-root fallback is a DEVELOPER convenience and is disabled whenever multi-tenant is
on, for the same reason: a file lying beside the project belongs to nobody in particular,
so it must not become everybody's. A deployment that legitimately shares one workbook can
still point EXCEL_SOURCE_PATH at it deliberately.

Sync runs migrate_excel.py as a subprocess so import-time state is fresh
(no stale openpyxl handles between runs) and stdout is captured for display.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core import settings

REPO_ROOT = Path(__file__).resolve().parent.parent


def _reload_dotenv() -> None:
    """Re-read .env on every call so edits don't need a Streamlit restart."""
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env", override=True)
    except Exception:
        pass


def _secret(name: str) -> Optional[str]:
    _reload_dotenv()
    val = os.environ.get(name)
    if val:
        return val
    try:
        import streamlit as st  # type: ignore
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return None


def _unescape_path(raw: str) -> str:
    """Undo dotenv's escape-sequence processing for Windows paths.

    `EXCEL_SOURCE_PATH="C:\\Users\\youruser\\..."` works, but if the user
    writes `"C:\\Users\\youruser"` and dotenv treats `\\n` as a newline, we
    receive a string with literal newline/tab chars. Convert those back to
    `\\n` / `\\t` so the Path still resolves.
    """
    if not raw:
        return raw
    return (raw.replace("\n", "\\n")
               .replace("\r", "\\r")
               .replace("\t", "\\t"))


WORKBOOK_DIRNAME = ".workbooks"


def _multitenant() -> bool:
    try:
        from auth.tenant_context import multitenant_enabled
        return bool(multitenant_enabled())
    except Exception:
        return False


def _current_tenant() -> Optional[str]:
    try:
        from auth.tenant_context import current_tenant_id
        tid = current_tenant_id()
        return str(tid) if tid else None
    except Exception:
        return None


def workbook_dir(tenant_id: Optional[str] = None, *, create: bool = False) -> Optional[Path]:
    """Where THIS tenant's uploaded workbook lives: `.workbooks/<tenant-id>/`.

    One directory per tenant is what makes the isolation structural rather than a filter
    somebody has to remember — a tenant's resolve looks in its own directory and there is
    nowhere else for it to see. Returns None when no tenant resolves, which is exactly when
    an upload must not be accepted."""
    tid = tenant_id or _current_tenant()
    if not tid:
        return None
    path = REPO_ROOT / WORKBOOK_DIRNAME / str(tid)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def tenant_workbook(tenant_id: Optional[str] = None) -> Optional[Path]:
    """The newest .xlsx this tenant has uploaded, or None."""
    directory = workbook_dir(tenant_id)
    if not directory or not directory.exists():
        return None
    books = sorted(directory.glob("*.xlsx"), key=lambda f: f.stat().st_mtime, reverse=True)
    return books[0] if books else None


def save_tenant_workbook(name: str, data: bytes, tenant_id: Optional[str] = None) -> Path:
    """Store an uploaded workbook for one tenant. Raises when no tenant resolves — an
    upload with nowhere tenant-specific to go is the leak, not a fallback."""
    directory = workbook_dir(tenant_id, create=True)
    if directory is None:
        raise RuntimeError("No tenant is active, so there is nowhere private to store this "
                           "workbook. Sign in to the tenant it belongs to and try again.")
    safe = Path(str(name or "workbook.xlsx")).name        # never escape the directory
    if not safe.lower().endswith(".xlsx"):
        safe += ".xlsx"
    for old_book in directory.glob("*.xlsx"):             # one master workbook per tenant
        if old_book.name != safe:
            try:
                old_book.unlink()
            except Exception:
                pass
    dest = directory / safe
    dest.write_bytes(data)
    return dest


def resolve_excel_path() -> dict:
    """Diagnostic: returns {env_value, resolved_path, source, error}."""
    raw = _secret("EXCEL_SOURCE_PATH")
    out: dict = {"env_value": raw, "resolved_path": None, "source": None, "error": None}
    # THIS tenant's own upload wins over anything shared — see the module docstring.
    own = tenant_workbook()
    if own is not None:
        out["resolved_path"] = own
        out["source"] = "tenant upload"
        return out
    if raw:
        cleaned = _unescape_path(raw).strip().strip('"').strip("'")
        p = Path(cleaned)
        if p.exists():
            out["resolved_path"] = p
            out["source"] = "EXCEL_SOURCE_PATH"
            return out
        out["error"] = (
            f"EXCEL_SOURCE_PATH set but file not found: {p}\n"
            "If the path contains \\n (e.g. \\youruser), wrap the .env value in "
            "single quotes — double quotes let dotenv interpret \\n as a newline."
        )
    # Repo-root fallback: any *.xlsx sitting beside the project. A developer-local
    # convenience ONLY — under multi-tenant it is somebody else's data by definition, so
    # it is not offered to anyone. (This is the path that leaked one tenant's workbook to
    # the whole deployment.)
    if _multitenant():
        return out
    for candidate in REPO_ROOT.glob("*.xlsx"):
        out["resolved_path"] = candidate
        out["source"] = "repo fallback"
        break
    return out


def get_excel_path() -> Optional[Path]:
    return resolve_excel_path().get("resolved_path")


def get_last_sync() -> tuple[Optional[float], Optional[str]]:
    """Return (last-synced mtime, ISO timestamp) from app_settings."""
    mt_raw = settings.get_setting("last_excel_sync_mtime")
    ts_raw = settings.get_setting("last_excel_sync")
    try:
        mt = float(mt_raw) if mt_raw else None
    except (TypeError, ValueError):
        mt = None
    return mt, ts_raw


def needs_sync() -> Optional[Path]:
    """Return the Excel path if an AUTO-sync is due (workbook newer than the last
    recorded sync).

    DEACTIVATED in multi-tenant mode: the the sample country team workbook is single-tenant data
    with no tenant_id, so auto-syncing it on page load would dump the organisation's records into
    whatever tenant the user is browsing (e.g. a brand-new RFPIS Inc. tenant) and
    also re-run on every session. Admin → Settings → "Sync now" calls sync() directly and
    still works for a deliberate, owner-initiated single-tenant refresh."""
    try:
        from auth.tenant_context import multitenant_enabled
        if multitenant_enabled():
            return None
    except Exception:
        pass
    path = get_excel_path()
    if not path:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    last_mt, _ = get_last_sync()
    if last_mt and last_mt >= mtime:
        return None
    return path


def sync(timeout: int = 300, updated_by: Optional[str] = None,
         tenant_id: Optional[str] = None) -> dict:
    """Run the migration. Records last sync on success. When `tenant_id` is given, the
    importer stamps every row to that tenant (so a tenant admin's sync lands in THEIR
    pipeline rather than as NULL-tenant rows)."""
    path = get_excel_path()
    if not path:
        return {
            "ok": False,
            "error": "No Excel file found. Set EXCEL_SOURCE_PATH in .env or place "
                     "the workbook at the repo root.",
        }
    # Force UTF-8 on BOTH ends of the pipe. Without this the child defaults to
    # cp1252 on Windows and crashes (exit 1) the first time it prints a glyph
    # like "→" or "⚠"; and the parent would then fail to decode UTF-8 bytes.
    # PYTHONIOENCODING + PYTHONUTF8 make the child write UTF-8; encoding/errors
    # make the parent read it back losslessly.
    _env = dict(os.environ)
    _env["PYTHONIOENCODING"] = "utf-8"
    _env["PYTHONUTF8"] = "1"
    if tenant_id:
        _env["RFPIS_SYNC_TENANT_ID"] = str(tenant_id)   # importer stamps rows to this tenant
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "migrate_excel.py"),
             "--xlsx", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(REPO_ROOT), env=_env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Sync timed out after {timeout}s"}

    ok = proc.returncode == 0
    result = {
        "ok": ok,
        "path": str(path),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }

    if ok:
        try:
            mtime = path.stat().st_mtime
            settings.set_setting("last_excel_sync_mtime", str(mtime), updated_by=updated_by)
            settings.set_setting("last_excel_sync",
                                 datetime.now(timezone.utc).isoformat(),
                                 updated_by=updated_by)
            result["mtime"] = mtime
        except OSError:
            pass
    return result
