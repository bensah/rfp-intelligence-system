"""Shared manual-scan runner.

Invoked from both:
  * Admin tab `Manual Scan` (admin-only entry point)
  * Screen view "🔄 Scan now" button (available to any logged-in user)

Runs `scripts/run_scan.py` as a subprocess, surfaces the result inline in
the current Streamlit context, and clears `st.cache_data` so the page
re-renders with any newly-inserted RFPs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import streamlit as st

_SOURCES_YAML = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"


def scannable_source_count() -> int:
    """Exact count of auto-scannable catalogued sources in config/sources.yaml
    (every entry whose method is NOT 'manual'). Used by the scan banners so the
    figure is real and updates with the file — never a hardcoded guess. Returns
    0 if the file can't be read."""
    try:
        import yaml
        with open(_SOURCES_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return sum(1 for s in (cfg.get("sources") or [])
                   if (s.get("method") or "").lower() != "manual")
    except Exception:
        return 0


def scan_banner(who: str | None = None) -> str:
    """The single, shared 'scan in progress' message — identical wherever a
    scan is launched (Admin → Manual Scan and Pipelines → Scan now). States the
    REAL source count and what the scan does: enrich each hit, then score it
    against the MUST/PREFER criteria into a Decision (Proceed / Park / Decline)."""
    n = scannable_source_count()
    src = f"{n} sources" if n else "all configured sources"
    lead = f"⏳ Scan running as **{who}**" if who else "⏳ Scan running"
    return (f"{lead} — scanning **{src}** with detail-page enrichment, then "
            "mapping each hit against the eligibility criteria (MUST / PREFER) "
            "into a Decision (Proceed / Park / Decline). Expect **3-8 minutes** "
            "— please stay on this screen.")


def run_scan_now(triggered_by: str = "manual", timeout_sec: int = 900) -> bool:
    """Trigger a scan synchronously and report status in the current Streamlit
    context. Returns True if the scan exited 0, False otherwise."""
    with st.spinner("Running scan…"):
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path("scripts/run_scan.py")),
                    "--triggered-by", triggered_by,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            st.error(f"Scan timed out after {timeout_sec // 60} minutes.")
            return False

    ok = proc.returncode == 0
    if ok:
        st.success("✓ Scan complete. Refreshing page…")
    else:
        st.error(f"Scan exited with errors (code {proc.returncode}).")

    with st.expander("Scan output", expanded=not ok):
        st.code(proc.stdout or "(no stdout)", language="text")
        if proc.stderr:
            st.markdown("**stderr:**")
            st.code(proc.stderr, language="text")

    # Invalidate all cached queries so freshly-inserted RFPs appear without a
    # browser refresh.
    st.cache_data.clear()
    return ok
