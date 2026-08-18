"""Page 9 — Admin panel.

Three working tabs in Phase 2/3:
  1. Donor Sources — CRUD over curated per-donor RFP listing URLs.
  2. Manual Scan — trigger a scanner run on demand; shows last-run summary.
  3. Scan Logs — read-only history of automated + manual scans.

User management, duplicate audit, and scoring-weight editor land in Phase 4.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path  # noqa: F401
from typing import Any

import pandas as pd
import streamlit as st

from core import excel_sync, settings
from core import permissions
from core.records import clean_df
from db.supabase_client import get_client, safe_execute
from views.account_sections import (
    render_manage_users, render_user_access, render_manage_tenants,
    render_blacklisted, render_org_suspend)
from views.org_setup import render_org_setup
from core.cache_scope import scope_key

user = st.session_state["app_user"]
# Defense in depth: the nav already omits this page for non-admins, but
# gate here too in case someone deep-links to it.
if not permissions.is_admin(user):
    st.error("Admins only.")
    st.stop()

sb = get_client()
st.title("Settings")


def _purge_seen_ledger(sb, deleted_rows) -> int:
    """Remove the given deleted rfp rows' uids from the permanent seen-ledger.

    Used ONLY by the explicit "rescan from scratch" / "fresh-test reset" tools, so
    a deliberate auto-scan wipe can be re-found. Individual RFP deletes do NOT
    call this — their tombstone stays, so a rejected opportunity never re-enters.
    Best-effort; returns the count purged (0 on any error)."""
    try:
        uids = [r.get("uid") for r in (deleted_rows or []) if r.get("uid")]
        if not uids:
            return 0
        sb.table("rfp_seen").delete().in_("uid", uids).execute()
        return len(uids)
    except Exception:
        return 0


def _render_maintenance(sb, user) -> None:
    """Super-User-only maintenance area (called from Records → Reset). Three zones:
    Backup (non-destructive failsafe) → Maintenance (safe housekeeping) → Danger zone
    (irreversible deletes, gated behind a downloaded-backup acknowledgement)."""
    from core import backup as _backup, onedrive as _onedrive
    # ── 🛟 Backup ───────────────────────────────────────────────────────
    st.subheader("🛟 Backup")
    st.caption(
        "Export a full **data snapshot** (all tables → CSV, zipped). Download it to your "
        "local disk, or push it straight to OneDrive. A **weekly incremental** off-site "
        "backup also runs automatically on **Sundays** once the OneDrive secrets are set — "
        "it captures only what changed since the previous backup, and skips entirely when "
        "nothing is new. The Supabase Free plan has no managed backups, so this is the "
        "failsafe — a data snapshot (no password hashes), not a full-fidelity restore. "
        "(These buttons always make a **full** snapshot.)")
    bkc1, bkc2 = st.columns(2)
    if bkc1.button("🛟 Prepare full backup (ZIP)", type="primary",
                   width='stretch', key="maint_backup_prep"):
        with st.spinner("Exporting all tables…"):
            try:
                _blob, _man = _backup.build_backup_zip()
                st.session_state["_backup_blob"] = _blob
                st.session_state["_backup_manifest"] = _man
                st.session_state["_backup_ready_at"] = _backup.backup_filename()
            except Exception as exc:
                st.error(f"Backup failed: {exc}")
    # Push to OneDrive on demand (only when configured).
    if _onedrive.is_configured():
        if bkc2.button("☁ Back up to OneDrive now", width='stretch',
                       key="maint_backup_od"):
            with st.spinner("Exporting + uploading to OneDrive…"):
                try:
                    _blob, _man = _backup.build_backup_zip()
                    _fn = _backup.backup_filename()
                    _onedrive.upload_bytes(_blob, _fn)
                    st.success(f"Uploaded **{_fn}** → OneDrive "
                               f"({_onedrive.folder_label()}).")
                except Exception as exc:
                    st.error(f"OneDrive upload failed: {exc}")
    else:
        bkc2.caption("☁ OneDrive off-site backup: add the `ONEDRIVE_*` secrets to enable "
                     "the daily job + a one-click upload here.")
    if st.session_state.get("_backup_blob"):
        _fn = st.session_state.get("_backup_ready_at", "opportunova_backup.zip")
        st.download_button(
            "⬇ Download backup ZIP", data=st.session_state["_backup_blob"],
            file_name=_fn, mime="application/zip", key="maint_backup_dl")
        _man = st.session_state.get("_backup_manifest") or {}
        with st.expander("What's in this backup", expanded=False):
            st.json(_man.get("tables", {}))

    st.markdown("---")
    # ── 🧹 Maintenance (safe / non-destructive) ─────────────────────────
    st.subheader("🧹 Maintenance")
    st.caption("Safe housekeeping — nothing is deleted.")
    mc1, mc2 = st.columns(2)
    if mc1.button("🔁 Re-flag duplicates (re-run dedup)", width='stretch',
                  key="maint_dedup"):
        from scripts.dedup_existing import run as run_dedup
        try:
            with st.spinner("Re-running dedup…"):
                res = run_dedup(reset=True, preserve_completed=True)
            st.success(
                f"Considered {res['considered']} canonical row(s) of {res['total_rows']} "
                f"total · flagged **{res['flagged']}** as duplicate · skipped "
                f"**{res['skipped_completed']}** Completed pair(s). (Flags only — no rows "
                "deleted.)")
        except Exception as exc:
            st.error(f"Re-dedup failed: {exc}")
    if mc2.button("🧽 Clear app cache", width='stretch', key="maint_cache"):
        st.cache_data.clear()
        try:
            from core import settings as _s
            _s.clear_cache()
        except Exception:
            pass
        st.success("Cache cleared — data re-fetches on next interaction.")
        st.rerun()

    st.markdown("---")
    # ── ⚠️ Danger zone (irreversible; backup-gated) ─────────────────────
    with st.expander("⚠️ Danger zone — irreversible deletes", expanded=False):
        st.warning(
            "These permanently delete rows from the live database and **cannot be undone**. "
            "Now that the scan pipeline is reliable, they should be rare — reach for them "
            "only for a deliberate clean-up, and **only after downloading a backup above.**")
        _ok = st.checkbox(
            "I have downloaded a current backup and understand this is irreversible.",
            key="maint_danger_ack")

        st.markdown("**Delete auto-scanned opportunities** — removes `rfp_submissions` "
                    "rows with `source='auto'` (keeps manual + imported), and clears their "
                    "seen-ledger tombstones so a fresh scan can re-find them.")
        if st.button("🗑 Delete auto-scanned opportunities", disabled=not _ok,
                     key="maint_del_auto"):
            try:
                res = sb.table("rfp_submissions").delete().eq("source", "auto").execute()
                _rows = res.data or []
                _purged = _purge_seen_ledger(sb, _rows)
                st.success(f"Deleted **{len(_rows)}** auto-scanned row(s)"
                           + (f" and {_purged} tombstone(s)" if _purged else "") + ".")
            except Exception as exc:
                st.error(f"Delete failed: {exc}")

        st.markdown("**Clear scan history** — removes every `scan_logs` row (metrics + "
                    "history read empty until the next scan). Does not touch opportunities.")
        if st.button("🗑 Clear scan history", disabled=not _ok, key="maint_del_logs"):
            try:
                res = (sb.table("scan_logs").delete()
                       .neq("id", "00000000-0000-0000-0000-000000000000").execute())
                st.success(f"Deleted **{len(res.data or [])}** scan-log row(s).")
            except Exception as exc:
                st.error(f"Clear failed: {exc}")

        st.markdown("**Wipe Excel-imported rows** — legacy cleanup for the (now-deactivated) "
                    "Excel sync: deletes `meeting_logs` (all) and `applied_funding` where "
                    "`source='migration'`.")
        wc1, wc2 = st.columns(2)
        if wc1.button("🗑 Wipe meeting_logs", disabled=not _ok, key="maint_wipe_ml"):
            try:
                res = (sb.table("meeting_logs").delete()
                       .neq("id", "00000000-0000-0000-0000-000000000000").execute())
                st.success(f"Deleted **{len(res.data or [])}** meeting-log row(s).")
            except Exception as exc:
                st.error(f"Wipe failed: {exc}")
        if wc2.button("🗑 Wipe migration grants", disabled=not _ok, key="maint_wipe_ag"):
            try:
                res = (sb.table("applied_funding").delete()
                       .eq("source", "migration").execute())
                st.success(f"Deleted **{len(res.data or [])}** migration grant row(s).")
            except Exception as exc:
                st.error(f"Wipe failed: {exc}")

# Role + tenant-category flags. Developer flags gate the cross-tenant DEVELOPER tasks
# (donor mapping, Sources catalog + Blocked tokens, Run Extraction, Records Verify/Reset,
# Learning data) to members of a developer/system tenant. A CLIENT
# tenant's admins keep only their own tenant-scoped surfaces. See core.permissions.
_is_super = permissions.is_super_user(user)
_dev_super = permissions.is_developer_super(user)     # super_user in a developer tenant
_dev_admin = permissions.is_developer_admin(user)     # admin+ in a developer tenant
_dev_member = permissions.is_developer_member(user)   # any member of a developer tenant

# Tab set. "Accounts" (users + super-only tenants/blacklisted) sits right after Setup;
# "Sources" nests Validated | Verify Registry | Blocked (Excel Sync moved to Manual Scan). "Learning data" is a developer-only
# view (its BODY is gated to developer-tenant members below, like Records → Verify/Reset).
# A developer Super User also gets a "Suggestions" review inbox (with a pending-count
# badge); the super_user also gets a cross-tenant "Analytics" tab.
# Sources + Learning data are DEVELOPER-only surfaces — hidden entirely (tab AND content)
# from client tenants; only members of a developer tenant see them. The per-tab render
# blocks below are guarded on `tab_sources`/`tab_learning` being non-None accordingly.
_tab_labels = ["Setup", "Accounts", "Records"]
if _dev_member:
    _tab_labels.append("Sources")
_tab_labels.append("Manual Scan")
if _dev_member:
    _tab_labels.append("Learning data")
_sug_label = None
if _dev_super:
    from core import suggestions as _sugmod
    _sug_n = _sugmod.pending_count(user)
    _sug_label = f"Suggestions ({_sug_n})" if _sug_n else "Suggestions"
    _tab_labels += [_sug_label]
if _is_super:
    _tab_labels += ["Analytics"]
_tabs = st.tabs(_tab_labels)
_tab_by = {name: _tabs[i] for i, name in enumerate(_tab_labels)}
tab_settings = _tab_by["Setup"]
tab_accounts = _tab_by["Accounts"]
tab_data = _tab_by["Records"]
tab_sources = _tab_by.get("Sources")            # None for a client tenant
tab_scan = _tab_by["Manual Scan"]
tab_learning = _tab_by.get("Learning data")      # None for a client tenant
tab_suggestions = _tab_by.get(_sug_label) if _sug_label else None
tab_analytics = _tab_by.get("Analytics")

if tab_suggestions is not None:
    with tab_suggestions:
        from views.suggestions_inbox import render_suggestions_inbox
        render_suggestions_inbox(user, sb)

# Accounts — Users, plus Tenants / Blacklisted. Tenants/Blacklisted are shown to EVERYONE
# now, but VIEW-ONLY for non-super users (scoped to the tenants they belong to); only the
# super_user gets the management controls (add / suspend / blacklist / approve / developer).
# User-admin logic lives in views/account_sections.py so pages share one impl.
with tab_accounts:
    # "Deployment" is super-only: it reports what this running instance read from its
    # environment (secrets provenance, project ref, key kind, resolved tenant) — the
    # answer to "the published app behaves differently from my machine".
    _acct_tab_names = ["Users", "Tenants", "Blacklisted"] + (["Deployment"] if _is_super
                                                             else [])
    _acct_tabs = st.tabs(_acct_tab_names)
    with _acct_tabs[0]:
        _picked_user = render_manage_users(user, sb)
        if _picked_user:
            st.divider()
            render_user_access(user, target=_picked_user)
    with _acct_tabs[1]:
        render_manage_tenants(user, sb, can_manage=_is_super)
    with _acct_tabs[2]:
        render_blacklisted(user, sb, can_manage=_is_super)
    if _is_super:
        with _acct_tabs[3]:
            from core.env_diag import render as render_env_diagnostics
            render_env_diagnostics(user)
if tab_analytics is not None:
    with tab_analytics:
        from views.super_analytics import render_super_analytics
        render_super_analytics(user)


# -----------------------------------------------------------------------------
# Tab 0 — App settings
# -----------------------------------------------------------------------------
with tab_settings:
    st.subheader("App-wide settings")
    st.caption(
        "Stored in the `app_settings` table and read by every page. No code "
        "change needed when the year rolls over."
    )

    from datetime import date as _date_today
    current_year = settings.get_year()
    calendar_year = _date_today.today().year
    c1, c2 = st.columns([1, 3])
    new_year = c1.number_input(
        "Default review year (optional)",
        min_value=2020, max_value=2050,
        value=int(current_year), step=1,
        help="Optional override. Set this ONLY to a FUTURE year for early "
             "planning. Past-year values are ignored automatically — the "
             "app always rolls forward to the current calendar year so the "
             "week dropdowns never go stale.",
    )
    if c1.button("💾 Save year", type="primary"):
        settings.set_setting("year", str(int(new_year)), updated_by=user.get("email"))
        if int(new_year) < calendar_year:
            st.warning(
                f"Saved as {int(new_year)} but ignored at runtime — past-year "
                f"override. Active year will remain {calendar_year} (calendar)."
            )
        else:
            st.success(f"Default year set to {int(new_year)}.")
        st.rerun()

    c2.info(
        f"**Active year: {current_year}**  · driven by the calendar by "
        "default. The dropdown above is only honoured if you set it to "
        "the current year or a future one (early-planning use case). "
        "Once the calendar rolls into a new year, the app auto-rolls — "
        "no manual intervention needed."
    )

    st.markdown("---")
    render_org_setup(user, sb)
    # Admin self-service: suspend (retire, never delete) this org's tenant account.
    # No-op for the super_user (who uses Accounts → Tenants) and in single-tenant mode.
    render_org_suspend(user, sb)

with tab_data:
    _dtab, _vtab, _rtab = st.tabs(["Data", "Verify", "Backup & Reset"])
    with _vtab:
        # Human verification acts on the SHARED scanner (auto-rejects, recovering
        # false-rejects into the global store) — a cross-tenant DEVELOPER task,
        # so it's restricted to a developer-tenant Super User. Info notice (not st.stop())
        # so sibling Records tabs still render in the same script pass.
        if not _dev_super:
            st.info(
                "🔒 **Verification is a developer task.** It tunes the shared scanner "
                "(confirming auto-rejects, recovering missed calls), "
                "which affects every tenant — so it's limited to the Super User of a "
                "developer tenant.")
        else:
            from views.verification import render_verification
            render_verification(user, sb)
    with _dtab:
        st.subheader("Data — view, filter, edit, delete, share")
        st.caption(
            "All record tables in one place. Pick a table below. **Sync behaviour:** "
            "auxiliary tables are keyed by `external_id` (stable hash of the row's "
            "natural key). When you re-sync from Excel, only Excel-managed columns "
            "are overwritten — app-managed columns (e.g. **Status** on Meeting Logs) "
            "are preserved. Rows you add here with no `external_id` are app-only "
            "and never touched by Excel sync."
        )

        # --- Per-table specs (Screened Solicitations handled separately below) ---
        _DATA_SPECS: dict[str, dict] = {
            "Meeting Logs": {
                "table": "meeting_logs",
                "order_col": "meeting_date",
                "table_cols": ["meeting_date", "donor_title", "owner", "deadline",
                               "is_resolved", "rfp_uid", "remarks", "actions", "source"],
                "col_labels": {
                    "meeting_date": "Meeting date", "donor_title": "Donor",
                    "owner": "Owner", "deadline": "Due",
                    "is_resolved": "Status", "rfp_uid": "Linked RFP",
                    "remarks": "Issues", "actions": "Actions", "source": "Source",
                },
                "date_cols": ["meeting_date", "deadline"],
                "search_cols": ["donor_title", "remarks", "actions", "owner"],
                "advanced_filters": ["owner", "is_resolved", "source"],
                "edit_fields": [
                    ("meeting_date",  "date",   "Meeting date *"),
                    ("donor_title",   "text",   "Donor title"),
                    ("remarks",       "area",   "Issues / Remarks"),
                    ("actions",       "area",   "Actions / Recommendations"),
                    ("owner",         "text",   "Owner"),
                    ("deadline",      "date",   "Due date"),
                    ("is_resolved",   "bool",   "Status: Resolved?"),
                    ("rfp_uid",       "text",   "Linked RFP UID"),
                ],
                "caption": (
                    "**Excel-managed:** date, donor, issues, actions, owner, deadline. "
                    "**App-managed (preserved on sync):** Status, Linked RFP."
                ),
            },
            "Engagement Logs": {
                "table": "engagement_logs",
                "order_col": "engagement_date",
                "table_cols": ["engagement_date", "donor", "engagement_type", "format",
                               "internal_lead", "purpose", "linked_rfp_uid", "source"],
                "col_labels": {
                    "engagement_date": "Date", "donor": "Donor",
                    "engagement_type": "Type", "format": "Format",
                    "internal_lead": "Internal lead", "purpose": "Purpose",
                    "linked_rfp_uid": "Linked RFP", "source": "Source",
                },
                "date_cols": ["engagement_date"],
                "search_cols": ["donor", "purpose", "outcome", "internal_lead"],
                "advanced_filters": ["internal_lead", "engagement_type", "source"],
                "edit_fields": [
                    ("engagement_date", "date", "Engagement date *"),
                    ("donor",           "text", "Donor"),
                    ("engagement_type", "text", "Engagement type"),
                    ("format",          "text", "Format"),
                    ("internal_lead",       "text", "Internal lead"),
                    ("donor_contacts",  "area", "Donor contacts"),
                    ("purpose",         "area", "Purpose"),
                    ("outcome",         "area", "Outcome"),
                    ("linked_rfp_uid",  "text", "Linked RFP UID"),
                ],
                "caption": "All fields Excel-managed; edits here are overwritten by next sync if the same row exists in Excel.",
            },
            "Applied Funding": {
                "table": "applied_funding",
                "order_col": "report_due_date",
                "table_cols": ["funding_id", "donor_title", "award_date", "end_date",
                               "status", "owner", "report_type", "report_due_date",
                               "source"],
                "col_labels": {
                    "funding_id": "Grant ID", "donor_title": "Donor",
                    "award_date": "Awarded", "end_date": "Ends",
                    "status": "Status", "owner": "Owner",
                    "report_type": "Report", "report_due_date": "Report due",
                    "source": "Source",
                },
                "date_cols": ["award_date", "end_date", "report_due_date", "submitted_date"],
                "search_cols": ["funding_id", "donor_title", "owner", "remarks"],
                "advanced_filters": ["owner", "status", "report_type", "source"],
                "edit_fields": [
                    ("funding_id",        "text", "Grant ID *"),
                    ("donor_title",     "text", "Donor"),
                    ("form_id_link",    "text", "Linked RFP form ID"),
                    ("award_date",      "date", "Award date"),
                    ("end_date",        "date", "End date"),
                    ("report_type",     "text", "Report type"),
                    ("report_due_date", "date", "Report due date"),
                    ("submitted_date",  "date", "Submitted date"),
                    ("status",          "text", "Status"),
                    ("owner",           "text", "Owner"),
                    ("remarks",         "area", "Remarks"),
                ],
                "caption": "Keyed on `funding_id`. Re-sync OVERWRITES matching rows from Excel.",
            },
            "Narrative Logs": {
                "table": "narrative_logs",
                "order_col": "version_date",
                "table_cols": ["version_date", "narrative_title", "used_in", "used_with",
                               "date_used", "status", "owner"],
                "col_labels": {
                    "version_date": "Version", "narrative_title": "Title",
                    "used_in": "Used in", "used_with": "Used with",
                    "date_used": "Date used", "status": "Status", "owner": "Owner",
                },
                "date_cols": ["version_date", "date_used"],
                "search_cols": ["narrative_title", "used_in", "used_with", "owner"],
                "advanced_filters": ["owner", "status"],
                "edit_fields": [
                    ("version_date",    "date", "Version date *"),
                    ("narrative_title", "text", "Title"),
                    ("used_in",         "text", "Used in"),
                    ("used_with",       "text", "Used with"),
                    ("date_used",       "date", "Date used"),
                    ("status",          "text", "Status"),
                    ("link_location",   "text", "Link / location"),
                    ("owner",           "text", "Owner"),
                ],
                "caption": "All fields Excel-managed.",
            },
        }

        _SCREENED = "Screened Solicitations"      # was "Found RFPs" (UI rename)
        _EXTRACTED = "Extracted Solicitations"
        _DATA_OPTIONS = [_SCREENED, _EXTRACTED] + list(_DATA_SPECS.keys())
        pick = st.selectbox("Table", _DATA_OPTIONS, key="data_table_pick")

        # ----- SCREENED SOLICITATIONS branch — renders the master line-list -----
        if pick == _SCREENED:
            from core.render_view import render_view
            render_view("rfp_records")

        # ----- EXTRACTED SOLICITATIONS branch — the global raw store (read-only) -
        elif pick == _EXTRACTED:
            st.info(
                "Every opportunity captured by scans, across **all geographies**, before "
                "eligibility screening. Read-only here; a scan populates it. Rows that "
                "your Screened pipeline rejects on geography still appear in this store.")
            from core import extracted_store as _es
            _erows = _es.list_extracted(limit=5000)
            edf = pd.DataFrame(_erows)
            if edf.empty:
                st.info("No extracted solicitations yet — run a scan to populate this store.")
            else:
                # Controls on ONE row: Search · Status · Page. (No "Find my matches"
                # here — this is the super-user/dev raw store; matching is a tenant
                # action that lives on the Pipeline page + Manual Scan tab.)
                _c1, _c2, _c3 = st.columns([3, 1.5, 1])
                _eq = _c1.text_input("Search (name / funder / geography)", key="extr_q",
                                     placeholder="malaria, Gates, Mali, LMICs")
                _status = _c2.selectbox("Status", ["All", "Open", "Closed"], key="extr_status")
                fdf = edf
                if _eq:
                    _term = _eq.lower()
                    _m = pd.Series(False, index=edf.index)
                    for _c in ("opportunity_name", "funder_name", "call_geographic_scope",
                               "solicitation_type"):
                        if _c in edf.columns:
                            _m |= edf[_c].fillna("").astype(str).str.lower().str.contains(
                                _term, regex=False)
                    fdf = edf[_m].reset_index(drop=True)
                if _status != "All" and "funding_status" in fdf.columns:
                    fdf = fdf[fdf["funding_status"] == _status].reset_index(drop=True)
                _per = 25
                _pages = max(1, (len(fdf) + _per - 1) // _per)
                _pg = int(_c3.number_input("Page", 1, _pages, 1, step=1, key="extr_pg"))
                st.caption(f"**{len(fdf)}** of {len(edf)} extracted solicitations "
                           f"(all geographies) · page {_pg} of {_pages}")
                _show_cols = ["opportunity_name", "funder_name", "funding_status",
                              "deadline", "deadline_confidence", "grant_amount",
                              "currency", "call_geographic_scope", "solicitation_type",
                              "funding_window", "source", "opportunity_url"]
                _show = fdf[[c for c in _show_cols if c in fdf.columns]]
                st.dataframe(
                    _show.iloc[(_pg - 1) * _per: _pg * _per], hide_index=True,
                    width='stretch',
                    column_config={"opportunity_url": st.column_config.LinkColumn(
                        "Link", display_text="Open ↗")})
                st.divider()
                st.download_button(
                    f"⬇ Download all {len(fdf)} (CSV)",
                    fdf.to_csv(index=False).encode("utf-8"),
                    file_name="extracted_solicitations.csv", mime="text/csv",
                    key="extr_dl",
                    help="Every row in the current (filtered) view — all pages, all columns.")

        # ----- Auxiliary tables branch -------------------------------------------
        else:
            spec = _DATA_SPECS[pick]
            st.info(spec["caption"])

            @st.cache_data(ttl=15)
            def _fetch_table(table: str, order_col: str, scope: str) -> pd.DataFrame:
    # `scope` is the tenant discriminator for this PROCESS-GLOBAL cache; it is unused in
    # the body on purpose. Never rename it with a leading underscore — Streamlit drops
    # underscore-prefixed args from the key. See core.cache_scope.
                try:
                    res = (
                        get_client()
                        .table(table)
                        .select("*")
                        .order(order_col, desc=True)
                        .limit(2000)
                        .execute()
                    )
                    return clean_df(pd.DataFrame(res.data or []))
                except Exception as exc:
                    st.error(f"Could not load {table}: {exc}")
                    return pd.DataFrame()

            df = _fetch_table(spec["table"], spec["order_col"], scope_key())
            if df.empty:
                st.info(f"No rows in `{spec['table']}` yet. Use ➕ Add new below.")
            else:
                # ----- Common filters (always visible) ----------------------------
                with st.expander("Filters", expanded=True):
                    fc1, fc2, fc3 = st.columns([2, 2, 3])
                    # Date-range filter on the primary date column
                    primary_date = spec["date_cols"][0] if spec["date_cols"] else None
                    date_range = None
                    if primary_date and primary_date in df.columns:
                        series = pd.to_datetime(df[primary_date], errors="coerce")
                        valid = series.dropna()
                        if not valid.empty:
                            dmin, dmax = valid.min().date(), valid.max().date()
                            date_range = fc1.date_input(
                                f"{spec['col_labels'].get(primary_date, primary_date)} range",
                                value=(dmin, dmax),
                                min_value=dmin, max_value=dmax,
                                key=f"flt_dates_{spec['table']}",
                            )
                    # Source filter (migration vs app)
                    if "source" in df.columns:
                        src_opts = sorted(df["source"].dropna().unique().tolist())
                        if src_opts:
                            f_source = fc2.multiselect(
                                "Source", src_opts, key=f"flt_src_{spec['table']}",
                            )
                        else:
                            f_source = []
                    else:
                        f_source = []
                    # Free-text search across spec-defined fields
                    text_q = fc3.text_input(
                        "Search (any field)",
                        placeholder=f"Searches: {', '.join(spec['search_cols'])}",
                        key=f"flt_text_{spec['table']}",
                    )

                    # Advanced filters expander
                    adv_filters: dict[str, list[str]] = {}
                    with st.expander("Advanced filters", expanded=False):
                        ac = st.columns(min(3, max(1, len(spec["advanced_filters"]))))
                        for i, col in enumerate(spec["advanced_filters"]):
                            if col not in df.columns:
                                continue
                            opts = sorted(df[col].dropna().astype(str).unique().tolist())
                            if not opts:
                                continue
                            adv_filters[col] = ac[i % len(ac)].multiselect(
                                spec["col_labels"].get(col, col).title(),
                                opts,
                                key=f"flt_adv_{spec['table']}_{col}",
                            )

                # ----- Apply filters ---------------------------------------------
                mask = pd.Series(True, index=df.index)
                if date_range and primary_date in df.columns:
                    if isinstance(date_range, tuple) and len(date_range) == 2:
                        lo, hi = date_range
                        series = pd.to_datetime(df[primary_date], errors="coerce").dt.date
                        # Keep rows with NULL dates — otherwise applied_funding
                        # entries that haven't been awarded yet get hidden,
                        # and the user can't see/manage them.
                        mask &= series.between(lo, hi) | series.isna()
                if f_source:
                    mask &= df["source"].isin(f_source)
                if text_q:
                    # Case-insensitive contains across all search_cols
                    term = text_q.lower()
                    col_match = pd.Series(False, index=df.index)
                    for c in spec["search_cols"]:
                        if c in df.columns:
                            col_match |= df[c].fillna("").astype(str).str.lower().str.contains(term, regex=False)
                    mask &= col_match
                for col, picks in adv_filters.items():
                    if picks:
                        mask &= df[col].astype(str).isin(picks)

                fdf = df[mask].copy().reset_index(drop=True)

                # ----- Pagination -------------------------------------------------
                pc1, pc2, pc3, pc4 = st.columns([1, 1, 4, 1])
                page_size = pc1.selectbox(
                    "Per page", [10, 25, 50, 100, 1000], index=0,
                    key=f"pgsize_{spec['table']}",
                )
                total_pages = max(1, (len(fdf) + page_size - 1) // page_size)
                page = pc2.number_input(
                    "Page", min_value=1, max_value=total_pages, value=1, step=1,
                    key=f"pgnum_{spec['table']}",
                )
                pc3.markdown(
                    f"<div style='padding-top: 28px; color: #555;'>Page <b>{page}</b> of <b>{total_pages}</b> · "
                    f"<b>{len(fdf)}</b> matching row{'s' if len(fdf)!=1 else ''} (of {len(df)} total)</div>",
                    unsafe_allow_html=True,
                )
                if pc4.button("🔄 Refresh", width='stretch', key=f"refresh_{spec['table']}"):
                    st.cache_data.clear()
                    st.rerun()

                start = (page - 1) * page_size
                end = start + page_size
                view_df = fdf.iloc[start:end].reset_index(drop=True)

                # ----- Display + row selection ------------------------------------
                display_cols = [c for c in spec["table_cols"] if c in view_df.columns]
                display = view_df.reindex(columns=display_cols).copy()
                col_cfg: dict[str, Any] = {}
                for c in display.columns:
                    lbl = spec["col_labels"].get(c, c)
                    if c in spec["date_cols"]:
                        display[c] = pd.to_datetime(display[c], errors="coerce").dt.date
                        col_cfg[c] = st.column_config.DateColumn(lbl)
                    elif c == "is_resolved":
                        col_cfg[c] = st.column_config.CheckboxColumn(lbl, width="small")
                    else:
                        col_cfg[c] = st.column_config.TextColumn(lbl)

                event = st.dataframe(
                    display,
                    width='stretch',
                    hide_index=True,
                    selection_mode="multi-row",
                    on_select="rerun",
                    column_config=col_cfg,
                    key=f"tbl_{spec['table']}",
                )
                selected_rows = (
                    event.selection.rows
                    if event and getattr(event, "selection", None) else []
                )
                # Resolve every selected row's full dict.
                sel_rows: list[dict] = [
                    view_df.iloc[i].to_dict() for i in selected_rows
                ]
                is_multi = len(sel_rows) > 1
                sel_row = sel_rows[0] if sel_rows else None  # back-compat for Edit modal

                # ----- Add-new + (when selected) Edit/Delete/Share buttons --------
                if not sel_rows:
                    ab1, _ = st.columns([1, 5])
                    add_clicked = ab1.button(
                        "➕ Add new", width='stretch', key=f"add_{spec['table']}",
                    )
                    st.info(
                        "👆 Click one or more rows. Single select → Edit / Delete / "
                        "Share. Multi-select → Delete / Share (batch)."
                    )
                    edit_clicked = del_clicked = share_clicked = False
                elif is_multi:
                    st.success(
                        f"**{len(sel_rows)} rows selected.** Edit is disabled for "
                        "multi-select. Use Delete or Share to act on all of them."
                    )
                    ab1, ab3, ab4, _ = st.columns([1, 1, 1, 5])
                    add_clicked = ab1.button("➕ Add", width='stretch', key=f"add_{spec['table']}")
                    edit_clicked = False
                    del_clicked = ab3.button(
                        f"🗑 Delete {len(sel_rows)}", width='stretch',
                        key=f"del_{spec['table']}",
                    )
                    share_clicked = ab4.button(
                        f"📤 Share {len(sel_rows)}", width='stretch',
                        key=f"share_{spec['table']}",
                    )
                else:
                    pretty = " — ".join(
                        str(sel_row.get(c))[:60]
                        for c in display_cols[:2] if sel_row.get(c) not in (None, "")
                    )
                    st.success(f"Selected: **{pretty}** · id `{sel_row.get('id')}`")
                    ab1, ab2, ab3, ab4, _ = st.columns([1, 1, 1, 1, 4])
                    add_clicked = ab1.button("➕ Add", width='stretch', key=f"add_{spec['table']}")
                    edit_clicked = ab2.button("✏ Edit", width='stretch', key=f"edit_{spec['table']}")
                    del_clicked = ab3.button("🗑 Delete", width='stretch', key=f"del_{spec['table']}")
                    share_clicked = ab4.button("📤 Share", width='stretch', key=f"share_{spec['table']}")

                # ----- Modal helpers ---------------------------------------------
                def _to_date(v):
                    if v is None or v == "":
                        return None
                    try:
                        if pd.isna(v):
                            return None
                    except (TypeError, ValueError):
                        pass
                    try:
                        ts = pd.to_datetime(v, errors="coerce")
                        return None if pd.isna(ts) else ts.date()
                    except Exception:
                        return None

                def _to_str(v) -> str:
                    if v is None:
                        return ""
                    try:
                        if pd.isna(v):
                            return ""
                    except (TypeError, ValueError):
                        pass
                    return str(v)

                def _row_form(table: str, edit_fields, current: dict | None) -> dict | None:
                    """Render input fields. Returns payload dict on save, None otherwise."""
                    payload_widgets: dict[str, Any] = {}
                    for col, kind, label in edit_fields:
                        key = f"form_{table}_{col}"
                        cur = (current or {}).get(col)
                        if kind == "date":
                            payload_widgets[col] = st.date_input(label, value=_to_date(cur), key=key)
                        elif kind == "area":
                            payload_widgets[col] = st.text_area(label, value=_to_str(cur), height=80, key=key)
                        elif kind == "bool":
                            payload_widgets[col] = st.checkbox(label, value=bool(cur), key=key)
                        else:
                            payload_widgets[col] = st.text_input(label, value=_to_str(cur), key=key)

                    sc, cc = st.columns([1, 1])
                    save = sc.button("💾 Save", type="primary", width='stretch',
                                      key=f"savebtn_{table}_{'edit' if current else 'add'}")
                    cancel = cc.button("Cancel", width='stretch',
                                        key=f"cancelbtn_{table}_{'edit' if current else 'add'}")
                    if cancel:
                        st.rerun()
                    if not save:
                        return None
                    # Build payload — drop empty strings, ISO-format dates
                    out: dict[str, Any] = {}
                    for col, kind, _label in edit_fields:
                        v = payload_widgets[col]
                        if kind == "date":
                            out[col] = v.isoformat() if v else None
                        elif kind == "bool":
                            out[col] = bool(v)
                        else:
                            s = (v or "").strip()
                            out[col] = s or None
                    return out

                # ----- Edit modal -------------------------------------------------
                @st.dialog(f"Edit {pick[:-1]}", width="large")
                def _edit_modal(row: dict) -> None:
                    st.caption(f"id `{row.get('id')}` · source `{row.get('source', '—')}` · external_id `{row.get('external_id', '—')}`")
                    payload = _row_form(spec["table"], spec["edit_fields"], row)
                    if payload is None:
                        return
                    try:
                        sb.table(spec["table"]).update(payload).eq("id", row["id"]).execute()
                        st.cache_data.clear()
                        st.toast(f"{pick[:-1]} updated", icon="✅")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Update failed: {exc}")

                # ----- Add modal --------------------------------------------------
                @st.dialog(f"Add new {pick[:-1]}", width="large")
                def _add_modal() -> None:
                    st.caption("App-only row — `source` will be set to 'app'. Excel sync will not touch it.")
                    payload = _row_form(spec["table"], spec["edit_fields"], None)
                    if payload is None:
                        return
                    payload["source"] = "app"
                    try:
                        sb.table(spec["table"]).insert(payload).execute()
                        st.cache_data.clear()
                        st.toast(f"{pick[:-1]} added", icon="✅")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Insert failed: {exc}")

                # ----- Delete confirm modal (single or batch) --------------------
                @st.dialog(f"Delete from {pick}?")
                def _del_modal(rows: list[dict]) -> None:
                    n = len(rows)
                    if n == 1:
                        row = rows[0]
                        st.warning(
                            f"This will permanently delete row `{row.get('id')}` "
                            f"from `{spec['table']}`. This cannot be undone."
                        )
                        pretty_summary = "\n".join(
                            f"- **{spec['col_labels'].get(c, c)}**: {row.get(c)}"
                            for c in spec["table_cols"][:5] if row.get(c) is not None
                        )
                        st.markdown(pretty_summary)
                    else:
                        st.warning(
                            f"This will permanently delete **{n} rows** from "
                            f"`{spec['table']}`. This cannot be undone."
                        )
                        preview = rows[:12]
                        for r in preview:
                            label = " — ".join(
                                str(r.get(c))[:60]
                                for c in spec["table_cols"][:2]
                                if r.get(c) not in (None, "")
                            )
                            st.markdown(f"- `{r.get('id')}` — {label}")
                        if n > len(preview):
                            st.markdown(f"_… and {n - len(preview)} more_")

                    dc1, dc2 = st.columns([1, 1])
                    if dc1.button(
                        f"🗑 Yes, delete {n}" if n > 1 else "🗑 Yes, delete",
                        type="primary", width='stretch',
                        key=f"confirmdel_{spec['table']}",
                    ):
                        try:
                            ids = [r["id"] for r in rows if r.get("id")]
                            sb.table(spec["table"]).delete().in_("id", ids).execute()
                            st.cache_data.clear()
                            st.toast(f"Deleted {n} row(s)", icon="🗑")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Delete failed: {exc}")
                    if dc2.button("Cancel", width='stretch',
                                   key=f"canceldel_{spec['table']}"):
                        st.rerun()

                # ----- Share modal (single or batch) -----------------------------
                @st.dialog(f"Share — {pick}")
                def _share_modal(rows: list[dict], full_df: pd.DataFrame) -> None:
                    n = len(rows)
                    if n == 1:
                        st.markdown("#### Selected row")
                        st.json({k: rows[0].get(k) for k in spec["table_cols"] if k in rows[0]})
                    else:
                        st.markdown(f"#### {n} rows selected")
                        with st.expander(f"View list ({n})", expanded=False):
                            for r in rows:
                                label = " — ".join(
                                    str(r.get(c))[:60]
                                    for c in spec["table_cols"][:2]
                                    if r.get(c) not in (None, "")
                                )
                                st.markdown(f"- `{r.get('id')}` — {label}")

                    st.markdown("#### Download")
                    # CSV of the SELECTED rows (preferred over the whole filtered
                    # view when the user explicitly multi-selected).
                    sel_df = clean_df(pd.DataFrame(rows))
                    buf = StringIO()
                    sel_df.to_csv(buf, index=False)
                    st.download_button(
                        f"⬇ Download selected ({n}) as CSV",
                        data=buf.getvalue(),
                        file_name=f"{spec['table']}_{n}_selected_{date.today().isoformat()}.csv",
                        mime="text/csv",
                        width='stretch',
                        key=f"dl_sel_{spec['table']}",
                    )
                    # Secondary download: the full filtered view, in case the user
                    # wants the whole table after seeing the action UI.
                    buf2 = StringIO()
                    full_df.to_csv(buf2, index=False)
                    st.download_button(
                        f"⬇ Or download the full filtered view ({len(full_df)} rows)",
                        data=buf2.getvalue(),
                        file_name=f"{spec['table']}_filtered_{date.today().isoformat()}.csv",
                        mime="text/csv",
                        width='stretch',
                        key=f"dl_full_{spec['table']}",
                    )

                    st.markdown("#### Copy as markdown")
                    blocks = []
                    for r in rows:
                        md_lines = [
                            f"**{spec['col_labels'].get(c, c)}**: {r.get(c)}"
                            for c in spec["table_cols"] if r.get(c) is not None
                        ]
                        blocks.append("\n".join(md_lines))
                    st.code("\n\n---\n\n".join(blocks), language="markdown")

                # ----- Trigger the modals ----------------------------------------
                if add_clicked:
                    _add_modal()
                if edit_clicked and sel_row and not is_multi:
                    _edit_modal(sel_row)
                if del_clicked and sel_rows:
                    _del_modal(sel_rows)
                if share_clicked and sel_rows:
                    _share_modal(sel_rows, fdf)


    with _rtab:
        # Maintenance / backup / destructive-reset tools are DEVELOPER-SUPER-ONLY. They
        # export and (in the danger zone) delete SHARED data (auto-scanned rows, seen
        # ledger, scan logs), so a regular admin — even a client-tenant super_user — can't
        # reach them. Info notice instead of st.stop() (which would blank the sibling
        # Settings tabs, since every tab renders in one script pass).
        if not _dev_super:
            st.info(
                "🔒 **Maintenance & backup tools are restricted to a developer-tenant "
                "Super User.** They export and (in the danger zone) permanently delete "
                "shared platform data, so they're not part of day-to-day admin. If "
                "something needs cleaning up or backing up, contact the app owner.")
        else:
            _render_maintenance(sb, user)

if tab_sources is not None:
    with tab_sources:
        _cat_tab, _vreg_tab, _blk_tab = st.tabs(
            ["Validated", "Verify Registry", "Blocked"])

else:
    _cat_tab = _vreg_tab = _blk_tab = None

# Verify Registry — classify hosts (aggregator vs primary) + push to the catalog.
# Moved here from Records → Verify so all source-catalogue management lives under Sources.
if _vreg_tab is not None:
    with _vreg_tab:
        from views.verification import _render_source_registry
        _render_source_registry(user)
if _cat_tab is not None:
    with _cat_tab:
        st.subheader("Validated sources catalog")
        st.caption(
            "Curated per-source funding URLs. The Friday scan + manual scan iterate "
            "over every **active** row here, in addition to the keyword-wide sources "
            "in `config/sources.yaml`. **New sources are added in the Verify Registry "
            "tab**, then pushed here (single point of entry). Select rows to edit "
            "or delete. (Download the grid as CSV via its built-in ⤓ icon.)"
        )
        # The catalog is a SHARED, cross-tenant developer resource (every tenant's scan
        # crawls it), so direct edits are limited to a developer-tenant Super User. A
        # non-developer PROPOSES a change for review instead (Phase B suggestion queue).
        from core import suggestions as _suggestions
        _can_edit_sources = _dev_super
        _can_suggest_sources = _suggestions.can_suggest(user)
        if not _can_edit_sources:
            st.info(
                "🔒 Read-only. The sources catalog is shared across all tenants, so edits "
                "are limited to a developer-tenant Super User. Propose additions/changes via "
                "**💡 Suggest** below or the Verify Registry tab.")

        _METHODS = ["html", "html_js", "rss", "rest_json", "manual"]  # scan dispatch
        # Unified "Method" dropdown — SAME labels as the Verify Registry tab, each
        # mapped to a scan dispatch value (donor_sources.scrape_method).
        _METHOD_LABELS = {"API": "rest_json", "RSS / feed": "rss", "Page crawl": "html",
                          "JS page crawl": "html_js", "Manual": "manual"}
        _METHOD_LABEL_OPTS = list(_METHOD_LABELS)
        _METHOD_TO_LABEL = {v: k for k, v in _METHOD_LABELS.items()}
        # Shared taxonomy vocab (single source of truth — defined in the registry view).
        from views.verification import _SRC_OPTS, _ACCESS_OPTS, _TYPE_OPTS

        @st.cache_data(ttl=15)
        def _donors(scope: str) -> pd.DataFrame:
    # `scope` is the tenant discriminator for this PROCESS-GLOBAL cache; it is unused in
    # the body on purpose. Never rename it with a leading underscore — Streamlit drops
    # underscore-prefixed args from the key. See core.cache_scope.
            try:
                res = safe_execute(get_client().table("donor_sources").select("*")
                                   .order("donor_name"))
            except Exception as exc:
                st.warning(f"Couldn't load donor sources right now (network issue): {exc}")
                return pd.DataFrame()
            return clean_df(pd.DataFrame(res.data or []))

        def _import_from_config() -> None:
            """Copy config/sources.yaml entries into donor_sources, skipping any
            already present (matched by donor_name OR rfp_listing_url)."""
            from pathlib import Path as _P
            import yaml as _yaml
            _yaml_path = (_P(__file__).resolve().parent.parent
                          / "config" / "sources.yaml")
            with _yaml_path.open(encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
            existing = (sb.table("donor_sources")
                        .select("donor_name,rfp_listing_url").execute().data or [])
            existing_names = {(r.get("donor_name") or "").strip().lower() for r in existing}
            existing_urls = {(r.get("rfp_listing_url") or "").strip().lower() for r in existing}
            to_insert, skipped = [], []
            for s in (_cfg.get("sources", []) or []):
                name = (s.get("name") or "").strip()
                url = (s.get("url") or "").strip()
                method = (s.get("method") or "html").strip()
                if not name or not url:
                    continue
                if name.lower() in existing_names or url.lower() in existing_urls:
                    skipped.append(name)
                    continue
                code = name.split("-")[0].split("(")[0].strip().split()[0][:12]
                to_insert.append({
                    "donor_name": name, "donor_code": code, "rfp_listing_url": url,
                    "scrape_method": method if method in _METHODS else "html",
                    "notes": s.get("note") or
                        f"Imported from sources.yaml on {date.today().isoformat()}",
                    "is_active": True, "created_by": user.get("email"),
                })
            if to_insert:
                sb.table("donor_sources").insert(to_insert).execute()
            st.cache_data.clear()
            st.toast(f"Imported {len(to_insert)} new source(s); skipped "
                     f"{len(skipped)} already present.", icon="📥")

        # ----- Add / Edit / Delete dialogs --------------------------------------
        @st.dialog("Add funding source", width="large")
        def _add_source_dialog():
            with st.form("add_donor_source_form", clear_on_submit=False):
                c1, c2 = st.columns(2)
                a_name = c1.text_input("Donor name *")
                a_code = c2.text_input("Donor code (e.g. BMGF)")
                c3, c4 = st.columns([3, 1])
                a_url = c3.text_input("RFP listing URL *")
                a_method = c4.selectbox("Method", _METHOD_LABEL_OPTS,
                                        index=_METHOD_LABEL_OPTS.index("Page crawl"))
                c5, c6 = st.columns(2)
                a_sc = c5.selectbox("Source class", _SRC_OPTS,
                                    index=_SRC_OPTS.index("Primary source"))
                a_access = c6.selectbox("Access", _ACCESS_OPTS,
                                        index=_ACCESS_OPTS.index("Free"))
                a_base = st.text_input("Base URL (optional)")
                a_notes = st.text_area("Notes", height=80)
                a_active = st.checkbox("Active", value=True)
                bc1, bc2 = st.columns(2)
                ok = bc1.form_submit_button("➕ Add", type="primary",
                                            width='stretch')
                cancel = bc2.form_submit_button("Cancel", width='stretch')
            if cancel:
                st.rerun()
            if ok:
                if not a_name.strip() or not a_url.strip():
                    st.error("Donor name and listing URL are required.")
                    return
                try:
                    sb.table("donor_sources").insert({
                        "donor_name": a_name.strip(),
                        "donor_code": a_code.strip() or None,
                        "base_url": a_base.strip() or None,
                        "rfp_listing_url": a_url.strip(),
                        "scrape_method": _METHOD_LABELS[a_method],
                        "source_class": a_sc,
                        "access_model": a_access,
                        "notes": a_notes.strip() or None,
                        "is_active": bool(a_active),
                        "created_by": user.get("email"),
                    }).execute()
                    st.cache_data.clear()
                    st.toast(f"Added {a_name.strip()}", icon="✅")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not add: {exc}")

        @st.dialog("Edit funding source", width="large")
        def _edit_source_dialog(_row):
            with st.form("edit_donor_source_form"):
                c1, c2 = st.columns(2)
                e_name = c1.text_input("Donor name *", value=_row.get("donor_name") or "")
                e_code = c2.text_input("Donor code", value=_row.get("donor_code") or "")
                c3, c4 = st.columns([3, 1])
                e_url = c3.text_input("RFP listing URL *",
                                      value=_row.get("rfp_listing_url") or "")
                _lbl = _METHOD_TO_LABEL.get(_row.get("scrape_method"), "Page crawl")
                e_method = c4.selectbox("Method", _METHOD_LABEL_OPTS,
                                        index=_METHOD_LABEL_OPTS.index(_lbl))
                c5, c6 = st.columns(2)
                _scv = (_row.get("source_class") if _row.get("source_class") in _SRC_OPTS
                        else "Primary source")
                e_sc = c5.selectbox("Source class", _SRC_OPTS,
                                    index=_SRC_OPTS.index(_scv))
                _acv = (_row.get("access_model") if _row.get("access_model") in _ACCESS_OPTS
                        else "Free")
                e_access = c6.selectbox("Access", _ACCESS_OPTS,
                                        index=_ACCESS_OPTS.index(_acv))
                e_base = st.text_input("Base URL", value=_row.get("base_url") or "")
                e_notes = st.text_area("Notes", value=_row.get("notes") or "", height=80)
                e_active = st.checkbox("Active", value=bool(_row.get("is_active")))
                bc1, bc2 = st.columns(2)
                ok = bc1.form_submit_button("💾 Save", type="primary",
                                            width='stretch')
                cancel = bc2.form_submit_button("Cancel", width='stretch')
            if cancel:
                st.rerun()
            if ok:
                if not e_name.strip() or not e_url.strip():
                    st.error("Donor name and listing URL are required.")
                    return
                try:
                    sb.table("donor_sources").update({
                        "donor_name": e_name.strip(),
                        "donor_code": e_code.strip() or None,
                        "base_url": e_base.strip() or None,
                        "rfp_listing_url": e_url.strip(),
                        "scrape_method": _METHOD_LABELS[e_method],
                        "source_class": e_sc,
                        "access_model": e_access,
                        "notes": e_notes.strip() or None,
                        "is_active": bool(e_active),
                    }).eq("id", _row.get("id")).execute()
                    st.cache_data.clear()
                    st.toast(f"Updated {e_name.strip()}", icon="✅")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save: {exc}")

        @st.dialog("Delete funding sources", width="medium")
        def _delete_sources_dialog(_ids, _names):
            st.error(f"Permanently delete **{len(_ids)}** funding source(s)? "
                     f"This cannot be undone.")
            st.markdown("\n".join(f"- {n}" for n in _names[:12])
                        + ("\n- …" if len(_names) > 12 else ""))
            bc1, bc2 = st.columns(2)
            if bc1.button("🗑 Delete", type="primary", width='stretch',
                          key="ds_del_confirm"):
                try:
                    sb.table("donor_sources").delete().in_("id", _ids).execute()
                    st.cache_data.clear()
                    st.toast(f"Deleted {len(_ids)} source(s)", icon="🗑️")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Delete failed: {exc}")
            if bc2.button("Cancel", width='stretch', key="ds_del_cancel"):
                st.rerun()

        # ----- Suggest a change (non-developers) --------------------------------
        @st.dialog("Suggest a source change", width="large")
        def _suggest_source_change_dialog(_row=None):
            """Non-developer proposes an add (_row=None) or edit to a shared source. Reuses the
            same field set as the add/edit dialogs, but files a suggestion instead of writing."""
            _is_add = _row is None
            _row = _row or {}
            st.caption("Your proposal goes to a developer Super User to review & apply — "
                       "the shared catalog isn't changed directly.")
            with st.form("suggest_source_form"):
                c1, c2 = st.columns(2)
                s_name = c1.text_input("Donor name *", value=_row.get("donor_name") or "")
                s_code = c2.text_input("Donor code", value=_row.get("donor_code") or "")
                c3, c4 = st.columns([3, 1])
                s_url = c3.text_input("RFP listing URL *", value=_row.get("rfp_listing_url") or "")
                _lbl = _METHOD_TO_LABEL.get(_row.get("scrape_method"), "Page crawl")
                s_method = c4.selectbox("Method", _METHOD_LABEL_OPTS,
                                        index=_METHOD_LABEL_OPTS.index(_lbl))
                c5, c6 = st.columns(2)
                _scv = (_row.get("source_class") if _row.get("source_class") in _SRC_OPTS
                        else "Primary source")
                s_sc = c5.selectbox("Source class", _SRC_OPTS, index=_SRC_OPTS.index(_scv))
                _acv = (_row.get("access_model") if _row.get("access_model") in _ACCESS_OPTS
                        else "Free")
                s_access = c6.selectbox("Access", _ACCESS_OPTS, index=_ACCESS_OPTS.index(_acv))
                s_base = st.text_input("Base URL", value=_row.get("base_url") or "")
                s_notes = st.text_area("Notes", value=_row.get("notes") or "", height=80)
                s_active = st.checkbox("Active", value=bool(_row.get("is_active", True)))
                s_rat = st.text_area("Why this change? (optional)", height=70)
                bc1, bc2 = st.columns(2)
                ok = bc1.form_submit_button("💡 Submit suggestion", type="primary",
                                            width='stretch')
                cancel = bc2.form_submit_button("Cancel", width='stretch')
            if cancel:
                st.rerun()
            if ok:
                if not s_name.strip() or not s_url.strip():
                    st.error("Donor name and listing URL are required.")
                    return
                _proposed = {
                    "donor_name": s_name.strip(),
                    "donor_code": s_code.strip() or None,
                    "base_url": s_base.strip() or None,
                    "rfp_listing_url": s_url.strip(),
                    "scrape_method": _METHOD_LABELS[s_method],
                    "source_class": s_sc,
                    "access_model": s_access,
                    "notes": s_notes.strip() or None,
                    "is_active": bool(s_active),
                }
                _base = {k: _row.get(k) for k in _proposed}
                _diff, _snap = _suggestions.diff_of(_proposed, _base, allowed=_DS_EDITABLE) \
                    if not _is_add else (_proposed, {})
                if not _diff:
                    st.warning("No changes to suggest — edit a field first.")
                    return
                try:
                    _suggestions.create_suggestion(
                        resource_type="donor_sources",
                        target_id=(None if _is_add else _row.get("id")),
                        proposed_diff=_diff, base_snapshot=_snap,
                        rationale=(s_rat.strip() or None),
                        target_label=s_name.strip(), user=user)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not submit suggestion: {exc}")
                    return
                st.toast("💡 Suggestion submitted for review", icon="💡")
                st.rerun()

        _DS_EDITABLE = _suggestions._DS_EDITABLE

        # ----- Top action bar (right-aligned). "Add" lives in the registry now,
        # so the catalogue only Imports (from yaml) + Refreshes. Non-developers get a
        # "Suggest new source" entry instead of the developer-only Import. ------------
        if _can_edit_sources:
            _tsp, t2, t3 = st.columns([6, 1.6, 1])
            if t2.button("📥 Import from config", width='stretch', key="ds_import_top"):
                try:
                    _import_from_config()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Import failed: {exc}")
        elif _can_suggest_sources:
            _tsp, t2, t3 = st.columns([6, 1.8, 1])
            if t2.button("💡 Suggest source", width='stretch', key="ds_suggest_add",
                         help="Propose a new funding source for a developer Super User to review."):
                _suggest_source_change_dialog(None)
        else:
            _tsp, t3 = st.columns([7.6, 1])
        if t3.button("🔄 Refresh", width='stretch', key="ds_refresh_top"):
            st.cache_data.clear()
            st.rerun()

        # ----- Selectable table -------------------------------------------------
        ddf = _donors(scope_key())
        if ddf.empty:
            st.info("No sources yet — add them in the **Verify Registry** tab, "
                    "then push to this catalogue.")
        else:
            ids = ddf["id"].tolist()
            _n_total = len(ddf)
            _n_active = int(ddf["is_active"].sum()) if "is_active" in ddf else _n_total
            # Friendly Method label (matches the Verify registry vocabulary).
            ddf["method_label"] = ddf["scrape_method"].map(
                lambda m: _METHOD_TO_LABEL.get(m, m))
            # source_uid (migration 043) leads the table when present.
            uid_col = ["source_uid"] if "source_uid" in ddf.columns else []
            base_cols = uid_col + ["donor_name", "donor_code", "rfp_listing_url",
                                   "method_label"]
            # Access + Source class (migration 037) shown when present.
            extra = [c for c in ("source_class", "access_model") if c in ddf.columns]
            # Solicitation / Instrument types (display as joined strings).
            type_cols = []
            for _c in ("solicitation_types", "instrument_types"):
                if _c in ddf.columns:
                    ddf[_c + "_disp"] = ddf[_c].map(
                        lambda v: "; ".join(v) if isinstance(v, (list, tuple)) else (v or ""))
                    type_cols.append(_c + "_disp")
            disp = ddf[base_cols + extra + type_cols + ["is_active", "last_scraped_at",
                                                        "last_scrape_status", "notes"]].copy()
            st.markdown(f"**{_n_total}** sources · **{_n_active}** active")
            sel = st.dataframe(
                disp, hide_index=True, width='stretch',
                selection_mode="multi-row", on_select="rerun", key="ds_table",
                column_config={
                    "source_uid": st.column_config.NumberColumn("ID", width="small"),
                    "donor_name": st.column_config.TextColumn("Source Name"),
                    "donor_code": st.column_config.TextColumn("Code", width="small"),
                    "rfp_listing_url": st.column_config.LinkColumn("Host"),
                    "method_label": st.column_config.TextColumn("Method", width="small"),
                    "source_class": st.column_config.TextColumn("Source class"),
                    "access_model": st.column_config.TextColumn("Access", width="small"),
                    "solicitation_types_disp": st.column_config.TextColumn("Solicitation"),
                    "instrument_types_disp": st.column_config.TextColumn("Instrument"),
                    "is_active": st.column_config.CheckboxColumn("Active", width="small"),
                    "last_scraped_at": st.column_config.DatetimeColumn(
                        "Last scan", format="YYYY-MM-DD HH:mm"),
                    "last_scrape_status": st.column_config.TextColumn("Last status"),
                    "notes": st.column_config.TextColumn("Notes"),
                },
            )
            picked = (getattr(sel, "selection", None) or {}).get("rows") or []
            picked = [i for i in picked if 0 <= i < len(ids)]
            sel_ids = [ids[i] for i in picked]
            sel_rows = [ddf.iloc[i].to_dict() for i in picked]
            sel_names = [r.get("donor_name") or "(unnamed)" for r in sel_rows]

            st.caption(f"**{len(picked)} of {_n_total}** selected." if picked else
                       "Tick rows to edit or delete.")

            a1, a2, a3, _asp = st.columns([1, 1, 1.4, 4.6])
            if a1.button("✏️ Edit", width='stretch', key="ds_edit_btn",
                         disabled=(not _can_edit_sources) or len(picked) != 1,
                         help="Select exactly one row to edit." if _can_edit_sources
                              else "Developer Super User only."):
                _edit_source_dialog(sel_rows[0])
            if a2.button("🗑 Delete", width='stretch', key="ds_delete_btn",
                         disabled=(not _can_edit_sources) or not picked,
                         help=None if _can_edit_sources else "Developer Super User only."):
                _delete_sources_dialog(sel_ids, sel_names)
            # Non-developers propose an edit to the selected row instead of editing it.
            if not _can_edit_sources and _can_suggest_sources:
                if a3.button("💡 Suggest edit", width='stretch', key="ds_suggest_edit",
                             disabled=len(picked) != 1,
                             help="Propose changes to the selected source for review."
                                  if len(picked) == 1 else "Select exactly one row."):
                    _suggest_source_change_dialog(sel_rows[0])

# -----------------------------------------------------------------------------
# Tab 2 — Manual Scan
# -----------------------------------------------------------------------------
with tab_scan:
    st.subheader("Trigger a manual scan")
    from core.scan_runner import scannable_source_count as _src_count
    # WHO IS READING THIS. Extraction is a platform job on a store shared by every tenant,
    # runnable only from a developer tenant; describing it to an admin who cannot run it
    # (and showing them its counters) invited the reasonable question of why another
    # organisation's crawl was appearing in their account. Each role gets the page it can
    # actually act on.
    if _dev_admin:
        st.caption(
            "Two workflows. **⛏ Run Extraction** crawls every catalogued funding source "
            f"and extracts opportunities into the global store — a full run ({_src_count()} "
            "sources with detail-page + PDF + LLM enrichment) is the slow backend job "
            "(**~20-40 minutes**, no org screening). **🎯 Eligibility Scan** then "
            "screens that store against this organisation's eligibility (Settings → Scan "
            "eligibility & auto-scoring policies) — fast, no crawl. **📊 Sync Excel** (when "
            "a workbook is configured) imports the master workbook into **this tenant's** "
            "pipeline.")
    else:
        st.caption(
            "**🎯 Eligibility Scan** screens the curated funding store against this "
            "organisation's eligibility (Settings → Scan eligibility & auto-scoring "
            "policies) — fast, no crawl. The store itself is kept up to date centrally by "
            "the system administrator. **📊 Sync Excel** (when a workbook is configured) "
            "imports your master workbook into **this tenant's** pipeline.")

    from core.scan_pipeline import MATCH_RUN_LABEL
    from datetime import timedelta as _td

    def _pretty_trigger(raw: str | None) -> str:
        """Strip the audit prefix (manual:/extraction:/match:) so the display reads
        as the user's name. The DB keeps the prefixed value for audit."""
        if not raw:
            return "—"
        for _p in ("manual:", "extraction:", "match:"):
            if raw.startswith(_p):
                return raw.split(_p, 1)[1]
        return raw

    def _run_summary(rows: list[dict]) -> dict | None:
        """Aggregate the EXACT most-recent run within `rows` (newest-first). Every
        scan_logs row of one run shares a `run_id` (migration 065), so we group by the
        latest row's run_id — no cumulative bleed from an earlier run. Legacy rows with
        no run_id fall back to the old timestamp-gap heuristic (contiguous rows < 15 min
        apart = one run), stopping before any newer run_id'd run."""
        if not rows:
            return None
        latest = rows[0]
        trig = latest.get("triggered_by")
        rid = latest.get("run_id")
        if rid:
            grp = [r for r in rows if r.get("run_id") == rid]
        else:
            grp = [latest]
            prev_ts = pd.to_datetime(latest["scan_date"])
            for r in rows[1:]:
                if r.get("run_id"):           # don't merge legacy into a newer real run
                    break
                ts = pd.to_datetime(r["scan_date"])
                if (prev_ts - ts).total_seconds() > 900:   # >15-min gap → different run
                    break
                if r.get("triggered_by") == trig:
                    grp.append(r)
                prev_ts = ts
        return {
            "ts": latest["scan_date"][:16].replace("T", " "),
            "trigger": _pretty_trigger(trig),
            "found": sum(int(r.get("rfps_found") or 0) for r in grp),
            "new": sum(int(r.get("rfps_new") or 0) for r in grp),
            "rejected": sum(int(r.get("rfps_rejected") or 0) for r in grp),
        }

    try:
        _all_logs = (safe_execute(
            sb.table("scan_logs").select("*").order("scan_date", desc=True).limit(500)
        ).data or [])
    except Exception as exc:
        _all_logs = []
        st.warning(f"Couldn't load scan history (transient connection issue) — "
                   f"refresh to retry. ({type(exc).__name__})")
    _ext_rows = [r for r in _all_logs if r.get("source") != MATCH_RUN_LABEL]
    _match_rows = [r for r in _all_logs if r.get("source") == MATCH_RUN_LABEL]

    # Compact metric-card fonts so long values (e.g. the extraction timestamp)
    # show in full instead of truncating. Applies to BOTH summary-card rows
    # (extraction + Eligible funding history) — same st.metric testid.
    st.markdown(
        "<style>[data-testid='stMetricValue']{font-size:1.05rem;line-height:1.3;"
        "white-space:normal;overflow:visible;}"
        "[data-testid='stMetricLabel']{font-size:0.8rem;}</style>",
        unsafe_allow_html=True,
    )

    # Two SEPARATE workflows (DATA_SCHEMA_ETL.md §2-3):
    #   • Run Extraction      — crawl every donor source → extract into the global
    #     store. PURE extraction, NO org screening (extract_only=True). Slow.
    #   • Eligibility Scan — screen the INTERNAL store against this org
    #     (geography + MUST/PREFER) → the funding the org is potentially eligible
    #     for. Fast (no crawl). Tenant-facing version = the Pipeline "Scan now".
    #   • Sync Excel (optional) — import a master workbook into THIS tenant's
    #     pipeline; shown only when a workbook resolves.
    # Buttons sit ABOVE the summary cards. Each flips to a disabled "running…"
    # label in place while it works.
    _who = user.get("name") or user.get("email") or "admin"
    # The whole Admin page is admin-gated at the top (permissions.is_admin), so anyone here
    # is an admin of the acting tenant — Excel sync is open to ANY tenant admin (not just a
    # developer tenant), per the "admin from any tenant" requirement.
    is_admin = permissions.is_admin(user)
    # Excel workbook availability — the "📊 Sync Excel" button appears ONLY when a workbook
    # resolves for this deployment (optional feature; some tenants have no workbook). Sync is
    # now available to ANY tenant admin (was developer-only) and is TENANT-AWARE: rows land in
    # the acting tenant's pipeline (excel_sync passes the tenant to the importer's override).
    from core import excel_sync as _xls
    _xls_resolved = _xls.resolve_excel_path()
    _xls_path = _xls_resolved.get("resolved_path")
    _excel_available = _xls_path is not None
    try:
        from auth.tenant_context import current_tenant_id as _ctid
        _cur_tid = _ctid()
    except Exception:
        _cur_tid = None

    # Run Extraction + Eligibility Scan (+ Sync Excel when available) sit together on the
    # RIGHT — compact narrow columns, wide spacer on the left.
    # A disabled button is an invitation to ask why it is disabled. Extraction simply is
    # not part of this page for a client tenant, so it is not drawn at all.
    _show_excel = bool(_excel_available and is_admin)
    if _dev_admin and _show_excel:
        _bcsp, _bc1, _bc2, _bc3 = st.columns([3.6, 1.5, 1.7, 1.5])
        _ext_slot, _match_slot, _xls_slot = _bc1.empty(), _bc2.empty(), _bc3.empty()
    elif _dev_admin:
        _bcsp, _bc1, _bc2 = st.columns([5.2, 1.5, 1.6])
        _ext_slot, _match_slot, _xls_slot = _bc1.empty(), _bc2.empty(), None
    elif _show_excel:
        _bcsp, _bc2, _bc3 = st.columns([5.2, 1.7, 1.5])
        _ext_slot, _match_slot, _xls_slot = None, _bc2.empty(), _bc3.empty()
    else:
        _bcsp, _bc2 = st.columns([6.7, 1.7])
        _ext_slot, _match_slot, _xls_slot = None, _bc2.empty(), None
    # Run Extraction is a PLATFORM job that crawls every source into the SHARED global
    # store (no per-tenant screening) — a developer task. Restricted to an admin/super
    # of a developer tenant. The "Eligibility Scan" screening
    # button beside it stays available to every tenant admin (it's tenant-scoped).
    _do_extract = _ext_slot.button(
        "⛏ Run Extraction", type="secondary", key="admin_extract_btn", width='stretch',
        help="Platform job: crawl all funding sources and extract into the global "
             "Extracted Solicitations store. No org screening here. Slow, LLM-enriched "
             "(~20-40 min for a full run).") if _ext_slot is not None else False
    _do_match = _match_slot.button(
        "🎯 Eligibility Scan", type="primary", key="admin_match_btn", width='stretch',
        help="Screen the curated store against this org's eligibility (geography + "
             "MUST/PREFER) — the funding you're potentially eligible for. Fast.")
    _do_excel = (_xls_slot.button(
        "📊 Sync Excel", type="secondary", key="admin_excel_btn", width='stretch',
        help="Import the master Excel workbook into THIS tenant's pipeline (rows are "
             "tagged to your tenant). Optional — shown only when a workbook is configured.")
        if _xls_slot is not None else False)

    if _do_extract and _dev_admin:          # defense in depth (button is also disabled)
        # Replace the button in place with a disabled "running" label during the run.
        _ext_slot.button("⏳ Running extraction…", disabled=True, width='stretch',
                         key="admin_extract_running")
        try:
            from core.scan_runner import run_scan_now
            run_scan_now(triggered_by=f"extraction:{_who}", extract_only=True)
        except Exception as exc:
            st.session_state["admin_scan_banner"] = {
                "ok": False, "msg": f"❌ Extraction crashed: `{type(exc).__name__}: {exc}`."}
        st.rerun()

    if _do_match:
        _match_slot.button("⏳ Selecting eligible funding…", disabled=True,
                           width='stretch', key="admin_match_running")
        try:
            from core.scan_runner import run_screening_now
            run_screening_now(triggered_by=f"match:{_who}")
        except Exception as exc:
            st.session_state["admin_scan_banner"] = {
                "ok": False,
                "msg": f"❌ Eligibility Scan failed: `{type(exc).__name__}: {exc}`."}
        st.rerun()

    if _do_excel and _xls_slot is not None and is_admin:   # defense in depth
        _xls_slot.button("⏳ Syncing Excel…", disabled=True, width='stretch',
                         key="admin_excel_running")
        with st.spinner("Importing the workbook into this tenant…"):
            _res = _xls.sync(updated_by=user.get("email"), tenant_id=_cur_tid)
        st.session_state["admin_scan_banner"] = (
            {"ok": True, "msg": f"✓ Excel synced from `{_res.get('path')}` into this tenant."}
            if _res.get("ok") else
            {"ok": False, "msg": f"❌ Excel sync failed: {_res.get('error') or 'see output below'}"})
        st.session_state["admin_excel_output"] = _res
        st.rerun()

    # Banner from the previous run (survives the post-scan rerun).
    _scan_banner = st.session_state.pop("admin_scan_banner", None)
    if _scan_banner:
        (st.success if _scan_banner.get("ok") else st.error)(_scan_banner["msg"])

    # ----- Excel workbook: status + upload (any tenant admin) ------------------
    # Optional feature: a tenant admin can point this deployment at a master workbook
    # and sync it into THEIR pipeline. Shown to admins whether or not a workbook is
    # currently resolved, so a tenant with none can add one.
    if is_admin:
        _wb_label = (f"📊 Excel workbook — `{_xls_path.name}` ready"
                     if _excel_available else "📊 Excel workbook — none configured")
        with st.expander(_wb_label, expanded=False):
            _last_mt, _last_ts = _xls.get_last_sync()
            if _excel_available:
                # The full server path is developer detail; for a tenant upload the file
                # name is all that is meaningful, and printing the directory would expose
                # the storage layout.
                if _xls_resolved.get("source") == "tenant upload":
                    st.caption(f"Uploaded for this tenant → `{_xls_path.name}`")
                else:
                    st.caption(f"Resolved via **{_xls_resolved.get('source')}** → "
                               f"`{_xls_path}`")
                if _last_ts:
                    st.caption(f"Last synced: {_last_ts}")
                st.caption(
                    "Use the **📊 Sync Excel** button above to import into this tenant. "
                    "Rows are tagged to your current tenant.")
            else:
                if _xls_resolved.get("error"):
                    st.warning(_xls_resolved["error"])
                st.caption(
                    "No workbook for this tenant yet. Upload one below — it is stored "
                    "privately for this tenant and is not visible to any other.")
            _up = st.file_uploader("Upload a master workbook (.xlsx)", type=["xlsx"],
                                   key="admin_excel_upload")
            if _up is not None and st.button("Save workbook", key="admin_excel_save"):
                try:
                    # Stored under this tenant's own directory, never beside the project:
                    # a shared path made one organisation's workbook visible to every
                    # other tenant (see core.excel_sync).
                    from core.excel_sync import save_tenant_workbook as _save_wb
                    _dest = _save_wb(_up.name, _up.getbuffer().tobytes())
                    st.success(f"Saved `{_dest.name}` for this tenant. Reopen this tab, "
                               f"then Sync Excel.")
                    st.rerun()
                except Exception as _e:
                    st.error(f"Could not save workbook: {type(_e).__name__}: {_e}")
            _out = st.session_state.pop("admin_excel_output", None)
            if _out and (_out.get("stdout") or _out.get("stderr")):
                st.code((_out.get("stdout") or "") + "\n" + (_out.get("stderr") or ""),
                        language="text")

    # Extraction summary cards (BELOW the buttons) — DEVELOPER TENANT ONLY. These count a
    # crawl of the shared store; they are not this tenant's numbers, and showing them in a
    # client account read as another organisation's activity leaking in.
    _ext = _run_summary(_ext_rows)
    if _ext and not _dev_admin:
        # What a client tenant actually needs to know: the store behind their screening was
        # refreshed, by whom, and whether they have screened it since.
        _mine = _run_summary(_match_rows)
        _screened_since = bool(_mine and _mine["ts"] >= _ext["ts"])
        st.info(
            f"The funding store was last refreshed by the system administrator on "
            f"**{_ext['ts']}**."
            + (f"  \n✅ Your last **Eligibility Scan** ran on **{_mine['ts']}**, after "
               f"that refresh." if _screened_since else
               "  \n🎯 You have not screened it since — run an **Eligibility Scan** to see "
               "what this organisation is now eligible for."))
        _ext = None                      # fall through without drawing the crawl counters
    if _ext:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Last extraction", _ext["ts"])
        c2.metric("Triggered by", _ext["trigger"])
        c3.metric("Found", _ext["found"],
                  help="Candidates returned by the crawlers across all sources.")
        c4.metric("Extracted", _ext["new"],
                  help="Written to the global Extracted Solicitations store.")
        c5.metric("Rejected", _ext["rejected"],
                  help="Failed the extraction gate (not-an-rfp / off-theme / "
                       "opportunity-type / language / past-deadline).")

    # ----- History (split): Extraction runs (the crawl) vs Found-matches runs --
    from core.scan_pipeline import MATCH_RUN_LABEL
    st.markdown("---")
    # Reuse the rows already fetched above (`_all_logs`) instead of re-running the identical
    # scan_logs select — it is the same query, and Streamlit re-runs this whole tab body on
    # every widget interaction, so the duplicate cost a round-trip (~0.35s) every click.
    logs = clean_df(pd.DataFrame(_all_logs or []))
    if not logs.empty and "triggered_by" in logs.columns:
        logs["triggered_by"] = (
            logs["triggered_by"].fillna("").astype(str).map(_pretty_trigger)
        )
    if not logs.empty and "source" in logs.columns:
        _is_match = logs["source"].astype(str) == MATCH_RUN_LABEL
    else:
        _is_match = pd.Series([False] * len(logs), index=logs.index)
    extr_logs = logs[~_is_match] if not logs.empty else logs
    match_logs = logs[_is_match] if not logs.empty else logs

    # --- Extraction history (the donor-source crawl) — DEVELOPER TENANT ONLY ---
    # Same reasoning as the counters above: this is the log of a platform crawl over the
    # shared store, not a record of anything this tenant did. "Eligible funding history"
    # below IS per-tenant and stays visible to everyone.
    if _dev_admin:
        st.subheader("Extraction history")
        st.caption("Each donor-source crawl (“Run Extraction”). Most recent 500 runs.")
    if _dev_admin and extr_logs.empty:
        st.info("No extraction runs recorded yet.")
    elif _dev_admin:
        hist_cols = [
            "scan_date", "triggered_by", "source",
            "rfps_found", "rfps_new", "rfps_duplicate",
        ]
        if "rfps_rejected" in extr_logs.columns:
            hist_cols.append("rfps_rejected")
        hist_cols += ["duration_sec", "errors"]
        st.dataframe(
            extr_logs[[c for c in hist_cols if c in extr_logs.columns]],
            width='stretch',
            hide_index=True,
            column_config={
                "scan_date": st.column_config.TextColumn("Scan time"),
                "triggered_by": st.column_config.TextColumn("Triggered by"),
                "source": st.column_config.TextColumn("Source"),
                "rfps_found": st.column_config.NumberColumn("Found"),
                "rfps_new": st.column_config.NumberColumn("New"),
                "rfps_duplicate": st.column_config.NumberColumn("Dup"),
                "rfps_rejected": st.column_config.NumberColumn(
                    "Rejected",
                    help="Filtered out by the strict eligibility gate "
                         "(country / theme / deadline / feasibility).",
                ),
                "duration_sec": st.column_config.NumberColumn("Duration (s)", format="%.2f"),
                "errors": st.column_config.TextColumn("Errors"),
            },
        )

    # --- Eligible funding history (the fast internal re-screen) ---
    st.markdown("---")
    st.subheader("Eligible funding history")
    # Summary cards for the latest "My eligible funding" run.
    _mt = _run_summary(_match_rows)
    if _mt:
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Last run", _mt["ts"])
        m2.metric("Triggered by", _mt["trigger"])
        m3.metric("Considered", _mt["found"],
                  help="Curated solicitations screened against your org.")
        m4.metric("Eligible", _mt["new"],
                  help="Newly eligible for your org (passed geography + MUST/PREFER).")
        m5.metric("Not a fit", _mt["rejected"])
    st.caption("Each “My eligible funding” run — a fast internal screen of the "
               "curated store against this org's eligibility policies.")
    if match_logs.empty:
        st.info("No “My eligible funding” runs yet.")
    else:
        _mcols = ["scan_date", "triggered_by", "rfps_found", "rfps_new",
                  "rfps_duplicate", "rfps_rejected", "duration_sec"]
        st.dataframe(
            match_logs[[c for c in _mcols if c in match_logs.columns]],
            width='stretch',
            hide_index=True,
            column_config={
                "scan_date": st.column_config.TextColumn("Run time"),
                "triggered_by": st.column_config.TextColumn("Run by"),
                "rfps_found": st.column_config.NumberColumn(
                    "Considered",
                    help="Curated solicitations screened against your org."),
                "rfps_new": st.column_config.NumberColumn("Eligible"),
                "rfps_duplicate": st.column_config.NumberColumn("Already tracked"),
                "rfps_rejected": st.column_config.NumberColumn("Not a fit"),
                "duration_sec": st.column_config.NumberColumn("Duration (s)", format="%.2f"),
            },
        )


