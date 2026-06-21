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


def _org_tag() -> str | None:
    """The deploying org's short/long name — stamps every learning row so labels
    stay attributable to THIS tenant (the organisation). Each deployment already has its own
    DB, and the features are computed against this org's profile, so labels are
    inherently per-tenant; this tag makes it explicit (and pooled-DB-ready)."""
    try:
        from core import settings as _s
        o = _s.get_org()
        return (o.get("org_short") or o.get("org_name") or "").strip() or None
    except Exception:
        return None


def _features(cand: dict) -> dict | None:
    """Capture the decision-model feature vector inline (ML Phase 3). Best-effort
    — telemetry must never break a scan or a save. System-reject candidates are
    pre-scoring, so their criteria features come back None; that's fine (they're
    negatives, judged on geo/deadline/channel). The org tag is folded in so the
    row is self-describing as a THIS-tenant label."""
    try:
        from core import features as _f
        feats = _f.extract(cand) or {}
        org = _org_tag()
        if org:
            feats["org"] = org      # tenant tag (the model reads only FEATURE_ORDER keys)
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
        "opportunity_type": cand.get("opportunity_type"),
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
    """Log a human Proceed / Park / Decline on a record — a training LABEL.

    Captures CONFIRMATIONS as well as overrides: a reviewer who saves a record
    keeping the recommended decision is endorsing it, which is the majority
    (and most important) signal — logging only changes would bias the model
    toward disagreement. Idempotent per record: skips when the latest
    human_decision already on file for this rfp carries the same label, so
    repeated saves don't pile up duplicates. Append-only otherwise (a later
    changed decision is a new row; the trainer takes the latest per rfp_uid)."""
    if not row or not (decision or "").strip():
        return False
    label = str(decision).strip().title()
    uid = row.get("uid") or row.get("rfp_uid")
    try:
        sb = get_client()
        if uid:
            try:
                prev = (sb.table(_TABLE).select("label")
                        .eq("event_type", "human_decision").eq("rfp_uid", uid)
                        .order("created_at", desc=True).limit(1).execute().data or [])
                if prev and (prev[0].get("label") or "").strip().lower() == label.lower():
                    return True            # unchanged confirmation already logged
            except Exception:
                pass                       # can't dedup → fall through and log
        # Capture the reviewer's written rationale alongside the decision so the
        # full human review (final 9 criteria in `features` + decision + rationale
        # + org tag) is one learning record.
        _note = (row.get("decision_note") or row.get("decision_rationale")
                 or row.get("notes") or None)
        rec = _base_record(row, event_type="human_decision",
                           label=label, reason=_note, by=by)
        sb.table(_TABLE).insert(rec).execute()
        return True
    except Exception as exc:
        log.debug("decision_log.log_decision failed: %s", exc)
        return False


def log_feedback(row: dict, verdict: str, by: str | None = None,
                 reason: str | None = None) -> bool:
    """Log reviewer feedback on a record: 'good' / 'neutral' / 'bad'.

    Three-way so it mirrors the decision classes without skewing the learning
    signal: Proceed→good, Park→neutral (intermediary, info-insufficient),
    Decline→bad. Migrated baseline decisions map the same way. `reason` tags the
    feedback's origin (e.g. 'search-result') so a web-search rating — which has
    no rfp_uid yet — stays distinguishable from feedback on an inserted record."""
    v = (verdict or "").strip().lower()
    if not row or v not in ("good", "neutral", "bad"):
        return False
    try:
        rec = _base_record(row, event_type="feedback", label=v,
                           reason=reason, by=by)
        get_client().table(_TABLE).insert(rec).execute()
        return True
    except Exception as exc:
        log.debug("decision_log.log_feedback failed: %s", exc)
        return False


# Reject-verification verdicts (Workstream A). Semantics are gate-quality, NOT
# the Proceed/Park/Decline model: a human is judging whether the HARD GATE was
# right to drop this candidate.
#   valid_reject — the auto-reject was correct (confirm the gate)
#   false_reject — the gate was WRONG; this should have entered (recoverable)
#   unsure       — can't tell
_REJECT_VERDICTS = ("valid_reject", "false_reject", "unsure")


