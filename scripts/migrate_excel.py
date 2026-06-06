"""One-time migration of a legacy Excel screener into Supabase.

Reads the workbook at `EXCEL_SOURCE_PATH` (or the first *.xlsx beside
the project root as fallback) — expects sheets: Form1, Schedule,
Meeting_Log, Engagement_Log, Active_Grants_Log, Narrative_Log. Upserts
the records into the corresponding Supabase tables with source='migration'.

Usage:
    python scripts/migrate_excel.py [--dry-run] [--xlsx PATH]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from openpyxl import load_workbook

# Allow running as `python scripts/migrate_excel.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.supabase_client import get_client  # noqa: E402
from core.scorer import CRITERIA, score_submission  # noqa: E402
from core.review_week import review_week_label  # noqa: E402

# Repo-root *.xlsx fallback — Excel files are gitignored so this is a
# developer-local convenience. Resolves to whichever *.xlsx happens to
# sit beside the project root.
_repo_root = Path(__file__).resolve().parent.parent
_xlsx_candidates = sorted(_repo_root.glob("*.xlsx"))
DEFAULT_XLSX = _xlsx_candidates[0] if _xlsx_candidates else None


# ---------------------------------------------------------------------------
# Cell coercion helpers
# ---------------------------------------------------------------------------
def _txt(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _date(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    try:
        return datetime.fromisoformat(str(v).split(" ")[0]).date().isoformat()
    except Exception:
        return None


def _ts(v: Any) -> Optional[str]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return _txt(v)


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        s = str(v).replace(",", "").replace("$", "").strip()
        try:
            return float(s)
        except ValueError:
            return None


def _int(v: Any) -> Optional[int]:
    n = _num(v)
    return int(n) if n is not None else None


def _bool_yes(v: Any) -> bool:
    return _txt(v) is not None and str(v).strip().lower() in {"yes", "y", "true", "1"}


def _multi(v: Any) -> Optional[list[str]]:
    s = _txt(v)
    if not s:
        return None
    # Excel multi-select uses ';' separator
    parts = [p.strip() for p in s.split(";") if p.strip()]
    return parts or None


# ---------------------------------------------------------------------------
# Sheet -> row mappers
# ---------------------------------------------------------------------------
import re as _re
import unicodedata as _ud


def _norm_header(s: str) -> str:
    """Normalise header text for case/whitespace/dash-style insensitivity."""
    if s is None:
        return ""
    # Normalize unicode (combining chars), strip BOM
    s = _ud.normalize("NFKC", str(s)).replace("﻿", "")
    # Normalize all dash variants to ASCII hyphen
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    # Collapse whitespace + lowercase
    return _re.sub(r"\s+", " ", s.strip().lower())


def build_col_map(ws) -> dict[str, int]:
    """Read header row 1 → {normalised_header: col_index}."""
    out: dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=1, column=c).value
        key = _norm_header(v)
        if key and key not in out:
            out[key] = c
    return out


def map_form1_row_by_header(row: list[Any], col_map: dict[str, int],
                             extra: dict[str, Any] | None = None) -> Optional[dict[str, Any]]:
    """Map a Form1 row by HEADER NAME (resilient to column reordering)."""

    def get(*names: str) -> Any:
        for n in names:
            idx = col_map.get(_norm_header(n))
            if idx and idx - 1 < len(row):
                return row[idx - 1]
        return None

    uid = _txt(get("Form_ID", "Form ID", "Form-ID"))
    title = _txt(get("Opportunity Title", "Title"))
    if not uid or not title:
        return None

    return {
        "uid": uid,
        "form_id": uid,
        "source": "migration",
        "search_date": _ts(get("Search Date")),
        "submitted_by": _txt(get("Submitted By")),
        "opportunity_title": title,
        "brief_description": _txt(get("Brief Description")),
        "date_posted": _date(get("Date Posted")),
        "funding_agency": _txt(get("Funding Agency", "Funder")),
        "geographic_scope": _multi(get("Geographic Scope")),
        "program_area": _multi(get("Program Area")),
        "focus_theme": _txt(get("Focus Theme")),
        "opportunity_link": _txt(get("Opportunity Link")),
        # applicant_role is recognised under several spreadsheet headers.
        # Preferred headers are "Applicant Role" / "Role"; the original
        # screener workbook labelled this column "the organisation Role", which we still
        # accept last so a legacy workbook keeps importing without edits.
        "applicant_role": _txt(get("Applicant Role", "Role", "the organisation Role")),
        "lead_applicant": _txt(get("Lead Applicant")),
        "sub_applicant": _txt(get("Sub Applicant")),
        "funding_window": _txt(get("Funding Window")),
        "submission_deadline": _date(get("Submission Deadline")),
        "expected_award_date": _date(get("Expected award date", "Expected Award Date")),
        "time_to_award": _txt(get("Time to award", "Time to Award")),
        "estimated_value": _num(get("Estimated Value")),
        "currency": _txt(get("Currency")),
        "project_duration": _int(get("Project Duration (months)", "Project Duration")),
        "submission_format": _txt(get("Submission Format")),
        "feasibility": _txt(get("Feasibility")),
        "must_1_govt_alignment": _txt(get("MUST 1 - Govt alignment")),
        "must_2_strategic_fit": _txt(get("MUST 2 - Strategic fit")),
        "must_3_implementable": _txt(get("MUST 3 - Implementable scope")),
        "must_4_compliant": _txt(get("MUST 4 - Compliant")),
        "must_5_resourcing": _txt(get("MUST 5 - Resourcing / timeline")),
        "prefer_6_funding_quality": _txt(get("PREFER 6 - Funding quality")),
        "prefer_7_monitorable": _txt(get("PREFER 7 - Monitorable results")),
        "prefer_8_partnership": _txt(get("PREFER 8 - Partnership advantage")),
        "prefer_9_scale": _txt(get("PREFER 9 - Scale & sustainability")),
        "decline_flags_present": _bool_yes(get("Decline flags present?", "Decline flags present")),
        "key_risks": _txt(get("Key risks (one line)", "Key risks")),
        "decision": _txt(get("Decision")),
        "decision_date": _date(get("Decision date")),
        "decision_rationale": _txt(get("Decision rationale (2-3 lines)",
                                       "Decision rationale (2–3 lines)",
                                       "Decision rationale")),
        "stage": _txt(get("Stage")),
        "proposal_lead": _txt(get("Proposal Lead")),
        "contributors": _multi(get("Contributors/Reviewers", "Contributors")),
        "reviewers": _multi(get("Reviewers")),
        "support_roles": _txt(get("Support ( e.g. tech/finance/compliance)",
                                    "Support (e.g. tech/finance/compliance)",
                                    "Support")),
        "progress_status": _txt(get("Progress Status")),
        "amount_requested": _num(get("Amount Requested")),
        "date_completed": _date(get("Date Completed")),
        "submissions": _int(get("Submissions")) or 1,
        "donor_decision": _txt(get("Donor Decision Status", "Donor Decision")),
        "next_action": _txt(get("Next Action")),
        "assigned_to": _txt(get("Assigned To")),
        "remarks": _txt(get("Remarks")),
        "action_deadline": _date(get("Action Deadline")),
        "last_update": _date(get("Last Update")),
        # "Donor Decision Date" is when the donor decided (= our date_of_approval)
        "date_of_approval": _date(get("Donor Decision Date", "Date of Approval")),
        "amount_secured": _num(get("Amount Secured")),
        "currency_secured": _txt(get("Currency Secured")),
        "donor_program_officer": _txt(get("Donor Program Officer")),
        "next_step": _txt(get("Next Step")),
        "kickoff_date": _date(get("Kick-off Date", "Kickoff Date")),
        **(extra or {}),
    }


# Backwards-compat shim for any caller still using the old positional form
def map_form1_row(row, extra=None):
    raise RuntimeError(
        "map_form1_row(row) is deprecated — use map_form1_row_by_header(row, col_map)"
    )


def _enrich_rfp(rec: dict[str, Any]) -> dict[str, Any]:
    """Compute review_week + alignment_score + auto_recommendation."""
    score, rec_decision = score_submission(
        {k: rec.get(k) for k in CRITERIA},
        bool(rec.get("decline_flags_present")),
    )
    rec["alignment_score"] = score
    rec["auto_recommendation"] = rec_decision

    # review_week: prefer the search_date, fall back to form_start_date / submitted_at
    anchor = rec.get("search_date") or rec.get("form_start_date")
    if anchor:
        try:
            d = datetime.fromisoformat(anchor.replace("Z", "+00:00")).date()
            rec["review_week"] = review_week_label(d)
        except Exception:
            pass
    return rec


def find_header_row(ws, *needed_headers: str, max_search: int = 20) -> Optional[int]:
    """Return the row number whose cells contain ALL of `needed_headers`.
    Search up to `max_search` rows. Header matching is case/whitespace/dash
    insensitive (via _norm_header).
    """
    needed = {_norm_header(h) for h in needed_headers}
    for r in range(1, min(ws.max_row + 1, max_search + 1)):
        seen = {
            _norm_header(ws.cell(row=r, column=c).value)
            for c in range(1, ws.max_column + 1)
        }
        if needed.issubset(seen):
            return r
    return None


def build_col_map_at(ws, header_row: int) -> dict[str, int]:
    """Build {normalised_header: col_index} from a specific header row."""
    out: dict[str, int] = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=header_row, column=c).value
        key = _norm_header(v)
        if key and key not in out:
            out[key] = c
    return out


import hashlib as _hashlib


def _meeting_external_id(
    meeting_date: str,
    donor_title: str | None,
    rfp_uid: str | None,
) -> str:
    """Stable identifier for a meeting log row.

    Prefers RFPID (the new column = Form_ID / uid in Form1) for identity,
    since it uniquely identifies an opportunity even if the user renames
    Donor_Title in the dropdown. Falls back to donor_title for legacy rows
    that predate the RFPID column. Owner is NOT part of the key — Excel can
    reassign an owner without us treating the row as new (we want to UPDATE
    the owner in place, preserving is_resolved).
    """
    natural_key = (rfp_uid or "").strip()
    if not natural_key:
        natural_key = (donor_title or "").strip().lower()
    key = f"{meeting_date}|{natural_key}"
    return _hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def map_meeting_row_by_header(row: list[Any], col_map: dict[str, int]) -> Optional[dict[str, Any]]:
    """Map one Meeting_Log row using header names."""
    def get(*names: str) -> Any:
        for n in names:
            idx = col_map.get(_norm_header(n))
            if idx and idx - 1 < len(row):
                return row[idx - 1]
        return None

    mdate = _date(get("Meeting Date"))
    if not mdate:
        return None
    donor = _txt(get("Donor_Title", "Donor Title"))
    # NEW: RFPID column carries the Form_ID / uid (= rfp_submissions.uid).
    rfpid = _txt(get("RFPID", "RFP_ID", "Form_ID", "Form ID", "UID"))
    owner = _txt(get("Owner"))
    return {
        "meeting_date": mdate,
        "donor_title": donor,
        "rfp_uid": rfpid,  # Linked RFP — was None before this fix
        "remarks": _txt(get("Remarks / Issues", "Remarks/Issues", "Remarks", "Issues")),
        "actions": _txt(get("Actions / Recommendations", "Actions/Recommendations",
                              "Actions", "Recommendations")),
        "owner": owner,
        "deadline": _date(get("Deadline", "Due Date", "Due")),
        "source": "migration",
        "external_id": _meeting_external_id(mdate, donor, rfpid),
    }


# Backwards-compat shim — deprecated, use map_meeting_row_by_header
def map_meeting_row(row, extra=None):
    raise RuntimeError(
        "map_meeting_row(row) is deprecated — use map_meeting_row_by_header(row, col_map)"
    )


def map_engagement_row(row: list[Any]) -> Optional[dict[str, Any]]:
    # Engagement_Log columns start at B (col 2): Date, Donor, Type, Format, Lead, Contacts, Purpose, Outcome, LinkedRFP
    def c(i: int) -> Any:
        return row[i - 1] if i - 1 < len(row) else None

    edate = _date(c(2))
    if not edate:
        return None
    donor = _txt(c(3))
    internal_lead = _txt(c(6))
    ext_key = f"{edate}|{(donor or '').strip().lower()}|{(internal_lead or '').strip().lower()}"
    return {
        "engagement_date": edate,
        "donor": donor,
        "engagement_type": _txt(c(4)),
        "format": _txt(c(5)),
        "internal_lead": internal_lead,
        "donor_contacts": _txt(c(7)),
        "purpose": _txt(c(8)),
        "outcome": _txt(c(9)),
        "linked_rfp_uid": _txt(c(10)),
        "source": "migration",
        "external_id": _hashlib.md5(ext_key.encode("utf-8")).hexdigest()[:16],
    }


def map_active_grant_row(row: list[Any]) -> Optional[dict[str, Any]]:
    # Active_Grants_Log columns start at B (col 2)
    def c(i: int) -> Any:
        return row[i - 1] if i - 1 < len(row) else None

    gid = _txt(c(2))
    if not gid:
        return None
    return {
        "grant_id": gid,
        "donor_title": _txt(c(3)),
        "form_id_link": _txt(c(4)),
        "award_date": _date(c(5)),
        "end_date": _date(c(6)),
        "report_type": _txt(c(7)),
        "report_due_date": _date(c(8)),
        "submitted_date": _date(c(9)),
        "status": _txt(c(10)),
        "owner": _txt(c(11)),
        "remarks": _txt(c(13)),
        "source": "migration",
    }


def map_narrative_row(row: list[Any]) -> Optional[dict[str, Any]]:
    # Narrative_Log columns start at B (col 2): VersionDate, Title, UsedIn, UsedWith, DateUsed, Status, Link, Owner
    def c(i: int) -> Any:
        return row[i - 1] if i - 1 < len(row) else None

    vdate = _date(c(2))
    if not vdate:
        return None
    title = _txt(c(3))
    ext_key = f"{vdate}|{(title or '').strip().lower()}"
    return {
        "version_date": vdate,
        "narrative_title": title,
        "used_in": _txt(c(4)),
        "used_with": _txt(c(5)),
        "date_used": _date(c(6)),
        "status": _txt(c(7)),
        "link_location": _txt(c(8)),
        "owner": _txt(c(9)),
        "source": "migration",
        "external_id": _hashlib.md5(ext_key.encode("utf-8")).hexdigest()[:16],
    }


def map_schedule_row(row: list[Any]) -> Optional[dict[str, Any]]:
    # Schedule columns start at B (col 2): CallDate, NoteTaker, Presenter, Chair
    def c(i: int) -> Any:
        return row[i - 1] if i - 1 < len(row) else None

    cdate = _date(c(2))
    if not cdate:
        return None
    return {
        "call_date": cdate,
        "note_taker": _txt(c(3)),
        "rfp_presenter": _txt(c(4)),
        "meeting_chair": _txt(c(5)),
    }


# ---------------------------------------------------------------------------
# Migration driver
# ---------------------------------------------------------------------------
def _rows(ws, header_row: int) -> Iterable[list[Any]]:
    for r in range(header_row + 1, ws.max_row + 1):
        row = [ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)]
        if any(v is not None and str(v).strip() != "" for v in row):
            yield row


def migrate(xlsx_path: Path, dry_run: bool = False) -> None:
    wb = load_workbook(xlsx_path, data_only=True)
    sb = None if dry_run else get_client()

    def upsert(table: str, rows: list[dict[str, Any]], conflict_key: str | None = None) -> None:
        if not rows:
            print(f"  {table}: 0 rows — skipping")
            return
        print(f"  {table}: {len(rows)} rows")
        if dry_run:
            return
        if conflict_key:
            sb.table(table).upsert(rows, on_conflict=conflict_key).execute()
        else:
            sb.table(table).insert(rows).execute()

    def merge_by_external_id(
        table: str,
        rows: list[dict[str, Any]],
        updatable_fields: list[str],
    ) -> None:
        """Merge Excel rows into a table keyed by external_id.

        Existing rows (source='migration') get `updatable_fields` UPDATED.
        Any other columns (e.g. app-managed toggles) are PRESERVED.
        Rows whose external_id is new get INSERTED.
        """
        if not rows:
            print(f"  {table}: 0 rows — skipping")
            return
        # In-Excel dedup safety net
        seen: set[str] = set()
        uniq: list[dict[str, Any]] = []
        for r in rows:
            k = r.get("external_id")
            if not k or k in seen:
                continue
            seen.add(k)
            uniq.append(r)
        if len(uniq) < len(rows):
            print(f"  {table}: deduped {len(rows) - len(uniq)} same-key rows in Excel")
        rows = uniq
        print(f"  {table}: {len(rows)} rows mapped")
        if dry_run:
            return
        existing = (
            sb.table(table)
            .select("id,external_id")
            .eq("source", "migration")
            .execute()
            .data
            or []
        )
        ext_to_id = {r["external_id"]: r["id"] for r in existing if r.get("external_id")}
        inserts: list[dict[str, Any]] = []
        updated = 0
        for r in rows:
            ext = r["external_id"]
            if ext in ext_to_id:
                payload = {f: r.get(f) for f in updatable_fields}
                sb.table(table).update(payload).eq("id", ext_to_id[ext]).execute()
                updated += 1
            else:
                inserts.append(r)
        if inserts:
            sb.table(table).insert(inserts).execute()
        print(f"  {table}: {updated} updated · {len(inserts)} inserted")

    # --- rfp_submissions (from Form1, header row 1)
    print("[Form1 -> rfp_submissions]")
    rfp_rows: list[dict[str, Any]] = []
    skipped: list[tuple[int, Any, Any, str]] = []
    ws = wb["Form1"]
    col_map = build_col_map(ws)
    print(f"  {len(col_map)} columns detected by header name")

    for excel_row_num in range(2, ws.max_row + 1):
        row = [ws.cell(row=excel_row_num, column=c).value
               for c in range(1, ws.max_column + 1)]
        if not any(v is not None and str(v).strip() != "" for v in row):
            continue  # blank row
        # Don't pass extra={"submissions": 1} — the function reads Submissions
        # from the row directly (with its own default-to-1 fallback) and
        # **extra here would CLOBBER the real value.
        rec = map_form1_row_by_header(row, col_map)
        if rec:
            rfp_rows.append(_enrich_rfp(rec))
        else:
            form_id_col = col_map.get(_norm_header("Form_ID")) or 2
            title_col = col_map.get(_norm_header("Opportunity Title")) or 5
            uid = row[form_id_col - 1] if form_id_col - 1 < len(row) else None
            title = row[title_col - 1] if title_col - 1 < len(row) else None
            reason = "missing Form_ID" if not _txt(uid) else "missing Opportunity Title"
            skipped.append((excel_row_num, uid, title, reason))
    if skipped:
        print(f"  ⚠ {len(skipped)} row(s) skipped:")
        for r_num, uid, title, reason in skipped:
            print(f"    row {r_num}: uid={uid!r} title={title!r} → {reason}")
    upsert("rfp_submissions", rfp_rows, conflict_key="uid")

    # --- meeting_logs (Meeting_Log) — header auto-detected
    print("[Meeting_Log -> meeting_logs]")
    ws_m = wb["Meeting_Log"]
    hr_m = find_header_row(ws_m, "Meeting Date", "Donor_Title", "Owner")
    if hr_m is None:
        print("  ⚠ Could not find header row in Meeting_Log — sheet skipped")
    else:
        print(f"  Header detected at row {hr_m}")
        col_map_m = build_col_map_at(ws_m, hr_m)
        m_rows: list[dict[str, Any]] = []
        for r in range(hr_m + 1, ws_m.max_row + 1):
            row = [ws_m.cell(row=r, column=c).value for c in range(1, ws_m.max_column + 1)]
            if not any(v is not None and str(v).strip() != "" for v in row):
                continue
            rec = map_meeting_row_by_header(row, col_map_m)
            if rec:
                m_rows.append(rec)
        # In-memory dedup safety net: collapse exact-duplicate rows BEFORE insert.
        seen_keys: set[str] = set()
        uniq_m_rows: list[dict[str, Any]] = []
        for r in m_rows:
            key = r["external_id"]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            uniq_m_rows.append(r)
        if len(uniq_m_rows) < len(m_rows):
            print(f"  meeting_logs: deduped {len(m_rows) - len(uniq_m_rows)} "
                  "rows with same external_id within Excel before insert")
        m_rows = uniq_m_rows
        print(f"  meeting_logs: {len(m_rows)} rows mapped")

        # MERGE semantics: existing rows get Excel-managed fields UPDATED;
        # is_resolved (and any app-side toggles) are PRESERVED. New rows
        # are inserted fresh.
        if not dry_run and m_rows:
            existing = (
                sb.table("meeting_logs")
                .select("id,external_id")
                .eq("source", "migration")
                .execute()
                .data
                or []
            )
            ext_to_id = {
                r["external_id"]: r["id"]
                for r in existing if r.get("external_id")
            }
            inserts: list[dict[str, Any]] = []
            updated = 0
            for r in m_rows:
                ext = r["external_id"]
                if ext in ext_to_id:
                    # Update Excel-managed fields; is_resolved (app-managed) survives.
                    # rfp_uid IS now Excel-managed (the new RFPID column).
                    sb.table("meeting_logs").update({
                        "meeting_date": r["meeting_date"],
                        "donor_title":  r["donor_title"],
                        "rfp_uid":      r["rfp_uid"],
                        "remarks":      r["remarks"],
                        "actions":      r["actions"],
                        "owner":        r["owner"],
                        "deadline":     r["deadline"],
                    }).eq("id", ext_to_id[ext]).execute()
                    updated += 1
                else:
                    inserts.append(r)
            if inserts:
                sb.table("meeting_logs").insert(inserts).execute()
            print(f"  meeting_logs: {updated} updated · {len(inserts)} inserted "
                  f"(is_resolved preserved across the {updated} updates)")

    # --- engagement_logs — merge by external_id (avoid duplicate accumulation)
    print("[Engagement_Log -> engagement_logs]")
    e_rows = [r for row in _rows(wb["Engagement_Log"], header_row=6) if (r := map_engagement_row(row))]
    merge_by_external_id(
        "engagement_logs",
        e_rows,
        updatable_fields=[
            "engagement_date", "donor", "engagement_type", "format",
            "internal_lead", "donor_contacts", "purpose", "outcome", "linked_rfp_uid",
        ],
    )

    # --- active_grants — keyed on grant_id. Delete stale migration rows
    # FIRST (rows that existed in a prior sync but disappeared from Excel
    # — without this they linger forever and pollute the per-grant view).
    # App-added rows (source='app') are preserved.
    print("[Active_Grants_Log -> active_grants]")
    g_rows = [r for row in _rows(wb["Active_Grants_Log"], header_row=6) if (r := map_active_grant_row(row))]
    current_grant_ids = [r["grant_id"] for r in g_rows if r.get("grant_id")]
    if not dry_run:
        # Delete migration rows whose grant_id is no longer in Excel.
        existing_migration = (
            sb.table("active_grants")
            .select("grant_id")
            .eq("source", "migration")
            .execute()
            .data
            or []
        )
        existing_mig_ids = {r["grant_id"] for r in existing_migration if r.get("grant_id")}
        stale_ids = sorted(existing_mig_ids - set(current_grant_ids))
        if stale_ids:
            sb.table("active_grants").delete().in_("grant_id", stale_ids).execute()
            print(f"  active_grants: deleted {len(stale_ids)} stale migration row(s): {stale_ids}")
    upsert("active_grants", g_rows, conflict_key="grant_id")

    # --- narrative_logs — merge by external_id
    print("[Narrative_Log -> narrative_logs]")
    n_rows = [r for row in _rows(wb["Narrative_Log"], header_row=6) if (r := map_narrative_row(row))]
    merge_by_external_id(
        "narrative_logs",
        n_rows,
        updatable_fields=[
            "version_date", "narrative_title", "used_in", "used_with",
            "date_used", "status", "link_location", "owner",
        ],
    )

    # --- meeting_schedule
    print("[Schedule -> meeting_schedule]")
    s_rows = [r for row in _rows(wb["Schedule"], header_row=4) if (r := map_schedule_row(row))]
    upsert("meeting_schedule", s_rows, conflict_key="call_date")

    print("\nDone." + (" (dry run — no writes performed)" if dry_run else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.xlsx.exists():
        sys.exit(f"Excel file not found: {args.xlsx}")
    print(f"Reading: {args.xlsx}")
    migrate(args.xlsx, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
