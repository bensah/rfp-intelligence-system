"""Shared manual-scan runner.

Invoked from both:
  * Admin tab `Manual Scan` (admin-only entry point)
  * Screen view "🔄 Scan now" button (available to any logged-in user)

Runs `scripts/run_scan.py` as a subprocess, surfaces the result inline in
the current Streamlit context, and clears `st.cache_data` so the page
re-renders with any newly-inserted RFPs.
"""
from __future__ import annotations

import os
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


def run_scan_now(triggered_by: str = "manual", timeout_sec: int = 900, *,
                 extract_only: bool = False) -> bool:
    """Run the donor-source crawl synchronously. extract_only=True → PURE
    extraction (crawl → global store, no org screening). The outcome is persisted
    to session_state['admin_scan_banner'] so it survives the caller's rerun.
    Returns True if the subprocess exited 0."""
    label = "Extraction" if extract_only else "Scan"
    if extract_only and timeout_sec == 900:
        # Extraction is the slow LLM-enrichment backend job; with maximised LLM
        # (per-page extractor + theme adjudication) a full ~50-source run can take
        # 25-40 min. Configurable via RFPIS_EXTRACT_TIMEOUT_SEC; default 45 min.
        try:
            timeout_sec = int(os.environ.get("RFPIS_EXTRACT_TIMEOUT_SEC", "2700") or 2700)
        except ValueError:
            timeout_sec = 2700
    cmd = [sys.executable, str(Path("scripts/run_scan.py")),
           "--triggered-by", triggered_by]
    if extract_only:
        cmd.append("--extract-only")
    with st.spinner(f"Running {label.lower()}…"):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            # Not a data loss: each source commits its records as it goes, so rows
            # extracted before the cutoff are already saved. Re-run to continue.
            msg = (f"{label} hit the {timeout_sec // 60}-min time limit — records "
                   "extracted before the cutoff were saved. Re-run to continue, or "
                   "raise RFPIS_EXTRACT_TIMEOUT_SEC (or lower LLM_EXTRACT_MAX_CALLS "
                   "for a faster, lighter-LLM run).")
            st.session_state["admin_scan_banner"] = {"ok": False, "msg": msg}
            st.warning(msg)
            st.cache_data.clear()
            return False

    ok = proc.returncode == 0
    m = re.search(
        r"Scan done\D*(\d+) source\(s\)\D+(\d+) found\D+(\d+) new\D+(\d+) dup"
        r"\D+(\d+) declined", proc.stdout or "")
    if ok and m:
        s, f, nw, dp, dc = m.groups()
        if extract_only:
            msg = (f"✓ Extraction complete — **{s}** sources · {f} found · "
                   f"**{nw} extracted** into the global store · {dc} not a fundable "
                   "opportunity. Run **My eligible funding** to screen them for your org.")
        else:
            msg = (f"✓ Scan complete — **{s}** sources · {f} found · **{nw} new** · "
                   f"{dp} duplicate · {dc} declined by the eligibility policy.")
    elif ok:
        msg = f"✓ {label} complete."
    else:
        msg = f"{label} exited with errors (code {proc.returncode})."
    st.session_state["admin_scan_banner"] = {"ok": ok, "msg": msg}
    (st.success if ok else st.error)(msg)

    with st.expander(f"{label} output", expanded=not ok):
        st.code(proc.stdout or "(no stdout)", language="text")
        if proc.stderr:
            st.markdown("**stderr:**")
            st.code(proc.stderr, language="text")

    st.cache_data.clear()
    return ok


def run_screening_now(triggered_by: str = "manual") -> bool:
    """'My eligible funding': screen the INTERNAL extracted store against this org's
    eligibility (geography + MUST/PREFER) — the opportunities the org is potentially
    eligible for. No web crawl → runs in-process in seconds. Outcome persisted to
    session_state['admin_scan_banner'] so it survives the caller's rerun."""
    with st.spinner("Selecting eligible funding — screening the curated store "
                    "against your eligibility policies…"):
        try:
            from core import scan_pipeline
            res = scan_pipeline.run_screening(triggered_by=triggered_by)
        except Exception as exc:
            st.session_state["admin_scan_banner"] = {
                "ok": False, "msg": f"My eligible funding failed: {exc}"}
            st.error(f"My eligible funding failed: {exc}")
            return False
    msg = (f"🎯 Screened **{res['considered']}** curated solicitations → "
           f"**{res['eligible']} eligible** for your org "
           f"({res['added']} newly added, {res['already_tracked']} already in your "
           f"pipeline) · {res['rejected']} not a fit. See the Screen tab.")
    st.session_state["admin_scan_banner"] = {"ok": True, "msg": msg}
    st.cache_data.clear()
    st.success(msg)
    return True