def log_reject_verification(reject: dict, verdict: str, by: str | None = None,
                            corrected_reason: str | None = None) -> bool:
    """Log a human verdict on an auto-rejected candidate (event_type
    'reject_verification'). Distinct from `feedback` (which rates gate-SURVIVORS
    for the P/P/D model) — this trains/tunes the hard GATE itself. `reject` is a
    scan_decisions system_reject row.

    `corrected_reason` (when given) is the reason the gate SHOULD have used —
    stored in `reason`, overriding the system reason category, so a learning
    pass (scripts/analyze_reject_feedback.py) can find systematic gate errors
    ("system says not-an-rfp, human says deadline"). The system reason stays on
    the original system_reject row for comparison.

    Append-only, idempotent per link: a no-op when the latest verification
    already carries the same verdict AND reason (re-saves don't pile up).
    Best-effort — never raises."""
    v = (verdict or "").strip().lower()
    if not reject or v not in _REJECT_VERDICTS:
        return False
    link = (reject.get("opportunity_link") or "").strip()
    reason_to_store = (corrected_reason or "").strip() or None
    try:
        sb = get_client()
        if link:
            try:
                prev = (sb.table(_TABLE).select("label, reason")
                        .eq("event_type", "reject_verification")
                        .eq("opportunity_link", link)
                        .order("created_at", desc=True).limit(1).execute().data or [])
                if prev and (prev[0].get("label") or "").strip().lower() == v \
                        and (prev[0].get("reason") or None) == reason_to_store:
                    return True
            except Exception:
                pass
        rec = _base_record(
            reject, event_type="reject_verification", label=v,
            reason=reason_to_store, by=by)
        sb.table(_TABLE).insert(rec).execute()
        return True
    except Exception as exc:
        log.debug("decision_log.log_reject_verification failed: %s", exc)
        return False


def log_type_label(row: dict, type_value: str, by: str | None = None) -> bool:
    """Log a human-assigned opportunity TYPE (Grant / RFP / Tender / Job / …) —
    ground truth for the type classifier (event_type 'type_label', label=type).
    Stored on scan_decisions so it needs no new table and works for both rejected
    and inserted rows (anything with an opportunity_link). Idempotent per link on
    the same type; append-only otherwise. Best-effort — never raises."""
    t = (type_value or "").strip()
    if not row or not t or t == "—":
        return False
    link = (row.get("opportunity_link") or "").strip()
    try:
        sb = get_client()
        if link:
            try:
                prev = (sb.table(_TABLE).select("label")
                        .eq("event_type", "type_label").eq("opportunity_link", link)
                        .order("created_at", desc=True).limit(1).execute().data or [])
                if prev and (prev[0].get("label") or "").strip().lower() == t.lower():
                    return True
            except Exception:
                pass
        rec = _base_record(row, event_type="type_label", label=t,
                           reason=None, by=by)
        sb.table(_TABLE).insert(rec).execute()
        return True
    except Exception as exc:
        log.debug("decision_log.log_type_label failed: %s", exc)
        return False


def latest_reasons(event_type: str) -> dict[str, str]:
    """Map opportunity_link → latest stored `reason` for the given event_type
    (e.g. the human-corrected reject reason). Lets the Verify UI re-display a
    saved Correct reason after a reload. {} on any error."""
    try:
        rows = (get_client().table(_TABLE)
                .select("opportunity_link, reason, created_at")
                .eq("event_type", event_type)
                .order("created_at", desc=True).limit(5000).execute().data or [])
    except Exception:
        return {}
    out: dict[str, str] = {}
    for r in rows:                       # newest-first → first wins
        link = (r.get("opportunity_link") or "").strip()
        if link and link not in out and (r.get("reason") or "").strip():
            out[link] = r["reason"].strip()
    return out


def latest_verifications(event_type: str) -> dict[str, str]:
    """Map opportunity_link → latest verdict label for the given event_type
    ('reject_verification' or 'feedback'). Powers the verification UI so each row
    shows its current human verdict. {} on any error."""
    try:
        rows = (get_client().table(_TABLE)
                .select("opportunity_link, label, created_at")
                .eq("event_type", event_type)
                .order("created_at", desc=True).limit(5000).execute().data or [])
    except Exception:
        return {}
    out: dict[str, str] = {}
    for r in rows:                       # rows are newest-first → first wins
        link = (r.get("opportunity_link") or "").strip()
        if link and link not in out:
            out[link] = (r.get("label") or "").strip()
    return out
