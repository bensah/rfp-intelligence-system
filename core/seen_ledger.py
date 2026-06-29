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

# Columns find_duplicates needs — the ledger is exactly this projection.
PROJECTION = (
    "uid", "opportunity_id", "opportunity_title", "opportunity_link",
    "funding_agency", "submission_deadline", "call_award_value",
)


def signature(row: Mapping[str, Any]) -> dict[str, Any]:
    """The dedup-relevant projection of an rfp row, with the deadline coerced to a
    plain ISO string (find_duplicates compares it as text)."""
    dl = row.get("submission_deadline")
    if hasattr(dl, "isoformat"):
        dl = dl.isoformat()
    return {
        "uid": row.get("uid"),
        "opportunity_id": row.get("opportunity_id"),
        "opportunity_title": row.get("opportunity_title"),
        "opportunity_link": row.get("opportunity_link"),
        "funding_agency": row.get("funding_agency"),
        "submission_deadline": str(dl) if dl else None,
        "call_award_value": row.get("call_award_value"),
    }


def fetch_all() -> list[dict[str, Any]]:
    """All tombstones as find_duplicates-compatible rows. [] on any error."""
    try:
        res = get_client().table("rfp_seen").select(",".join(PROJECTION)).execute()
        return res.data or []
    except Exception as exc:
        log.debug("seen_ledger.fetch_all unavailable: %s", exc)
        return []


def record(rows: Iterable[Mapping[str, Any]], *, reason: str = "ingested") -> int:
    """Upsert tombstones for the given rfp rows (keyed by uid). Best-effort:
    returns the number recorded, 0 on error. Never raises."""
    payload: list[dict[str, Any]] = []
    for r in rows:
        sig = signature(r)
        if not sig.get("uid"):
            continue
        sig["reason"] = reason
        payload.append(sig)
    if not payload:
        return 0
    try:
        get_client().table("rfp_seen").upsert(payload, on_conflict="uid").execute()
        return len(payload)
    except Exception as exc:
        log.debug("seen_ledger.record skipped (%d rows): %s", len(payload), exc)
        return 0


def record_one(row: Mapping[str, Any], *, reason: str = "ingested") -> None:
    record([row], reason=reason)
