"""Excel → Supabase sync, callable from the Streamlit app.

Resolves the source workbook in this order:
  1. EXCEL_SOURCE_PATH environment variable / Streamlit secret
  2. Local copy at the repo root (any *.xlsx — gitignored)

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

    `EXCEL_SOURCE_PATH="C:\\Users\\nbernard\\..."` works, but if the user
    writes `"C:\\Users\\nbernard"` and dotenv treats `\\n` as a newline, we
    receive a string with literal newline/tab chars. Convert those back to
    `\\n` / `\\t` so the Path still resolves.
    """
    if not raw:
        return raw
    return (raw.replace("\n", "\\n")
               .replace("\r", "\\r")
               .replace("\t", "\\t"))


def resolve_excel_path() -> dict:
    """Diagnostic: returns {env_value, resolved_path, source, error}."""
    raw = _secret("EXCEL_SOURCE_PATH")
    out: dict = {"env_value": raw, "resolved_path": None, "source": None, "error": None}
    if raw:
        cleaned = _unescape_path(raw).strip().strip('"').strip("'")
        p = Path(cleaned)
        if p.exists():
            out["resolved_path"] = p
            out["source"] = "EXCEL_SOURCE_PATH"
            return out
        out["error"] = (
            f"EXCEL_SOURCE_PATH set but file not found: {p}\n"
            "If the path contains \\n (e.g. \\nbernard), wrap the .env value in "
            "single quotes — double quotes let dotenv interpret \\n as a newline."
        )
    # Repo-root fallback: pick any *.xlsx sitting beside the project.
    # Excel workbooks are gitignored so this is a developer-local
    # convenience, not a shipped path.
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
    """Return the Excel path if it's newer than the last recorded sync."""
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


def sync(timeout: int = 300, updated_by: Optional[str] = None) -> dict:
    """Run the migration. Records last sync on success."""
    path = get_excel_path()
    if not path:
        return {
            "ok": False,
            "error": "No Excel file found. Set EXCEL_SOURCE_PATH in .env or place "
                     "the workbook at the repo root.",
        }
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "migrate_excel.py"),
             "--xlsx", str(path)],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO_ROOT),
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
