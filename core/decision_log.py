"""Decision / reject logging — labeled-data capture for the learning pipeline.

ML Phase 1. Every call appends one row to `scan_decisions` (migration 027):

  * log_rejects(records)        — bulk-log scan-gate rejects (deduped by link so
                                  re-scanning the same dead URL doesn't pile up).
  * log_decision(row, decision) — a reviewer set Proceed / Park / Decline.
  * log_feedback(row, verdict)  — a reviewer flagged a record good / bad.

Every function is best-effort and NEVER raises into the caller — capturing
training data must not break a scan or a UI save. Works both in the Streamlit
app and the headless scan subprocess (it only uses the plain Supabase client).
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from db.supabase_client import get_client

log = logging.getLogger(__name__)

_TABLE = "scan_decisions"


def _reason_category(reason: str | None) -> str:
    """The gate prefixes reasons as 'category: detail' (e.g. 'type: loan …').
    Return the category for a clean label; fall back to the whole string."""
    r = (reason or "").strip()
    return r.split(":", 1)[0].strip() if ":" in r else r


def _date_or_none(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _scope_text(v: Any) -> str | None:
    if not v:
        return None
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x) or None
    return str(v)


def _features(cand: dict) -> dict | None:
    """Capture the decision-model feature vector inline (ML Phase 3). Best-effort
    — telemetry must never break a scan or a save. System-reject candidates are
    pre-scoring, so their criteria features come back None; that's fine (they're
    negatives, judged on geo/deadline/channel)."""
    try:
        from core import features as _f
        feats = _f.extract(cand)
        return feats or None
    except Exception as exc:
        log.debug("decision_log._features failed: %s", exc)
        return None


def _base_record(cand: dict, *, event_type: str, label: str | None,
                 reason: str | None, by: str | None) -> dict:
    return {
        "event_type": event_type,
        "label": label,
        "reason": (reason or "")[:600] or None,
        "rfp_uid": cand.get("uid") or cand.get("rfp_uid"),
        "opportunity_title": (cand.get("opportunity_title") or "")[:500] or None,
        "opportunity_link": cand.get("opportunity_link"),
        "funding_agency": cand.get("funding_agency"),
        "source": cand.get("source") or cand.get("_source_origin"),
        "geographic_scope": _scope_text(cand.get("geographic_scope")),
        "submission_deadline": _date_or_none(cand.get("submission_deadline")),
        "alignment_score": cand.get("alignment_score"),
        "features": _features(cand),
        "decided_by": by,
    }


def log_rejects(records: Iterable[dict]) -> int:
    """Bulk-log scan-gate rejects. Each `record` is a candidate dict plus a
    '_reject_reason' key. Dedupes against links already logged as system_reject
    so the table holds each rejected opportunity once. Returns rows written."""
    recs = [r for r in (records or []) if r]
    if not recs:
        return 0
    try:
        sb = get_client()
        # Skip links already captured as a system_reject (keep it to unique opps).
        seen: set[str] = set()
        try:
            existing = (sb.table(_TABLE).select("opportunity_link")
                        .eq("event_type", "system_reject").execute().data or [])
            seen = {(e.get("opportunity_link") or "") for e in existing}
        except Exception:
            seen = set()
        rows, batch_seen = [], set()
        for r in recs:
            link = (r.get("opportunity_link") or "").strip()
            key = link or (r.get("opportunity_title") or "")
            if not key or key in seen or key in batch_seen:
                continue
            batch_seen.add(key)
            reason = r.get("_reject_reason")
            rows.append(_base_record(
                r, event_type="system_reject",
                label=_reason_category(reason), reason=reason, by=None))
        for i in range(0, len(rows), 200):
            sb.table(_TABLE).insert(rows[i:i + 200]).execute()
        if rows:
            log.info("decision_log: recorded %d new system rejects", len(rows))
        return len(rows)
    except Exception as exc:  # never break a scan over telemetry
        log.debug("decision_log.log_rejects failed: %s", exc)
        return 0


def log_decision(row: dict, decision: str, by: str | None = None) -> bool:
    """Log a human Proceed / Park / Decline on a record."""
    if not row or not (decision or "").strip():
        return False
    try:
        rec = _base_record(row, event_type="human_decision",
                           label=str(decision).strip().title(),
                           reason=None, by=by)
        get_client().table(_TABLE).insert(rec).execute()
        return True
    except Exception as exc:
        log.debug("decision_log.log_decision failed: %s", exc)
        return False


def log_feedback(row: dict, verdict: str, by: str | None = None) -> bool:
    """Log a reviewer 👍/👎 on a record (verdict 'good' or 'bad')."""
    v = (verdict or "").strip().lower()
    if not row or v not in ("good", "bad"):
        return False
    try:
        rec = _base_record(row, event_type="feedback", label=v,
                           reason=None, by=by)
        get_client().table(_TABLE).insert(rec).execute()
        return True
    except Exception as exc:
        log.debug("decision_log.log_feedback failed: %s", exc)
        return False
