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
