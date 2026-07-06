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
    # `-u` → unbuffered child stdout, so per-source progress lines arrive LIVE (not
    # block-buffered until the process ends). We stream them into a progress UI below.
    cmd = [sys.executable, "-u", str(Path("scripts/run_scan.py")),
           "--triggered-by", triggered_by]
    if extract_only:
        cmd.append("--extract-only")

    stdout_full, ok, timed_out = _stream_scan(cmd, label, timeout_sec)

    if timed_out:
        msg = (f"{label} hit the {timeout_sec // 60}-min time limit — records "
               "extracted before the cutoff were saved. Re-run to continue, or "
               "raise RFPIS_EXTRACT_TIMEOUT_SEC (or lower LLM_EXTRACT_MAX_CALLS "
               "for a faster, lighter-LLM run).")
        st.session_state["admin_scan_banner"] = {"ok": False, "msg": msg}
        st.warning(msg)
        st.cache_data.clear()
        return False

    m = re.search(
        r"Scan done\D*(\d+) source\(s\)\D+(\d+) found\D+(\d+) new\D+(\d+) dup"
        r"\D+(\d+) declined", stdout_full or "")
    # Store-write errors (RLS/DB) are reported separately from declines — surface them
    # loudly so an infra problem is never mistaken for "nothing was fundable".
    _se_m = re.search(r"(\d+) store-error", stdout_full or "")
    _store_errors = int(_se_m.group(1)) if _se_m else 0
    _se_note = ("" if not _store_errors else
                f" ⚠ **{_store_errors} store-write error(s)** — these PASSED the gate but "
                "couldn't be saved (DB/RLS); they are NOT declines. Check the log.")
    if ok and m:
        s, f, nw, dp, dc = m.groups()
        if extract_only:
            msg = (f"✓ Extraction complete — **{s}** sources · {f} found · "
                   f"**{nw} extracted** into the shared curated store · {dc} not a "
                   "fundable opportunity. This is a platform/admin job — the curated "
                   "store now feeds every tenant's eligibility screening. (Only if you're "
                   "acting for a specific tenant, run that tenant's **My Eligible "
                   "Funding**.)" + _se_note)
        else:
            msg = (f"✓ Scan complete — **{s}** sources · {f} found · **{nw} new** · "
                   f"{dp} duplicate · {dc} declined by the eligibility policy." + _se_note)
    elif ok:
        msg = f"✓ {label} complete." + _se_note
    else:
        msg = f"{label} exited with errors."
    # A store-write error means the run didn't fully succeed even if it exited 0.
    _clean_ok = ok and _store_errors == 0
    st.session_state["admin_scan_banner"] = {"ok": _clean_ok, "msg": msg}
    (st.success if _clean_ok else st.warning if ok else st.error)(msg)

    with st.expander(f"{label} full log", expanded=not ok):
        st.code(stdout_full or "(no output)", language="text")

    st.cache_data.clear()
    return ok


def _stream_scan(cmd: list[str], label: str, timeout_sec: int) -> tuple[str, bool, bool]:
    """Run the scan subprocess and render a LIVE progress view (current phase, source
    name, per-source found/extracted/rejected, cumulative counts, elapsed timer) by
    streaming its stdout. A background reader thread + a 2s queue poll enforces the wall
    timeout even if the child goes briefly silent, and keeps ticking the elapsed clock so
    the user can see it's alive. Returns (full_stdout, ok, timed_out)."""
    import json as _json
    import queue as _queue
    import threading as _threading
    import time as _time
    from collections import deque

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace",
    )
    q: "_queue.Queue[str | None]" = _queue.Queue()

    def _reader():
        try:
            for line in proc.stdout:          # type: ignore[union-attr]
                q.put(line)
        finally:
            q.put(None)                        # EOF sentinel

    _threading.Thread(target=_reader, daemon=True).start()

    box = st.status(f"Starting {label.lower()}…", expanded=True)
    bar = box.progress(0.0)
    metric = box.empty()
    current = box.empty()
    logbox = box.empty()

    total, done, phase = 0, 0, "scrape"
    agg = {"found": 0, "extracted": 0, "rejected": 0, "evaluated": 0, "store_errors": 0}
    tail: "deque[str]" = deque(maxlen=14)
    full: list[str] = []
    start = _time.time()
    timed_out = False

    def _render():
        elapsed = int(_time.time() - start)
        clock = f"{elapsed // 60}m{elapsed % 60:02d}s"
        frac = 0.0
        if total:
            half = (done / total) * 0.5
            frac = half if phase == "scrape" else min(1.0, 0.5 + half)
        bar.progress(min(1.0, frac))
        if phase == "scrape":
            metric.markdown(
                f"🔎 **Crawling sources** · **{done}/{total or '…'}** done · "
                f"**{agg['found']}** links found · ⏱ {clock}")
        else:
            _serr = (f" · ⚠ **{agg['store_errors']}** store-write error(s)"
                     if agg["store_errors"] else "")
            metric.markdown(
                f"⛏ **Extracting** · **{done}/{total or '…'}** sources · "
                f"**{agg['extracted']}** extracted · **{agg['rejected']}** rejected"
                f"{_serr} · ⏱ {clock}")

    _render()
    while True:
        try:
            line = q.get(timeout=2.0)
        except _queue.Empty:
            if _time.time() - start > timeout_sec:
                proc.kill()
                timed_out = True
                break
            _render()                          # tick the clock while the child is quiet
            continue
        if line is None:                       # EOF
            break
        full.append(line)
        s = line.rstrip("\n")
        if s.startswith("@@PROGRESS@@ "):
            try:
                evt = _json.loads(s[len("@@PROGRESS@@ "):])
            except Exception:
                evt = None
            if evt:
                e = evt.get("event")
                if e == "start":
                    total, phase = evt.get("total", 0), "scrape"
                    box.update(label=f"Crawling {total} sources for opportunities…")
                elif e == "scraped":
                    done, total = evt.get("i", done), evt.get("total", total)
                    agg["found"] += int(evt.get("found", 0) or 0)
                    _mark = "⚠" if evt.get("err") else "✓"
                    current.markdown(
                        f"{_mark} {evt.get('source', '')} — "
                        f"{evt.get('found', 0)} links")
                elif e == "ingest_start":
                    phase, done, total = "ingest", 0, evt.get("total", total)
                    box.update(label=f"Extracting opportunities from {total} sources…")
                elif e == "ingested":
                    done, total = evt.get("i", done), evt.get("total", total)
                    agg["extracted"] += int(evt.get("new", 0) or 0)
                    agg["rejected"] += int(evt.get("rejected", 0) or 0)
                    agg["evaluated"] += int(evt.get("found", 0) or 0)
                    _se = int(evt.get("store_err", 0) or 0)
                    agg["store_errors"] += _se
                    current.markdown(
                        f"⛏ {evt.get('source', '')} — "
                        f"**{evt.get('new', 0)}** extracted · "
                        f"{evt.get('rejected', 0)} rejected · "
                        f"{evt.get('found', 0)} evaluated"
                        + (f" · ⚠ {_se} store-write error(s)" if _se else ""))
                _render()
        elif s.strip():
            tail.append(s)
            logbox.code("\n".join(tail), language="text")
        if _time.time() - start > timeout_sec:
            proc.kill()
            timed_out = True
            break

    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    ok = (proc.returncode == 0) and not timed_out
    _render()
    box.update(
        label=(f"{label} timed out" if timed_out else
               f"{label} complete" if ok else f"{label} finished with errors"),
        state=("error" if (timed_out or not ok) else "complete"),
        expanded=False,
    )
    return "".join(full), ok, timed_out


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