# -----------------------------------------------------------------------------
# Sources → Blocked (formerly the top-level "Blacklist" tab) — hard-reject URL
# substrings ("blocked tokens"). Distinct from Accounts → Blacklisted (users/tenants).
# -----------------------------------------------------------------------------
if _blk_tab is not None:
    with _blk_tab:
        from core import blacklist as _blmod

        st.subheader("Blocked tokens")
        # The scan blacklist is a SHARED, platform-wide reject list (it drops candidate URLs
        # for EVERY tenant's scan), so it's a developer-account feature — restricted to an
        # admin/super_user of a developer tenant.
        if not _dev_admin:
            st.info(
                "🔒 **Blocked tokens is a developer-account feature.** These patterns drop "
                "candidate URLs for every tenant's scan, so they're managed by a developer "
                "tenant, not per client tenant.")
        else:
            st.caption(
                "Each pattern is matched as a case-insensitive **substring of the "
                "candidate URL** during scanning. Any match → the link is rejected "
                "before scoring and never becomes a record. Use a bare domain "
                "(`cdc.gov`) to block a whole site, or a path fragment "
                "(`comicrelief.com/sportrelief`, `/donate`, `/careers`) to block a "
                "section. Edit cells, add rows (＋), then **Save**."
            )
            try:
                _bl_rows = (sb.table("scan_blacklist").select("pattern,reason")
                            .order("pattern").execute().data or [])
            except Exception as exc:
                _bl_rows = []
                st.warning(f"Couldn't load the blacklist — did you run migration 024? ({exc})")

            if _bl_rows:
                _bl_df = pd.DataFrame(_bl_rows)[["pattern", "reason"]]
            else:
                _bl_df = pd.DataFrame({"pattern": pd.Series(dtype="object"),
                                       "reason": pd.Series(dtype="object")})
            _bl_edited = st.data_editor(
                _bl_df, num_rows="dynamic", width='stretch', hide_index=True,
                key="blacklist_editor",
                column_config={
                    "pattern": st.column_config.TextColumn("Pattern (URL substring)", required=True),
                    "reason": st.column_config.TextColumn("Reason / note"),
                },
            )
            if st.button("💾 Save blacklist", type="primary", key="save_blacklist"):
                recs, seen = [], set()
                for _, r in _bl_edited.iterrows():
                    p = str(r.get("pattern") or "").strip().lower()
                    if not p or p in seen:
                        continue
                    seen.add(p)
                    recs.append({
                        "pattern": p,
                        "reason": (str(r.get("reason") or "").strip() or None),
                        "created_by": user.get("email"),
                    })
                try:
                    sb.table("scan_blacklist").delete().neq("id", -1).execute()  # replace-all
                    if recs:
                        sb.table("scan_blacklist").insert(recs).execute()
                    _blmod.clear_cache()
                    st.success(f"Saved {len(recs)} blacklist pattern(s).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Save failed: {exc}")


