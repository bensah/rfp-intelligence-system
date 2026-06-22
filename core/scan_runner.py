"""Shared manual-scan runner.

Invoked from both:
  * Admin tab `Manual Scan` (admin-only entry point)
  * Screen view "🔄 Scan now" button (available to any logged-in user)

Runs `scripts/run_scan.py` as a subprocess, surfaces the result inline in
the current Streamlit context, and clears `st.cache_data` so the page
re-renders with any newly-inserted RFPs.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import streamlit as st


def scannable_source_count() -> int:
    """Exact count of the sources a scan will actually scrape — the non-manual
    rows of the ACTIVE donor_sources catalogue, live from the DB so it tracks
    every add/remove. Delegates to run_scan.count_scannable_sources so the banner
    figure can never drift from the real scan set. Returns 0 on error."""
    try:
        from scripts.run_scan import count_scannable_sources
        return count_scannable_sources()
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
    # Report THIS run's own totals (parsed from its stdout summary line) rather
    # than re-reading scan_logs — otherwise an automated 'cron' scan that happens
    # to overlap can hijack the "last scan" figure and show 0 for your run.
    m = re.search(
        r"Scan done\D*(\d+) source\(s\)\D+(\d+) found\D+(\d+) new\D+(\d+) dup"
        r"\D+(\d+) declined", proc.stdout or "")
    if ok and m:
        s, f, nw, dp, dc = m.groups()
        st.success(
            f"✓ Scan complete — **{s}** sources · {f} found · **{nw} new** · "
            f"{dp} duplicate · {dc} declined by the eligibility policy."
        )
    elif ok:
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
