"""Permanent 'seen' ledger (tombstones) for RFP de-duplication.

Every RFP that has EVER entered rfp_submissions is recorded here — at ingest time
(scan / manual submit / Excel import) plus the one-time backfill in migration 033.
Tombstones are NEVER deleted, so once an opportunity has been seen it can never
silently re-enter the pipeline, even after its live rfp_submissions row is hard-
deleted (declined / parked / proceeded rows still live there and are caught by the
live deduplicator; this ledger is the backstop for DELETED rows — the leak it
closes).

The ledger stores the SAME minimal projection core.deduplicator.find_duplicates
reads, so suppression reuses that matcher verbatim — no second normalisation.

Best-effort throughout: if the table is missing (migration 033 not yet run) or the
DB is unreachable, fetch_all() returns [] and record() is a no-op, so the scan and
submit flows never break.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from db.supabase_client import get_client

log = logging.getLogger(__name__)

# Identity columns — present under these names in every version of the table, and the
# ones find_duplicates rules 1-3 (opportunity_id / link / title) actually match on.
_IDENTITY = ("uid", "opportunity_id", "opportunity_title", "opportunity_link",
             "funding_agency")

# The deadline and value columns are the ones whose NAMES DIVERGED, and that divergence
# killed the whole ledger. Migrations 056 and 059 renamed `submission_deadline` ->
# `call_submission_deadline` and `estimated_value` -> `call_award_value` ON
# rfp_submissions ONLY; rfp_seen, created back in migration 033, kept the old names. This
# module asked for the new ones, so every read AND every write raised Postgres 42703
# (`column "call_submission_deadline" does not exist`) - and both call sites caught it and
# carried on, so the ledger reported success while recording nothing and suppressing
# nothing. 150 rows sat in the table unreadable, and an opportunity that had been deleted
# came back on the next scan every time.
#
# Rather than pin one spelling and break again on the next rename, resolve it against the
# live table once per process and keep working either way.
_MODERN = {"call_submission_deadline": "call_submission_deadline",
           "call_award_value": "call_award_value"}
_LEGACY = {"call_submission_deadline": "submission_deadline",
           "call_award_value": "estimated_value"}

_colmap: dict[str, str] | None = None


def column_map() -> dict[str, str]:
    """Map find_duplicates' field names -> this table's actual column names.

    Probes the live table once. `{}` means neither spelling worked, in which case the
    ledger falls back to identity columns only: suppression by opportunity_id, link and
    title still works (find_duplicates rules 1-3), which is what blocking a returning
    opportunity needs. Only rule 4, which requires a matching deadline, is lost.
    """
    global _colmap
    if _colmap is not None:
        return _colmap
    from db.supabase_client import safe_execute
    for candidate in (_MODERN, _LEGACY):
        try:
            safe_execute(get_client().table("rfp_seen")
                         .select(",".join(candidate.values())).limit(1))
            _colmap = candidate
            return _colmap
        except Exception:
            continue
    log.warning("seen_ledger: rfp_seen has neither the current nor the legacy "
                "deadline/value columns - suppressing on identity columns only")
    _colmap = {}
    return _colmap


def _projection() -> tuple[str, ...]:
    return _IDENTITY + tuple(column_map().values())


# Kept for callers that import it. Read `_projection()` for what is actually queried.
PROJECTION = _IDENTITY + tuple(_MODERN.values())


def signature(row: Mapping[str, Any]) -> dict[str, Any]:
    """The dedup-relevant projection of an rfp row, with the deadline coerced to a
    plain ISO string (find_duplicates compares it as text)."""
    dl = row.get("call_submission_deadline")
    if hasattr(dl, "isoformat"):
        dl = dl.isoformat()
    return {
        "uid": row.get("uid"),
        "opportunity_id": row.get("opportunity_id"),
        "opportunity_title": row.get("opportunity_title"),
        "opportunity_link": row.get("opportunity_link"),
        "funding_agency": row.get("funding_agency"),
        "call_submission_deadline": str(dl) if dl else None,
        "call_award_value": row.get("call_award_value"),
    }


def fetch_all() -> list[dict[str, Any]]:
    """All tombstones as find_duplicates-compatible rows. [] on any error.

    Rows come back keyed the way find_duplicates expects, whatever the table calls its
    own columns.
    """
    cmap = column_map()
    try:
        from db.supabase_client import safe_execute
        res = safe_execute(get_client().table("rfp_seen").select(",".join(_projection())))
        rows = res.data or []
    except Exception as exc:
        # WARNING, not DEBUG. A ledger that cannot be read silently defeats the only
        # thing standing between a deleted opportunity and its return, and that is
        # exactly how it went unnoticed - the scan reported normally for weeks.
        log.warning("seen_ledger.fetch_all unavailable, suppression is OFF: %s", exc)
        return []
    if not cmap:
        return rows
    out = []
    for r in rows:
        row = dict(r)
        for field, col in cmap.items():
            if col != field:
                row[field] = row.pop(col, None)
        out.append(row)
    return out


def record(rows: Iterable[Mapping[str, Any]], *, reason: str = "ingested") -> int:
    """Upsert tombstones for the given rfp rows (keyed by uid). Best-effort:
    returns the number recorded, 0 on error. Never raises."""
    cmap = column_map()
    payload: list[dict[str, Any]] = []
    for r in rows:
        sig = signature(r)
        if not sig.get("uid"):
            continue
        sig["reason"] = reason
        for field, col in cmap.items():          # write the names the table really has
            val = sig.pop(field, None)
            if col:
                sig[col] = val
        for field in _MODERN:                    # drop anything the table cannot store
            sig.pop(field, None)
        payload.append(sig)
    if not payload:
        return 0
    try:
        from db.supabase_client import safe_execute
        safe_execute(get_client().table("rfp_seen").upsert(payload, on_conflict="uid"))
        return len(payload)
    except Exception as exc:
        # WARNING, not DEBUG: a tombstone that was never written is an opportunity that
        # will come back, and the caller believed it was recorded.
        log.warning("seen_ledger.record FAILED (%d rows not tombstoned): %s",
                    len(payload), exc)
        return 0


def record_one(row: Mapping[str, Any], *, reason: str = "ingested") -> None:
    record([row], reason=reason)
