"""Data-access layer for `extracted_solicitations` — the GLOBAL, org-agnostic raw
store (DATA_SCHEMA_ETL.md §1–§4). The extraction stage writes here; the per-tenant
scorer reads here to produce Screened rows (rfp_submissions).

Best-effort like core.decision_log: never raises into a scan. Requires migration
044 to be applied. `uid` is a stable content key derived from the opportunity URL,
so re-scanning the same page UPSERTS the same row instead of duplicating it.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from db.supabase_client import get_client

log = logging.getLogger(__name__)

_TABLE = "extracted_solicitations"

# Columns the table actually has — anything else in a record dict is dropped so a
# stray key never fails the insert.
_COLS = {
    "uid", "opportunity_name", "opportunity_id", "opportunity_url", "apply_url",
    "funding_opportunity_number", "funder_name", "agency_code", "grantmaking_entity",
    "donor_intel_id", "donor_key", "brief_description", "full_description",
    "applicant_fit_profile", "project_stages", "what_is_funded", "what_is_not_funded",
    "eligibility_applicant_types", "eligibility_countries", "eligibility_other",
    "grant_amount", "call_award_floor", "call_award_ceiling", "total_program_funding",
    "expected_awards", "currency", "date_posted", "deadline", "deadline_confidence",
    "funding_status", "funding_window", "expected_award_date", "time_to_award",
    "project_duration", "submission_format", "solicitation_type", "instrument_type",
    "opportunity_type", "focus_themes", "call_domain_areas", "call_geographic_scope",
    "solicitation_language", "attachments", "resource_links", "funding_tiers",
    "source", "source_uid",
    "raw_text", "content_hash", "extraction_confidence", "field_provenance",
    "scraped_at", "updated_at",
}


def normalize_url(url: str) -> str:
    """Lowercase, drop fragment + trailing slash, keep query (some portals key the
    opportunity on a query param). Mirrors decision_log._reject_keys link norm."""
    return (url or "").strip().lower().split("#", 1)[0].rstrip("/")


def make_uid(url: str, title: str = "") -> str:
    """Stable content key: sha1 of the normalised URL (or title when URL absent)."""
    basis = normalize_url(url) or (title or "").strip().lower()
    return "es_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


def _clean(rec: dict[str, Any]) -> dict[str, Any]:
    """Keep only real columns; drop None so DB defaults apply on insert.

    List/dict values are passed through NATIVELY. supabase-py (PostgREST) serialises
    the whole request body to JSON exactly once, so a Python list/dict lands in a
    jsonb column as a real array/object. A previous ``json.dumps(v)`` here
    DOUBLE-ENCODED — the client re-serialised the already-stringified value, so jsonb
    stored a string scalar like ``"[\\"EU\\"]"`` instead of ``["EU"]``. Readers such
    as ``geographies.flatten_scope_terms`` / ``criteria_derive._as_list`` still tolerate
    the legacy stringified shape, but every new write must be clean single-encoded."""
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if k not in _COLS or v is None:
            continue
        out[k] = v
    return out


def upsert_extracted(rec: dict[str, Any]) -> str | None:
    """Insert or update one extracted solicitation (keyed on uid). Returns the uid,
    or None on failure. Fills uid from the URL when absent and stamps updated_at."""
    try:
        uid = rec.get("uid") or make_uid(rec.get("opportunity_url", ""),
                                         rec.get("opportunity_name", ""))
        payload = _clean({**rec, "uid": uid,
                          "updated_at": datetime.now(timezone.utc).isoformat()})
        if not payload.get("opportunity_name") or not payload.get("opportunity_url"):
            log.warning("extracted_store: skipping row missing name/url (uid=%s)", uid)
            return None
        from db.supabase_client import safe_execute
        safe_execute(get_client().table(_TABLE).upsert(payload, on_conflict="uid"))
        return uid
    except Exception as exc:                       # never break a scan
        log.warning("extracted_store.upsert failed (%s): %s",
                    rec.get("opportunity_url"), exc)
        return None


def get_extracted(uid: str) -> dict[str, Any] | None:
    try:
        rows = (get_client().table(_TABLE).select("*").eq("uid", uid)
                .limit(1).execute().data or [])
        return rows[0] if rows else None
    except Exception as exc:
        log.debug("extracted_store.get failed: %s", exc)
        return None


def exists(uid: str) -> bool:
    try:
        rows = (get_client().table(_TABLE).select("uid").eq("uid", uid)
                .limit(1).execute().data or [])
        return bool(rows)
    except Exception:
        return False


def recent_uids(days: int) -> set[str]:
    """UIDs of store rows refreshed within the last `days` days (updated_at,
    falling back to scraped_at). Used to skip re-extraction of still-fresh
    opportunities. Best-effort: returns an empty set on any error."""
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=max(0, int(days or 0)))).isoformat()
        rows = (get_client().table(_TABLE).select("uid,updated_at,scraped_at")
                .gte("updated_at", cutoff).execute().data or [])
        out = {r["uid"] for r in rows if r.get("uid")}
        # Rows whose updated_at is NULL are excluded by the server-side .gte above;
        # sweep those in with a scraped_at fallback so a freshly-inserted row that
        # never got an updated_at stamp still counts as fresh.
        try:
            more = (get_client().table(_TABLE).select("uid,updated_at,scraped_at")
                    .is_("updated_at", "null").gte("scraped_at", cutoff)
                    .execute().data or [])
            out |= {r["uid"] for r in more if r.get("uid")}
        except Exception:
            pass
        return out
    except Exception as exc:
        log.debug("extracted_store.recent_uids failed: %s", exc)
        return set()


def list_extracted(*, status: str | None = None, source: str | None = None,
                   limit: int = 1000) -> list[dict[str, Any]]:
    """List raw extracted rows (newest first), optionally filtered by funding_status
    / source. The per-tenant scorer consumes this."""
    try:
        q = get_client().table(_TABLE).select("*")
        if status:
            q = q.eq("funding_status", status)
        if source:
            q = q.eq("source", source)
        return (q.order("scraped_at", desc=True).limit(limit).execute().data or [])
    except Exception as exc:
        log.debug("extracted_store.list failed: %s", exc)
        return []


def mark_closed_past_deadline(today_iso: str) -> int:
    """Flip funding_status Open->Closed for rows whose deadline has passed. Returns
    rows updated (best-effort; called from the weekly cron before screening).

    This existed but had NO CALLER anywhere in the repository, so nothing ever aged a row
    out of the store: screening is handed the whole Open set on every run
    (`list_extracted(status="Open")`), and rows whose deadline had passed long ago were
    re-offered to every tenant every week. Measured on the live store: 719 Open rows, 258
    of them with a deadline already in the past.
    """
    try:
        res = (get_client().table(_TABLE).update({"funding_status": "Closed"})
               .eq("funding_status", "Open").lt("deadline", today_iso).execute())
        return len(res.data or [])
    except Exception as exc:
        log.warning("extracted_store.mark_closed failed: %s", exc)
        return 0


# How long an undated row may sit in the Open set before it is treated as stale. Matches
# auto_scorer._STALE_POSTING_DAYS - the same judgement about how long a call plausibly
# stays open - so the two do not drift apart.
_STALE_UNDATED_DAYS = 90


def mark_closed_stale_undated(today_iso: str, *, days: int = _STALE_UNDATED_DAYS) -> int:
    """Close Open rows that have NO deadline at all and have not been seen for `days`.

    `mark_closed_past_deadline` cannot touch these: its predicate is `deadline < today`,
    and a NULL deadline never satisfies a comparison, so an undated row was Open forever
    by construction. Those are exactly the rows that kept coming back - 30 of them on the
    live store, including every one of the repeatedly-reported expired calls.

    A row is only closed when it is BOTH undated and stale, so a genuinely rolling call
    that is still being re-crawled keeps its Open status (each crawl refreshes the
    timestamp).
    """
    from datetime import date, timedelta
    try:
        cutoff = (date.fromisoformat(today_iso[:10]) - timedelta(days=days)).isoformat()
    except (ValueError, TypeError):
        return 0
    closed = 0
    for stamp in ("updated_at", "scraped_at", "created_at"):
        try:
            res = (get_client().table(_TABLE).update({"funding_status": "Closed"})
                   .eq("funding_status", "Open").is_("deadline", "null")
                   .lt(stamp, cutoff).execute())
            closed += len(res.data or [])
            break                       # the first column the table actually has wins
        except Exception:
            continue
    return closed