# Verify (human verification + feedback over the scan) now lives as a sub-tab
# under Records → Data | Verify | Reset (see `with tab_data:` above).


# -----------------------------------------------------------------------------
# Tab 9 — Learning data (ML Phase 1: captured rejects / decisions / feedback)
# -----------------------------------------------------------------------------
if tab_learning is not None:
    with tab_learning:
        st.subheader("Learning data — captured signals")
        # Learning data is the platform's shared training signal (the labeled set behind the
        # scoring model) — a developer resource. Restricted to members of a developer tenant
        #; client-tenant admins see a lock. _dev_member covers the
        # super_user (homed to the RFPIS platform tenant). Guarded body so sibling tabs still
        # render (no st.stop()).
        if not _dev_member:
            st.info(
                "🔒 **Learning data is a developer view.** These captured signals train the "
                "shared scoring model across all tenants, so they're available to members of "
                "a developer tenant.")
            st.stop()
        st.caption(
            "Every scan **reject**, human **decision** (Proceed/Park/Decline) and "
            "👍/👎 **feedback** is logged to `scan_decisions` — the labeled training "
            "set for the scoring model (ML Phase 2/3). Read-only here.")
        # TRUE counts come from server-side count='exact' queries — NOT from the fetched
        # display window. PostgREST caps a fetch at ~1000 rows, so when a recent scan floods
        # that window with fresh system_reject rows, counting over it badly undercounts older
        # feedback / human-decision signals (they fall outside the window). The signals are
        # NOT lost — only the window is capped — so the cards must count the whole table.
        @st.cache_data(ttl=30)
        def _ld_count(event_type: str | None = None, scope: str = "") -> int:
    # `scope` is the tenant discriminator for this PROCESS-GLOBAL cache; it is unused in
    # the body on purpose. Never rename it with a leading underscore — Streamlit drops
    # underscore-prefixed args from the key. See core.cache_scope.
            try:
                q = get_client().table("scan_decisions").select("id", count="exact")
                if event_type:
                    q = q.eq("event_type", event_type)
                return int(q.execute().count or 0)
            except Exception:
                return 0

        @st.cache_data(ttl=30)
        def _reject_reason_counts(scope: str) -> dict:
            """Accurate reason histogram over ALL system_reject rows (paginated label-only
            fetch), not just the recent display window."""
    # `scope` is the tenant discriminator for this PROCESS-GLOBAL cache; it is unused in
    # the body on purpose. Never rename it with a leading underscore — Streamlit drops
    # underscore-prefixed args from the key. See core.cache_scope.
            from collections import Counter as _Counter
            out, start, page = _Counter(), 0, 1000
            try:
                while True:
                    chunk = (get_client().table("scan_decisions").select("label")
                             .eq("event_type", "system_reject")
                             .range(start, start + page - 1).execute().data or [])
                    if not chunk:
                        break
                    out.update((r.get("label") or "—") for r in chunk)
                    if len(chunk) < page:
                        break
                    start += page
            except Exception:
                pass
            return dict(out)

        _total = _ld_count(scope=scope_key())
        try:
            _ld = (sb.table("scan_decisions").select("*")
                   .order("created_at", desc=True).limit(1000).execute().data or [])
        except Exception as exc:
            st.warning(f"Couldn't load scan_decisions — did you run migration 027? ({exc})")
            _ld = []
        if _total == 0:
            st.info("No signals captured yet. Run a scan, set a decision, or hit 👍/👎 "
                    "on a record — they'll appear here.")
        else:
            _ldf = pd.DataFrame(_ld)
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Total signals", _total)
            m2.metric("System rejects", _ld_count("system_reject", scope=scope_key()))
            m3.metric("Human decisions", _ld_count("human_decision", scope=scope_key()))
            m4.metric("👍/👎 feedback", _ld_count("feedback", scope=scope_key()))
            m5.metric("Reject verdicts", _ld_count("reject_verification", scope=scope_key()))
            with st.expander("Rejects by reason category", expanded=False):
                _rc = _reject_reason_counts(scope_key())
                if _rc:
                    _by = (pd.DataFrame(sorted(_rc.items(), key=lambda kv: -kv[1]),
                                        columns=["reason", "count"]))
                    st.dataframe(_by, hide_index=True, width='stretch')
                else:
                    st.caption("No rejects logged yet.")
            st.caption(f"Table below shows the **{len(_ldf)}** most recent of **{_total}** "
                       f"total signals (newest first). Counts above are the full-table totals.")
            _cols = [c for c in ["created_at", "event_type", "label", "reason",
                                 "opportunity_title", "funding_agency", "source",
                                 "call_submission_deadline", "alignment_score",
                                 "opportunity_link", "decided_by"]
                     if c in _ldf.columns]
            st.dataframe(
                _ldf[_cols], hide_index=True, width='stretch',
                column_config={
                    "opportunity_link": st.column_config.LinkColumn(
                        "Link", display_text="Open ↗"),
                })
            st.download_button(
                "⬇ Download CSV", _ldf[_cols].to_csv(index=False).encode("utf-8"),
                file_name="scan_decisions.csv", mime="text/csv")
