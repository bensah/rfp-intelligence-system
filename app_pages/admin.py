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
from db.supabase_client import get_client, safe_execute
from views.account_sections import render_manage_users, render_user_access
from views.org_setup import render_org_setup

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

(tab_settings, tab_users, tab_access, tab_data, tab_sources,
 tab_scan, tab_blacklist, tab_learning) = st.tabs(
    ["Setup", "Manage Users", "User Access", "Records", "Sources",
     "Manual Scan", "Blacklist", "Learning data"]
)

# User administration tabs — moved here from the old User page in the
# 2026-06-07 nav redesign (admin / super_user only; the page is already
# gated above). Logic lives in views/account_sections.py so the Profile
# page and these tabs share one implementation.
with tab_users:
    render_manage_users(user, sb)
with tab_access:
    render_user_access(user)


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

with tab_data:
    _dtab, _vtab, _rtab = st.tabs(["Data", "Verify", "Reset"])
    with _vtab:
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

        # --- Per-table specs (Found RFPs handled separately below) ---------------
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
            "Active Grants": {
                "table": "active_grants",
                "order_col": "report_due_date",
                "table_cols": ["grant_id", "donor_title", "award_date", "end_date",
                               "status", "owner", "report_type", "report_due_date",
                               "source"],
                "col_labels": {
                    "grant_id": "Grant ID", "donor_title": "Donor",
                    "award_date": "Awarded", "end_date": "Ends",
                    "status": "Status", "owner": "Owner",
                    "report_type": "Report", "report_due_date": "Report due",
                    "source": "Source",
                },
                "date_cols": ["award_date", "end_date", "report_due_date", "submitted_date"],
                "search_cols": ["grant_id", "donor_title", "owner", "remarks"],
                "advanced_filters": ["owner", "status", "report_type", "source"],
                "edit_fields": [
                    ("grant_id",        "text", "Grant ID *"),
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
                "caption": "Keyed on `grant_id`. Re-sync OVERWRITES matching rows from Excel.",
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

        _DATA_OPTIONS = ["Found RFPs"] + list(_DATA_SPECS.keys())
        pick = st.selectbox("Table", _DATA_OPTIONS, key="data_table_pick")

        # ----- FOUND RFPS branch — renders the master line-list inline ---------
        if pick == "Found RFPs":
            from core.render_view import render_view
            render_view("rfp_records")

        # ----- Auxiliary tables branch -------------------------------------------
        else:
            spec = _DATA_SPECS[pick]
            st.info(spec["caption"])

            @st.cache_data(ttl=15)
            def _fetch_table(table: str, order_col: str) -> pd.DataFrame:
                try:
                    res = (
                        get_client()
                        .table(table)
                        .select("*")
                        .order(order_col, desc=True)
                        .limit(2000)
                        .execute()
                    )
                    return pd.DataFrame(res.data or [])
                except Exception as exc:
                    st.error(f"Could not load {table}: {exc}")
                    return pd.DataFrame()

            df = _fetch_table(spec["table"], spec["order_col"])
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
                        # Keep rows with NULL dates — otherwise active_grants
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
                    sel_df = pd.DataFrame(rows)
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
        st.subheader("Duplicate flags")
        st.caption(
            "Re-runs the deduplicator with two safety rules: "
            "**(1)** the most *complete* row in each cluster wins as canonical "
            "(weighted by Progress = Completed, Donor Decision set, Decision = Proceed, "
            "Amount Requested / Date Completed populated, Submissions > 1), and "
            "**(2)** rows with Progress = Completed are never flagged as duplicates — "
            "they represent real donor submission events."
        )
        dc1, dc2 = st.columns([3, 1])
        if dc2.button("🔁 Reset & re-dedup", width='stretch'):
            from scripts.dedup_existing import run as run_dedup
            try:
                with st.spinner("Re-running dedup..."):
                    res = run_dedup(reset=True, preserve_completed=True)
                dc1.success(
                    f"Considered {res['considered']} canonical row(s) of {res['total_rows']} total · "
                    f"flagged **{res['flagged']}** as duplicate · "
                    f"skipped **{res['skipped_completed']}** Completed pair(s)."
                )
                with dc1.expander("Flagged rows", expanded=False):
                    for uid, canon, reason in res["updates"]:
                        st.markdown(f"- `{uid}` → dup of `{canon}` ({reason})")
            except Exception as exc:
                dc1.error(f"Re-dedup failed: {exc}")

        st.markdown("---")
        st.subheader("Reset Meeting Logs (one-time cleanup)")
        st.caption(
            "**Use only once**, to clean up duplicates from old syncs that ran "
            "before migration 006 added the `external_id` column. After this "
            "one-time wipe + sync, future syncs MERGE instead of replacing — "
            "Status toggles and other app edits are preserved automatically. "
            "Notes added via the app (source='app') are also deleted by this "
            "wipe, so prefer the **Other Records** tab to delete specific rows."
        )
        rc1, rc2 = st.columns([3, 1])
        if rc2.button("🧹 Wipe meeting_logs", width='stretch'):
            try:
                res = sb.table("meeting_logs").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
                deleted = len(res.data or [])
                rc1.success(
                    f"Deleted **{deleted}** rows from meeting_logs. "
                    "Run **🔄 Sync now** above to repopulate from Excel."
                )
            except Exception as exc:
                rc1.error(f"Wipe failed: {exc}")

        st.markdown("---")
        st.subheader("Wipe active_grants migration rows (one-time cleanup)")
        st.caption(
            "Deletes every row in `active_grants` where `source = 'migration'`. "
            "After running this once and re-syncing, the table will exactly "
            "mirror the Excel `Active_Grants_Log` sheet (no stragglers from "
            "earlier syncs). App-only rows you added via Admin → Data → Active "
            "Grants are NOT touched."
        )
        agc1, agc2 = st.columns([3, 1])
        if agc2.button("🧹 Wipe migration grants", width='stretch', key="wipe_ag_migration"):
            try:
                res = (
                    sb.table("active_grants")
                    .delete()
                    .eq("source", "migration")
                    .execute()
                )
                deleted = len(res.data or [])
                agc1.success(
                    f"Deleted **{deleted}** migration row(s). Click **🔄 Sync now** "
                    "above to repopulate from Excel."
                )
            except Exception as exc:
                agc1.error(f"Wipe failed: {exc}")

        st.markdown("---")
        st.subheader("Delete auto-scanned RFPs (rescan from scratch)")
        st.caption(
            "Removes every row in `rfp_submissions` where `source = 'auto'` — "
            "i.e. RFPs added by the scanner but **not** ones submitted manually "
            "(`source = 'manual'`) or imported from Excel (`source = 'migration'`). "
            "Use this after policy changes when you want a clean rescan instead "
            "of incremental merges. Click **🔄 Scan now** afterwards to repopulate."
        )
        arc1, arc2 = st.columns([3, 1])
        if arc2.button("🧹 Wipe auto-scan rows", width='stretch', key="wipe_auto_rfps"):
            try:
                res = sb.table("rfp_submissions").delete().eq("source", "auto").execute()
                _rows = res.data or []
                deleted = len(_rows)
                # This is an explicit "rescan from scratch" reset, so also clear
                # these uids from the permanent seen-ledger — otherwise the
                # tombstones would suppress the very rows you want re-found.
                # (Individual RFP deletes elsewhere intentionally KEEP their
                # tombstone, so a rejected opportunity never re-enters.)
                _purged = _purge_seen_ledger(sb, _rows)
                arc1.success(
                    f"Deleted **{deleted}** auto-scanned RFP(s)"
                    + (f" (and cleared {_purged} seen-ledger tombstone(s))" if _purged else "")
                    + ". Click 🔄 Scan now on the Screen tab (or in Manual Scan tab) to refresh."
                )
            except Exception as exc:
                arc1.error(f"Wipe failed: {exc}")

        st.markdown("---")
        st.subheader("Clear scan history")
        st.caption(
            "Removes every row in `scan_logs`. The Manual Scan tab's metric "
            "strip and scan history table will read empty afterwards. Does not "
            "touch RFP records — pair with **🧹 Wipe auto-scan rows** above if "
            "you want a totally clean slate for a fresh test scan."
        )
        sh1, sh2 = st.columns([3, 1])
        if sh2.button("🧹 Clear scan history", width='stretch', key="wipe_scan_logs"):
            try:
                res = (
                    sb.table("scan_logs")
                    .delete()
                    .neq("id", "00000000-0000-0000-0000-000000000000")
                    .execute()
                )
                deleted = len(res.data or [])
                sh1.success(
                    f"Deleted **{deleted}** scan log row(s). The Manual Scan tab "
                    "will show fresh data after your next scan."
                )
            except Exception as exc:
                sh1.error(f"Wipe failed: {exc}")

        st.markdown("---")
        st.subheader("🔁 Fresh-test reset (one-click clean slate)")
        st.caption(
            "Convenience button — wipes BOTH `rfp_submissions` rows where "
            "source='auto' AND all of `scan_logs` in one shot. Excel-imported "
            "rows (`source='migration'`) and manually-submitted rows "
            "(`source='manual'`) are preserved. Use this before testing a new "
            "policy configuration so previous scan noise doesn't muddy the view."
        )
        ft1, ft2 = st.columns([3, 1])
        if ft2.button("🔁 Reset for fresh test", type="secondary",
                       width='stretch', key="fresh_test_reset"):
            try:
                r1 = sb.table("rfp_submissions").delete().eq("source", "auto").execute()
                r2 = (
                    sb.table("scan_logs")
                    .delete()
                    .neq("id", "00000000-0000-0000-0000-000000000000")
                    .execute()
                )
                # Clear these uids from the permanent seen-ledger so the fresh
                # rescan can repopulate them (see "Wipe auto-scan rows" above).
                _purge_seen_ledger(sb, r1.data or [])
                ft1.success(
                    f"✓ Reset complete. Deleted **{len(r1.data or [])}** auto-scan "
                    f"RFP(s) and **{len(r2.data or [])}** scan log row(s). Click "
                    "**Manual Scan → ▶ Run scan now** for a clean test."
                )
            except Exception as exc:
                ft1.error(f"Reset failed: {exc}")


    # -----------------------------------------------------------------------------
    # Tab — Data (row-select + Edit/Delete/Share modals)
    #   First option = Found RFPs (jumps to the dedicated RFP Records page where
    #   the full 5-tab edit modal lives). Other options = auxiliary tables with
    #   the same row-select UX pattern.
    # -----------------------------------------------------------------------------
        st.markdown("---")
        st.subheader("🧽 Clear app cache")
        st.caption("Force-refresh cached data (records, dropdowns, settings) without a full restart.")
        if st.button("🧽 Clear cache", key="clear_app_cache_btn"):
            st.cache_data.clear()
            try:
                from core import settings as _s
                _s.clear_cache()
            except Exception:
                pass
            st.success("Cache cleared — data re-fetches on next interaction.")
            st.rerun()

with tab_sources:
    st.subheader("Donor sources catalog")
    st.caption(
        "Curated per-source funding URLs. The Friday scan + manual scan iterate "
        "over every **active** row here, in addition to the keyword-wide sources "
        "in `config/sources.yaml`. **New sources are added in Verify → Source "
        "registry**, then pushed here (single point of entry). Select rows to edit "
        "or delete. (Download the grid as CSV via its built-in ⤓ icon.)"
    )

    _METHODS = ["html", "html_js", "rss", "rest_json", "manual"]  # scan dispatch
    # Unified "Method" dropdown — SAME labels as the Verify > Source registry, each
    # mapped to a scan dispatch value (donor_sources.scrape_method).
    _METHOD_LABELS = {"API": "rest_json", "RSS / feed": "rss", "Page crawl": "html",
                      "JS page crawl": "html_js", "Manual": "manual"}
    _METHOD_LABEL_OPTS = list(_METHOD_LABELS)
    _METHOD_TO_LABEL = {v: k for k, v in _METHOD_LABELS.items()}
    # Shared taxonomy vocab (single source of truth — defined in the registry view).
    from views.verification import _SRC_OPTS, _ACCESS_OPTS, _TYPE_OPTS

    @st.cache_data(ttl=15)
    def _donors() -> pd.DataFrame:
        res = (get_client().table("donor_sources").select("*")
               .order("donor_name").execute())
        return pd.DataFrame(res.data or [])

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
    @st.dialog("Add donor source", width="large")
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

    @st.dialog("Edit donor source", width="large")
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

    @st.dialog("Delete donor sources", width="medium")
    def _delete_sources_dialog(_ids, _names):
        st.error(f"Permanently delete **{len(_ids)}** donor source(s)? "
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

    # ----- Top action bar (right-aligned). "Add" lives in the registry now,
    # so the catalogue only Imports (from yaml) + Refreshes. ------------------
    _tsp, t2, t3 = st.columns([6, 1.6, 1])
    if t2.button("📥 Import from config", width='stretch',
                 key="ds_import_top"):
        try:
            _import_from_config()
            st.rerun()
        except Exception as exc:
            st.error(f"Import failed: {exc}")
    if t3.button("🔄 Refresh", width='stretch', key="ds_refresh_top"):
        st.cache_data.clear()
        st.rerun()

    # ----- Selectable table -------------------------------------------------
    ddf = _donors()
    if ddf.empty:
        st.info("No sources yet — add them in **Verify → Source registry**, "
                "then push to this catalogue.")
    else:
        ids = ddf["id"].tolist()
        _n_total = len(ddf)
        _n_active = int(ddf["is_active"].sum()) if "is_active" in ddf else _n_total
        # Friendly Method label (matches the Verify registry vocabulary).
        ddf["method_label"] = ddf["scrape_method"].map(
            lambda m: _METHOD_TO_LABEL.get(m, m))
        base_cols = ["donor_name", "donor_code", "rfp_listing_url", "method_label"]
        # Access + Source class (migration 037) shown when present.
        extra = [c for c in ("source_class", "access_model") if c in ddf.columns]
        disp = ddf[base_cols + extra + ["is_active", "last_scraped_at",
                                        "last_scrape_status", "notes"]].copy()
        st.markdown(f"**{_n_total}** sources · **{_n_active}** active")
        sel = st.dataframe(
            disp, hide_index=True, width='stretch',
            selection_mode="multi-row", on_select="rerun", key="ds_table",
            column_config={
                "donor_name": st.column_config.TextColumn("Source Name"),
                "donor_code": st.column_config.TextColumn("Code", width="small"),
                "rfp_listing_url": st.column_config.LinkColumn("Host"),
                "method_label": st.column_config.TextColumn("Method", width="small"),
                "source_class": st.column_config.TextColumn("Source class"),
                "access_model": st.column_config.TextColumn("Access", width="small"),
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

        a1, a2, _asp = st.columns([1, 1, 6])
        if a1.button("✏️ Edit", width='stretch', key="ds_edit_btn",
                     disabled=len(picked) != 1,
                     help="Select exactly one row to edit."):
            _edit_source_dialog(sel_rows[0])
        if a2.button("🗑 Delete", width='stretch', key="ds_delete_btn",
                     disabled=not picked):
            _delete_sources_dialog(sel_ids, sel_names)

    # ----- Excel sync ------------------------------------------------------
    # The master workbook is a source in its own right (it seeds rfp_submissions
    # alongside the scanned donor sources above), so its sync controls live here
    # under Sources rather than on the Records tab.
    st.markdown("---")
    st.subheader("Excel sync")
    st.caption(
        "Pulls the master workbook into Supabase. Path comes from "
        "`EXCEL_SOURCE_PATH` in `.env` (or the local repo copy if unset). "
        "Auto-sync runs on page load when the file is newer than the last sync."
    )

    resolved = excel_sync.resolve_excel_path()
    xls_path = resolved.get("resolved_path")
    last_mtime, last_iso = excel_sync.get_last_sync()

    sc1, sc2 = st.columns([3, 1])
    with sc1:
        # Single line showing the active workbook path (was two lines before;
        # EXCEL_SOURCE_PATH and the resolved path were almost always identical).
        if xls_path:
            try:
                mt = xls_path.stat().st_mtime
                st.code(f"Active workbook: {xls_path}")
                st.caption(
                    f"File modified: {datetime.fromtimestamp(mt, tz=timezone.utc).isoformat(timespec='seconds')}  ·  "
                    f"Last sync: {last_iso or '(never)'}"
                )
                if last_mtime and last_mtime >= mt:
                    st.success("✓ In sync with the workbook")
                else:
                    st.warning("⚠ Workbook is newer than last sync — click to refresh")
            except OSError as exc:
                st.error(f"Can't read file: {exc}")
        else:
            st.error(
                "No Excel file found. Upload a workbook below, set "
                "`EXCEL_SOURCE_PATH` in `.env`, or drop the workbook in the "
                "repo root."
            )
        if resolved.get("error"):
            st.error(resolved["error"])

    if sc2.button("🔄 Sync now", type="primary", disabled=xls_path is None,
                  width='stretch'):
        with st.spinner("Running migrate_excel.py..."):
            result = excel_sync.sync(updated_by=user.get("email"))
        if result.get("ok"):
            st.success(f"Synced from {result['path']}")
        else:
            st.error(f"Sync failed: {result.get('error') or 'see stderr'}")
        with st.expander("Sync output", expanded=not result.get("ok")):
            st.code(result.get("stdout") or "(no stdout)", language="text")
            if result.get("stderr"):
                st.code(result.get("stderr"), language="text")
        st.rerun()

    # ----- Upload a replacement workbook ----------------------------------
    # Useful when the user is on a different machine where OneDrive / the
    # original path doesn't exist, or wants to ship a one-off updated file.
    # Saves to the currently-resolved path (overwriting), or to the repo
    # root if no path is resolvable yet.
    with st.expander("📤 Upload a new workbook (replaces the active file)", expanded=False):
        st.caption(
            "Pick a `.xlsx` file from your computer to replace whatever the "
            "app is currently reading. The uploaded file is saved to "
            "the path shown above (or to the repo root if no path is "
            "resolvable). Admin-only — when user policies land we'll gate this "
            "behind a per-user permission too."
        )
        up = st.file_uploader(
            "Choose a .xlsx file",
            type=["xlsx"],
            accept_multiple_files=False,
            key="excel_workbook_upload",
        )
        if up is not None:
            # Determine the destination. Prefer the currently-resolved path
            # (replaces in-place). Fall back to the repo root with the
            # uploaded filename.
            from pathlib import Path as _P
            dest = (
                xls_path
                if xls_path is not None
                else _P(__file__).resolve().parent.parent / up.name
            )
            ub1, ub2 = st.columns([1, 1])
            confirm = ub1.button(
                f"💾 Save as `{dest.name}` and replace active workbook",
                type="primary", key="confirm_upload_btn",
            )
            if ub2.button("Cancel upload", key="cancel_upload_btn"):
                st.session_state.pop("excel_workbook_upload", None)
                st.rerun()
            if confirm:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(up.getbuffer())
                    st.success(
                        f"✓ Saved to `{dest}`. The next sync will pick it up "
                        "automatically; click 🔄 Sync now above to refresh now."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save: {exc}")


# -----------------------------------------------------------------------------
# Tab 2 — Manual Scan
# -----------------------------------------------------------------------------
with tab_scan:
    st.subheader("Trigger a manual scan")
    from core.scan_runner import scannable_source_count as _src_count
    st.caption(
        "Scan every configured donor source for new RFPs that match this "
        "organisation's eligibility policies (Settings → Scan eligibility & "
        f"auto-scoring policies). A full run ({_src_count()} catalogued sources "
        "with detail-page + PDF enrichment) typically takes **3-8 minutes**."
    )

    try:
        last = (
            safe_execute(
                sb.table("scan_logs").select("*").order("scan_date", desc=True).limit(1)
            ).data
        )
    except Exception as exc:
        last = None
        st.warning(f"Couldn't load scan history (transient connection issue) — "
                   f"refresh to retry. ({type(exc).__name__})")
    def _pretty_trigger(raw: str | None) -> str:
        """Strip the audit prefix so the user-facing display reads as a name.
        DB still stores 'manual:<name>' for audit; we just hide the prefix
        in the UI. 'cron' / 'startup' / 'test' values are shown as-is."""
        if not raw:
            return "—"
        return raw.split("manual:", 1)[1] if raw.startswith("manual:") else raw

    if last:
        # Aggregate the WHOLE scan run, not just the last source. Each run
        # writes one row per source; we group everything within 5 minutes
        # of the latest row and sum the counts so the metrics reflect the
        # full scan outcome.
        from datetime import timedelta as _td
        latest_ts = pd.to_datetime(last[0]["scan_date"])
        recent = (
            safe_execute(
                sb.table("scan_logs")
                .select("*")
                .gte("scan_date", (latest_ts - _td(minutes=5)).isoformat())
                .order("scan_date", desc=True)
            ).data
            or []
        )
        # Filter to the same triggered_by as the latest row — protects
        # against a cron scan and manual scan interleaving.
        latest_trigger = last[0].get("triggered_by")
        recent = [r for r in recent if r.get("triggered_by") == latest_trigger]

        total_found = sum(int(r.get("rfps_found") or 0) for r in recent)
        total_new = sum(int(r.get("rfps_new") or 0) for r in recent)
        total_declined = sum(int(r.get("rfps_rejected") or 0) for r in recent)

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Last scan", l := last[0]["scan_date"][:16].replace("T", " "))
        c2.metric("Triggered by", _pretty_trigger(latest_trigger))
        c3.metric(
            "Found", total_found,
            help="Candidates returned by scrapers across all sources in this run.",
        )
        c4.metric(
            "New", total_new,
            help="Inserted or merge-updated rows in rfp_submissions — passed "
                 "the strict eligibility gate.",
        )
        c5.metric(
            "Rejected", total_declined,
            help="Filtered out at scan time by the STRICT eligibility gate "
                 "(country / theme / deadline / feasibility hard-reject). "
                 "These never enter the DB. Not to be confused with **Declined** "
                 "on the Screen tab — that's the auto_recommendation = Decline "
                 "based on MUST/PREFER scoring, which only applies to RFPs that "
                 "PASSED this gate.",
        )

    # Banner from the previous run (survives the post-scan rerun).
    _scan_banner = st.session_state.pop("admin_scan_banner", None)
    if _scan_banner:
        (st.success if _scan_banner.get("ok") else st.error)(_scan_banner["msg"])

    if st.button("▶ Run scan now", type="primary", key="admin_scan_btn"):
        # Lock navigation while the long-running subprocess holds the
        # script. Otherwise switching tabs mid-scan produces the
        # "double-screen" overlap where the previous render lingers
        # grayed-out next to the new one.
        st.markdown(
            """
            <style>
              [data-testid="stTabs"] [role="tablist"],
              [data-testid="stSidebarNav"] {
                pointer-events: none !important;
                opacity: 0.45 !important;
              }
            </style>
            """,
            unsafe_allow_html=True,
        )
        # Show visible "click registered" feedback BEFORE the import so the
        # user sees something happen instantly. Without this the click can
        # feel like a no-op for the first ~500ms while Python imports
        # scan_runner + scraper + bs4 + feedparser.
        _status = st.empty()
        _status.info("⏳ Initialising scan…")
        try:
            from core.scan_runner import run_scan_now, scan_banner
            _who = user.get("name") or user.get("email") or "admin"
            _status.info(scan_banner(_who))
            ok = run_scan_now(triggered_by=f"manual:{_who}")
        except Exception as exc:
            # Surface ANY exception so a scan that 'does nothing' becomes
            # diagnosable. Previous behaviour swallowed import / pipeline
            # crashes silently.
            _status.empty()
            st.error(
                f"❌ Scan crashed before completion: `{type(exc).__name__}: {exc}`. "
                "Check the terminal for the full traceback."
            )
            st.stop()

        st.session_state["admin_scan_banner"] = {
            "ok": bool(ok),
            "msg": (
                f"✓ Scan complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}. "
                "Last-scan metric and history below have been refreshed."
            ) if ok else "Scan exited with errors. See output above for details.",
        }
        st.rerun()

    # ----- Scan history (merged from former "Scan Logs" tab) -----------------
    st.markdown("---")
    st.subheader("Scan history")
    st.caption("Most recent 500 scan runs.")
    res = (
        sb.table("scan_logs")
        .select("*")
        .order("scan_date", desc=True)
        .limit(500)
        .execute()
    )
    logs = pd.DataFrame(res.data or [])
    if logs.empty:
        st.info("No scans recorded yet.")
    else:
        # Strip "manual:" prefix in the displayed column. Defensive against
        # NaN / None / non-string values so the apply never short-circuits.
        if "triggered_by" in logs.columns:
            logs["triggered_by"] = (
                logs["triggered_by"]
                .fillna("")
                .astype(str)
                .map(_pretty_trigger)
            )
        # rfps_rejected exists only after migration 012 — fall back gracefully
        # if the column hasn't been added yet.
        hist_cols = [
            "scan_date", "triggered_by", "source",
            "rfps_found", "rfps_new", "rfps_duplicate",
        ]
        if "rfps_rejected" in logs.columns:
            hist_cols.append("rfps_rejected")
        hist_cols += ["duration_sec", "errors"]
        st.dataframe(
            logs[hist_cols],
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


# -----------------------------------------------------------------------------
# Tab 4 — Scan blacklist (hard-reject URL substrings)
# -----------------------------------------------------------------------------
with tab_blacklist:
    from core import blacklist as _blmod

    st.subheader("Scan blacklist")
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
with tab_learning:
    st.subheader("Learning data — captured signals")
    st.caption(
        "Every scan **reject**, human **decision** (Proceed/Park/Decline) and "
        "👍/👎 **feedback** is logged to `scan_decisions` — the labeled training "
        "set for the scoring model (ML Phase 2/3). Read-only here.")
    try:
        _ld = (sb.table("scan_decisions").select("*")
               .order("created_at", desc=True).limit(2000).execute().data or [])
    except Exception as exc:
        st.warning(f"Couldn't load scan_decisions — did you run migration 027? ({exc})")
        _ld = []
    if not _ld:
        st.info("No signals captured yet. Run a scan, set a decision, or hit 👍/👎 "
                "on a record — they'll appear here.")
    else:
        _ldf = pd.DataFrame(_ld)
        m1, m2, m3, m4, m5 = st.columns(5)
        _ev = _ldf.get("event_type", pd.Series(dtype=str))
        m1.metric("Total signals", len(_ldf))
        m2.metric("System rejects", int((_ev == "system_reject").sum()))
        m3.metric("Human decisions", int((_ev == "human_decision").sum()))
        m4.metric("👍/👎 feedback", int((_ev == "feedback").sum()))
        m5.metric("Reject verdicts",
                  int((_ev == "reject_verification").sum()))
        with st.expander("Rejects by reason category", expanded=False):
            _rej = _ldf[_ev == "system_reject"]
            if not _rej.empty:
                _by = (_rej["label"].fillna("—").value_counts()
                       .rename_axis("reason").reset_index(name="count"))
                st.dataframe(_by, hide_index=True, width='stretch')
            else:
                st.caption("No rejects logged yet.")
        _cols = [c for c in ["created_at", "event_type", "label", "reason",
                             "opportunity_title", "funding_agency", "source",
                             "submission_deadline", "alignment_score",
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
