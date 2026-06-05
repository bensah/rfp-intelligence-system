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

# Must be the FIRST Streamlit call so a direct refresh lands in wide layout.
st.set_page_config(
    page_title="Admin — RFPIS",
    page_icon="🛈",
    layout="wide",
    initial_sidebar_state="expanded",
)

from auth.authenticator import ensure_logged_in
from core import excel_sync, settings
from db.supabase_client import get_client

user = ensure_logged_in()
if not user:
    st.stop()

from core.app_header import render_app_header  # noqa: E402
render_app_header()
if user.get("role") != "admin":
    st.error("Admins only.")
    st.stop()

sb = get_client()
st.title("Admin Panel")

tab_settings, tab_data, tab_sources, tab_scan = st.tabs(
    ["Settings", "Data", "Sources", "Manual Scan"]
)


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
    st.subheader("Organization")
    st.caption(
        "RFPIS ships as a multi-tenant product — the deploying-org profile "
        "below stamps every page header, the Report dashboard, and outgoing "
        "email digests. Defaults are intentional placeholders — fill these "
        "in the first time you set up the app for your organization."
    )

    _org = settings.get_org()
    oc1, oc2 = st.columns(2)
    org_name = oc1.text_input(
        "Organization name", value=_org.get("org_name", ""),
        help="Full name shown in page captions and the Report header. "
             "e.g. 'Acme Foundation — Business Development Team'.",
    )
    org_short = oc2.text_input(
        "Short name / abbreviation", value=_org.get("org_short", ""),
        help="Compact label used in page titles, grant lead/sub defaults, "
             "and scan-log displays. e.g. 'Acme BD'.",
    )
    oc3, oc4 = st.columns(2)
    org_country = oc3.text_input(
        "Primary country", value=_org.get("org_country", ""),
        help="Country the deploying org operates from. Used in the Report "
             "geographic context. e.g. 'Cameroon'.",
    )
    org_team = oc4.text_input(
        "Team / department", value=_org.get("org_team", ""),
        help="The team within the org running the screening. e.g. "
             "'Business Development Team'.",
    )
    oc5, oc6 = st.columns(2)
    org_email = oc5.text_input(
        "Contact email", value=_org.get("org_contact_email", ""),
        help="Distribution list for digest emails + the From / Reply-To "
             "address on outgoing notifications.",
    )
    org_website = oc6.text_input(
        "Website (optional)", value=_org.get("org_website", ""),
        help="Public URL — surfaces in the Report footer and exported PDFs.",
    )
    # ---- Logo: file uploader stored as base64 in app_settings -----------
    # The file is encoded inline into the settings table — no filesystem
    # dependency, so it survives Streamlit Cloud container restarts (where
    # local uploads would be wiped). Legacy `org_logo_url` is still
    # respected as a fallback by get_org_logo() / the Report header for any
    # install that pasted a hosted URL before this changed.
    st.markdown("**Logo** (optional) — uploaded to the app and persisted "
                "in the settings table. Renders in the Report header.")
    current_logo_bytes, _current_logo_mime = settings.get_org_logo()
    lcol_preview, lcol_uploader, lcol_clear = st.columns([1, 3, 1])
    with lcol_preview:
        if current_logo_bytes:
            st.image(current_logo_bytes, width=110, caption="Current logo")
        else:
            st.caption("_No logo uploaded yet._")
    with lcol_uploader:
        new_logo_file = st.file_uploader(
            "Upload a new logo (PNG / JPG / WebP / GIF / SVG, ≤2 MB recommended)",
            type=["png", "jpg", "jpeg", "webp", "gif", "svg"],
            key="org_logo_uploader",
            help="The file is encoded and stored in the app_settings table. "
                 "Use a transparent-background PNG ≤200px tall for the "
                 "cleanest Report header.",
        )
    with lcol_clear:
        # Vertical spacer so the button aligns with the uploader, not its label.
        st.markdown("<div style='height:1.85rem'></div>", unsafe_allow_html=True)
        if current_logo_bytes and st.button(
            "🗑 Remove", key="org_logo_remove",
            help="Delete the stored logo. Falls back to no-logo until you upload another.",
        ):
            settings.clear_org_logo(updated_by=user.get("email"))
            st.success("Logo removed.")
            st.rerun()

    if st.button("💾 Save organization profile", type="primary",
                 key="save_org_profile"):
        settings.set_org({
            "org_name":          org_name.strip(),
            "org_short":         org_short.strip(),
            "org_country":       org_country.strip(),
            "org_team":          org_team.strip(),
            "org_contact_email": org_email.strip(),
            "org_website":       org_website.strip(),
        }, updated_by=user.get("email"))
        # Save the uploaded logo (if any) alongside the text fields so a
        # single button click captures everything.
        if new_logo_file is not None:
            file_bytes = new_logo_file.read()
            if len(file_bytes) > 5 * 1024 * 1024:
                st.warning(
                    f"⚠ Logo is {len(file_bytes) / 1024 / 1024:.1f} MB — "
                    "consider downsizing. Stored anyway."
                )
            settings.set_org_logo(
                file_bytes,
                new_logo_file.type or "image/png",
                updated_by=user.get("email"),
            )
        st.success("Organization profile saved.")
        st.rerun()

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
                  use_container_width=True):
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

    st.markdown("---")
    st.subheader("Currency exchange rates")
    st.caption(
        "USD rate = how many USD one unit of the currency converts to "
        "(e.g. GBP 1.33 means £1 = $1.33). **Click any cell to edit it.** "
        "Use the **+** at the bottom of the table to add a new currency, "
        "and the row trash icon to remove. Click **💾 Save currency rates** "
        "below when done."
    )
    from core import dropdowns as _d
    cur_list = _d.load().get("currencies", []) or []
    cur_df = pd.DataFrame([
        {
            "Code": c.get("code") or "",
            "Label": c.get("label") or "",
            "Symbol": c.get("symbol") or "",
            "Aliases": ", ".join(c.get("aliases") or []),
            "USD rate": float(c.get("usd_rate") or 1.0),
        }
        for c in cur_list
    ])
    edited_cur = st.data_editor(
        cur_df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        column_config={
            "Code":   st.column_config.TextColumn("Code", required=True, help="3-letter ISO code (USD, GBP, EUR, XAF, CAD, ...)"),
            "Label":  st.column_config.TextColumn("Label"),
            "Symbol": st.column_config.TextColumn("Symbol", help="$, £, €, C$, or blank"),
            "Aliases": st.column_config.TextColumn("Aliases", help="Comma-separated legacy labels (e.g. 'GBP £, £')"),
            "USD rate": st.column_config.NumberColumn(
                "USD rate", min_value=0.0001, max_value=10000.0, step=0.0001, format="%.4f", required=True,
            ),
        },
        key="fx_editor",
    )
    if st.button("💾 Save currency rates", type="primary"):
        new_list = []
        for _, row in edited_cur.iterrows():
            code = (row.get("Code") or "").strip()
            if not code:
                continue
            aliases = [a.strip() for a in (row.get("Aliases") or "").split(",") if a.strip()]
            new_list.append({
                "code": code,
                "label": (row.get("Label") or code).strip() or code,
                "symbol": (row.get("Symbol") or None) or None,
                "aliases": aliases,
                "usd_rate": float(row.get("USD rate") or 1.0),
            })
        settings.set_currency_overrides(new_list, updated_by=user.get("email"))
        st.success(f"Saved {len(new_list)} currency entries.")
        st.rerun()

    # ------------------------------------------------------------------
    # Eligibility policies (drives scan-time country/theme filter +
    # auto-scoring of the 9 MUST/PREFER criteria).
    # ------------------------------------------------------------------
    st.markdown("---")
    st.subheader("Scan Preferences - Eligibility & Auto-Scoring")
    st.caption(
        "**Stage 1 — Eligibility gate (applied during scan):** RFPs that don't "
        "match country or theme are NOT inserted. **Stage 2 — Auto-scoring "
        "(applied to inserted RFPs):** each of the 9 MUST/PREFER criteria gets "
        "an auto-assigned Yes/Partial/No based on keyword matching at the "
        "configured rigor level. Reviewers can override anything on the Review tab. "
        "This whole block is the configurable algorithm — change it to point "
        "the system at a different organisation's priorities."
    )
    from core import policies as _pol
    _live = _pol.get_policies()

    # Render any save/reset banner from the previous rerun.
    _pol_msg = st.session_state.pop("pol_save_msg", None)
    if _pol_msg:
        st.success(_pol_msg)

    # --- Tag-chip widget helper ---------------------------------------------
    # Wraps st.multiselect with accept_new_options where supported (Streamlit
    # 1.41+). On older versions we fall back to a plain multiselect — the
    # user can still pick from the option pool, just not type new values.
    def _tag_input(label: str, values: list[str], *, options: list[str] | None = None,
                    key: str, help: str | None = None):
        opts = list(options or [])
        # Make sure every currently-selected value is in the options list,
        # otherwise multiselect will silently drop it.
        for v in values:
            if v not in opts:
                opts.append(v)
        try:
            return st.multiselect(
                label, options=opts, default=list(values),
                accept_new_options=True, key=key, help=help,
            )
        except TypeError:
            # Older Streamlit without accept_new_options — degrade to plain.
            return st.multiselect(
                label, options=opts, default=list(values), key=key, help=help,
            )

    pol_tabs = st.tabs(["Countries", "Themes", "Criteria", "JSON (advanced)"])

    with pol_tabs[0]:
        from core.auto_scorer import KNOWN_COUNTRIES as _ALL_COUNTRIES
        st.markdown(
            "**Eligible countries** — RFPs mentioning any of these are admitted. "
            "Pick from the dropdown or type to add a new country."
        )
        _tag_input(
            "Eligible countries",
            list(_live["countries"]["eligible"]),
            options=list(_ALL_COUNTRIES),
            key="pol_countries_eligible",
            help="Selected countries appear as removable tags below the dropdown.",
        )
        st.markdown(
            "**Broad-geography terms** — terms that imply the above are in-scope "
            "(LMIC, Africa, Sub-Saharan, etc.). Type each and press Enter."
        )
        _tag_input(
            "Broad terms",
            list(_live["countries"]["broad_terms"]),
            key="pol_countries_broad",
            help="Free-text — type a phrase and press Enter to add as a tag.",
        )
        permissive = st.checkbox(
            "Permissive when geography unmentioned (recommended ON)",
            value=bool(_live["countries"].get("permissive_when_silent", True)),
            key="pol_countries_permissive",
            help="If the RFP says nothing about geography, treat as eligible. "
                 "Turn off to be strict (only RFPs that explicitly name your countries pass).",
        )

    with pol_tabs[1]:
        st.markdown(
            "**Required theme keywords** — RFP must mention ≥1 of these to be admitted. "
            "Type and press Enter to add."
        )
        _tag_input(
            "Required themes",
            list(_live["themes"]["required_any"]),
            key="pol_themes_required",
        )
        st.markdown(
            "**Excluded themes (HARD REJECT at scan time)** — RFPs matching any "
            "of these are dropped before insertion. Use this for off-mission "
            "topics like *clinical trial*, *preclinical*, etc."
        )
        _tag_input(
            "Excluded themes",
            list(_live["themes"].get("excluded_any") or []),
            key="pol_themes_excluded",
        )

    with pol_tabs[2]:
        st.markdown(
            "**Per-criterion rigor + keyword bags.** Rigor 0 = criterion not "
            "enforced (always Yes). Rigor 5 = needs 5+ positive matches for Yes; "
            "≥3 matches → Partial. Negative-keyword matches force No."
        )
        st.warning(
            "⚠ **Feasibility is special**: its negative keywords act as a "
            "scan-time HARD REJECT (the RFP isn't inserted at all). Other "
            "criteria's negatives only flip the criterion's value to *No*. "
            "Put clinical-trial / preclinical / out-of-capability phrases under "
            "**Feasibility → Negative** to filter at scan time, OR under "
            "**Themes → Excluded** (which also rejects)."
        )
        criteria_inputs: dict[str, dict] = {}
        crit_labels = {
            "feasibility": "Feasibility",
            "must_1_govt_alignment": "MUST 1 — Govt alignment",
            "must_2_strategic_fit": "MUST 2 — Strategic fit",
            "must_3_implementable": "MUST 3 — Implementable",
            "must_4_compliant": "MUST 4 — Compliant",
            "must_5_resourcing": "MUST 5 — Resourcing",
            "prefer_6_funding_quality": "PREFER 6 — Funding quality",
            "prefer_7_monitorable": "PREFER 7 — Monitorable",
            "prefer_8_partnership": "PREFER 8 — Partnership",
            "prefer_9_scale": "PREFER 9 — Scale",
        }
        for ckey in _pol.CRITERION_KEYS:
            rule = (_live.get("criteria") or {}).get(ckey, {}) or {}
            with st.expander(crit_labels[ckey], expanded=False):
                rigor = st.slider(
                    "Rigor (0 = ignored, 5 = strict)",
                    min_value=0, max_value=5,
                    value=int(rule.get("rigor", 2)),
                    key=f"pol_rigor_{ckey}",
                )
                col_pos, col_neg = st.columns(2)
                with col_pos:
                    _tag_input(
                        "Positive keywords",
                        list(rule.get("positive") or []),
                        key=f"pol_pos_{ckey}",
                        help="Type and press Enter to add.",
                    )
                with col_neg:
                    _tag_input(
                        "Negative keywords (force No)",
                        list(rule.get("negative") or []),
                        key=f"pol_neg_{ckey}",
                    )
                criteria_inputs[ckey] = {"rigor": rigor}

    with pol_tabs[3]:
        st.markdown(
            "**Raw JSON** — read-only preview. Use the other tabs to edit; "
            "this is here for export/debugging only."
        )
        st.code(__import__("json").dumps(_live, indent=2), language="json")

    pc1, pc2, _ = st.columns([1, 1, 4])
    if pc1.button("💾 Save preferences", type="primary", key="save_policies_btn"):
        def _list(key: str) -> list[str]:
            """Multiselect-backed session keys hold a list of strings;
            be defensive in case an older string value lingers."""
            v = st.session_state.get(key, [])
            if isinstance(v, str):
                return [ln.strip() for ln in v.splitlines() if ln.strip()]
            return [str(x).strip() for x in (v or []) if str(x).strip()]

        new_pol = {
            "countries": {
                "eligible": _list("pol_countries_eligible"),
                "broad_terms": _list("pol_countries_broad"),
                "permissive_when_silent": bool(st.session_state.get("pol_countries_permissive", True)),
            },
            "themes": {
                "required_any": _list("pol_themes_required"),
                "excluded_any": _list("pol_themes_excluded"),
            },
            "criteria": {
                ckey: {
                    "rigor": int(st.session_state.get(f"pol_rigor_{ckey}", 2)),
                    "positive": _list(f"pol_pos_{ckey}"),
                    "negative": _list(f"pol_neg_{ckey}"),
                }
                for ckey in _pol.CRITERION_KEYS
            },
        }
        try:
            _pol.set_policies(new_pol, updated_by=user.get("email"))
            st.session_state["pol_save_msg"] = (
                "✓ Policies saved. The next scan will use the new rules."
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Save failed: {exc}")
    if pc2.button("↺ Reset to defaults", key="reset_policies_btn"):
        _pol.reset_to_defaults(updated_by=user.get("email"))
        st.session_state["pol_save_msg"] = "✓ Reset to defaults."
        st.rerun()

    st.markdown("---")
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
    if dc2.button("🔁 Reset & re-dedup", use_container_width=True):
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
    if rc2.button("🧹 Wipe meeting_logs", use_container_width=True):
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
    if agc2.button("🧹 Wipe migration grants", use_container_width=True, key="wipe_ag_migration"):
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
    if arc2.button("🧹 Wipe auto-scan rows", use_container_width=True, key="wipe_auto_rfps"):
        try:
            res = sb.table("rfp_submissions").delete().eq("source", "auto").execute()
            deleted = len(res.data or [])
            arc1.success(
                f"Deleted **{deleted}** auto-scanned RFP(s). "
                "Click 🔄 Scan now on the Screen tab (or in Manual Scan tab) to refresh."
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
    if sh2.button("🧹 Clear scan history", use_container_width=True, key="wipe_scan_logs"):
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
                   use_container_width=True, key="fresh_test_reset"):
        try:
            r1 = sb.table("rfp_submissions").delete().eq("source", "auto").execute()
            r2 = (
                sb.table("scan_logs")
                .delete()
                .neq("id", "00000000-0000-0000-0000-000000000000")
                .execute()
            )
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
with tab_data:
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
                ("is_resolved",   "bool",   "Status: Achieved?"),
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
                           "application_lead", "purpose", "linked_rfp_uid", "source"],
            "col_labels": {
                "engagement_date": "Date", "donor": "Donor",
                "engagement_type": "Type", "format": "Format",
                "application_lead": "Internal lead", "purpose": "Purpose",
                "linked_rfp_uid": "Linked RFP", "source": "Source",
            },
            "date_cols": ["engagement_date"],
            "search_cols": ["donor", "purpose", "outcome", "application_lead"],
            "advanced_filters": ["application_lead", "engagement_type", "source"],
            "edit_fields": [
                ("engagement_date", "date", "Engagement date *"),
                ("donor",           "text", "Donor"),
                ("engagement_type", "text", "Engagement type"),
                ("format",          "text", "Format"),
                ("application_lead",       "text", "Internal lead"),
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
            if pc4.button("🔄 Refresh", use_container_width=True, key=f"refresh_{spec['table']}"):
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
                use_container_width=True,
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
                    "➕ Add new", use_container_width=True, key=f"add_{spec['table']}",
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
                add_clicked = ab1.button("➕ Add", use_container_width=True, key=f"add_{spec['table']}")
                edit_clicked = False
                del_clicked = ab3.button(
                    f"🗑 Delete {len(sel_rows)}", use_container_width=True,
                    key=f"del_{spec['table']}",
                )
                share_clicked = ab4.button(
                    f"📤 Share {len(sel_rows)}", use_container_width=True,
                    key=f"share_{spec['table']}",
                )
            else:
                pretty = " — ".join(
                    str(sel_row.get(c))[:60]
                    for c in display_cols[:2] if sel_row.get(c) not in (None, "")
                )
                st.success(f"Selected: **{pretty}** · id `{sel_row.get('id')}`")
                ab1, ab2, ab3, ab4, _ = st.columns([1, 1, 1, 1, 4])
                add_clicked = ab1.button("➕ Add", use_container_width=True, key=f"add_{spec['table']}")
                edit_clicked = ab2.button("✏ Edit", use_container_width=True, key=f"edit_{spec['table']}")
                del_clicked = ab3.button("🗑 Delete", use_container_width=True, key=f"del_{spec['table']}")
                share_clicked = ab4.button("📤 Share", use_container_width=True, key=f"share_{spec['table']}")

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
                save = sc.button("💾 Save", type="primary", use_container_width=True,
                                  key=f"savebtn_{table}_{'edit' if current else 'add'}")
                cancel = cc.button("Cancel", use_container_width=True,
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
                    type="primary", use_container_width=True,
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
                if dc2.button("Cancel", use_container_width=True,
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
                    use_container_width=True,
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
                    use_container_width=True,
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


# -----------------------------------------------------------------------------
# Tab 1 — Donor Sources
# -----------------------------------------------------------------------------
with tab_sources:
    st.subheader("Donor sources catalog")
    st.caption(
        "Curated per-donor RFP-publishing URLs. The Friday scan + manual scan "
        "iterate over every **active** row here, in addition to the keyword-"
        "wide sources in `config/sources.yaml`. Use **📥 Import from config** "
        "below to copy the YAML entries into this table so you can edit them "
        "via the UI."
    )

    # ----- Import from config/sources.yaml ----------------------------------
    # Copies every yaml entry into donor_sources, skipping any already
    # present (matched by donor_name OR rfp_listing_url).
    ic1, ic2 = st.columns([3, 1])
    if ic2.button("📥 Import from config", use_container_width=True, key="import_sources_yaml"):
        try:
            from pathlib import Path as _P
            import yaml as _yaml
            _yaml_path = _P(__file__).resolve().parent.parent / "config" / "sources.yaml"
            with _yaml_path.open(encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
            _yaml_sources = _cfg.get("sources", []) or []

            existing = sb.table("donor_sources").select("donor_name,rfp_listing_url").execute().data or []
            existing_names = {(r.get("donor_name") or "").strip().lower() for r in existing}
            existing_urls = {(r.get("rfp_listing_url") or "").strip().lower() for r in existing}

            to_insert: list[dict] = []
            skipped: list[str] = []
            for s in _yaml_sources:
                name = (s.get("name") or "").strip()
                url = (s.get("url") or "").strip()
                method = (s.get("method") or "html").strip()
                if not name or not url:
                    continue
                if name.lower() in existing_names or url.lower() in existing_urls:
                    skipped.append(name)
                    continue
                # Derive a short donor_code from the name (first word, max 12 chars).
                code = name.split("—")[0].split("(")[0].strip().split()[0][:12]
                to_insert.append({
                    "donor_name": name,
                    "donor_code": code,
                    "rfp_listing_url": url,
                    "scrape_method": method if method in ("html", "rss", "rest_json", "manual") else "html",
                    "notes": s.get("note") or f"Imported from sources.yaml on {date.today().isoformat()}",
                    "is_active": True,
                    "created_by": user.get("email"),
                })

            if to_insert:
                sb.table("donor_sources").insert(to_insert).execute()
            ic1.success(
                f"Imported **{len(to_insert)}** new source(s). "
                f"Skipped **{len(skipped)}** already present "
                f"({', '.join(skipped[:6])}{'…' if len(skipped) > 6 else ''})."
            )
            st.cache_data.clear()
            st.rerun()
        except Exception as exc:
            ic1.error(f"Import failed: {exc}")
    st.info(
        "💡 **Click any cell to edit it** (URL, donor name, notes, etc.), then "
        "click **💾 Save changes** below to persist. Use **➕ Add a new donor "
        "source** to insert, or **⚠ Delete a donor source** to remove."
    )

    @st.cache_data(ttl=15)
    def _donors() -> pd.DataFrame:
        res = (
            get_client()
            .table("donor_sources")
            .select("*")
            .order("donor_name")
            .execute()
        )
        return pd.DataFrame(res.data or [])

    ddf = _donors()
    if ddf.empty:
        st.info("No donor sources yet — add one below to start scraping a specific donor.")
    else:
        editor_key = "donor_sources_editor"
        view = ddf[
            [
                "id",
                "donor_name",
                "donor_code",
                "rfp_listing_url",
                "scrape_method",
                "is_active",
                "last_scraped_at",
                "last_scrape_status",
                "notes",
            ]
        ].copy()

        edited = st.data_editor(
            view,
            hide_index=True,
            use_container_width=True,
            key=editor_key,
            num_rows="fixed",
            column_config={
                "id": st.column_config.TextColumn("ID", disabled=True, width="small"),
                "donor_name": st.column_config.TextColumn("Donor", required=True),
                "donor_code": st.column_config.TextColumn("Code", width="small"),
                "rfp_listing_url": st.column_config.LinkColumn("Listing URL", required=True),
                "scrape_method": st.column_config.SelectboxColumn(
                    "Method", options=["html", "rss", "rest_json", "manual"], required=True
                ),
                "is_active": st.column_config.CheckboxColumn("Active"),
                "last_scraped_at": st.column_config.DatetimeColumn(
                    "Last scan", disabled=True, format="YYYY-MM-DD HH:mm"
                ),
                "last_scrape_status": st.column_config.TextColumn("Last status", disabled=True),
                "notes": st.column_config.TextColumn("Notes"),
            },
        )

        cs1, cs2, cs3 = st.columns([1, 1, 4])
        if cs1.button("💾 Save changes", type="primary"):
            state = st.session_state.get(editor_key, {})
            edits = state.get("edited_rows") or {}
            wrote = 0
            for row_idx, changes in edits.items():
                payload = {
                    k: (None if pd.isna(v) or v == "" else v)
                    for k, v in changes.items()
                    if k != "id"
                }
                if not payload:
                    continue
                src_id = view.iloc[row_idx]["id"]
                sb.table("donor_sources").update(payload).eq("id", src_id).execute()
                wrote += 1
            if wrote:
                st.cache_data.clear()
                st.success(f"Saved {wrote} source(s).")
                st.rerun()
            else:
                st.info("No changes detected.")

        if cs2.button("🔄 Refresh"):
            st.cache_data.clear()
            st.rerun()

    st.divider()
    with st.expander("➕ Add a new donor source", expanded=False):
        with st.form("add_donor_source", clear_on_submit=True):
            c1, c2 = st.columns(2)
            new_name = c1.text_input("Donor name *")
            new_code = c2.text_input("Donor code (e.g. BMGF)")
            c3, c4 = st.columns([3, 1])
            new_url = c3.text_input("RFP listing URL *")
            new_method = c4.selectbox("Method", ["html", "rss", "rest_json", "manual"])
            new_base = st.text_input("Base URL (optional, e.g. https://wellcome.org)")
            new_notes = st.text_area("Notes", height=80)
            ok = st.form_submit_button("Add donor source", type="primary")
        if ok:
            if not new_name.strip() or not new_url.strip():
                st.error("Donor name and listing URL are required.")
            else:
                try:
                    sb.table("donor_sources").insert(
                        {
                            "donor_name": new_name.strip(),
                            "donor_code": new_code.strip() or None,
                            "base_url": new_base.strip() or None,
                            "rfp_listing_url": new_url.strip(),
                            "scrape_method": new_method,
                            "notes": new_notes.strip() or None,
                            "created_by": user.get("email"),
                            "is_active": True,
                        }
                    ).execute()
                    st.cache_data.clear()
                    st.success(f"Added donor source: {new_name}")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not add: {exc}")

    with st.expander("⚠ Delete a donor source", expanded=False):
        if not ddf.empty:
            options = {f"{r['donor_name']} — {r['rfp_listing_url']}": r["id"] for _, r in ddf.iterrows()}
            pick = st.selectbox("Pick source to delete", list(options.keys()), key="ds_del_pick")
            if st.button("Delete this donor source", type="secondary"):
                sb.table("donor_sources").delete().eq("id", options[pick]).execute()
                st.cache_data.clear()
                st.success("Deleted.")
                st.rerun()


# -----------------------------------------------------------------------------
# Tab 2 — Manual Scan
# -----------------------------------------------------------------------------
with tab_scan:
    st.subheader("Trigger a manual scan")
    st.caption(
        "Scan every configured donor source for new RFPs that match this "
        "organisation's eligibility policies (Settings → Scan eligibility & "
        "auto-scoring policies). A full run (~40 donor sources with detail-"
        "page + PDF enrichment) typically takes **3-8 minutes**."
    )

    last = (
        sb.table("scan_logs")
        .select("*")
        .order("scan_date", desc=True)
        .limit(1)
        .execute()
        .data
    )
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
            sb.table("scan_logs")
            .select("*")
            .gte("scan_date", (latest_ts - _td(minutes=5)).isoformat())
            .order("scan_date", desc=True)
            .execute()
            .data
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
            from core.scan_runner import run_scan_now
            _who = user.get("name") or user.get("email") or "admin"
            _status.info(
                f"⏳ Scan running as **{_who}** — with ~35 sources + "
                "detail-page enrichment, expect 3-8 minutes. Please stay on this tab."
            )
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
            use_container_width=True,
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
