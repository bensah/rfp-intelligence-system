"""Bridge from raw scraper output to rfp_submissions rows.

Each candidate from `core.scraper.scan_source(...)` is:
  1. Country + theme eligibility gate (drop out-of-scope candidates).
  2. Assigned a UID (initials='AS' for auto-scan, timestamp suffix).
  3. Auto-scored using admin-configurable policies.
  4. Checked against existing rfp_submissions for duplicates.
     - No match → INSERT new row.
     - Match → MERGE: fill empty fields on the existing row from this
       scrape, refresh auto-scoring only if the existing row had never
       been reviewed. Human-edited fields are never overwritten.

Returns (inserted, updated, duplicate) for the scan_logs row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.auto_scorer import auto_score, is_eligible
from core.deduplicator import find_duplicates
from core.policies import get_policies
from core.review_week import upcoming_review_week_label
from db.supabase_client import get_client

log = logging.getLogger(__name__)


# Fields that the scraper provides. Re-scans may fill these in if currently
# NULL on the existing row, but NEVER overwrite a populated value (which
# might have been edited by a human).
_SCRAPE_MANAGED_FIELDS = (
    "opportunity_link",
    "opportunity_id",
    "funding_agency",
    "brief_description",
    "date_posted",
    "submission_deadline",
)

# Auto-scoring outputs. Refreshed only when the existing row is still
# "unreviewed" (alignment_score IS NULL). Once a human touches the Review
# tab, we treat the score & criteria as theirs.
_AUTOSCORE_FIELDS = (
    "feasibility",
    "must_1_govt_alignment",
    "must_2_strategic_fit",
    "must_3_implementable",
    "must_4_compliant",
    "must_5_resourcing",
    "prefer_6_funding_quality",
    "prefer_7_monitorable",
    "prefer_8_partnership",
    "prefer_9_scale",
    "alignment_score",
    "auto_recommendation",
    "decline_flags_present",
)


def _generate_auto_uid(serial: int, ts: datetime | None = None) -> str:
    """Auto-scan UID — 'AS-YYMMDD-HHMM' with a -<serial> tail to avoid
    collisions when a single scan produces multiple candidates within the
    same minute."""
    ts = ts or datetime.now()
    return f"AS-{ts.strftime('%y%m%d')}-{ts.strftime('%H%M')}{serial:02d}"


def _build_row(
    candidate: dict[str, Any], serial: int, ts: datetime,
    policies: dict[str, Any],
) -> dict[str, Any]:
    """Build a fresh rfp_submissions row for INSERT."""
    uid = _generate_auto_uid(serial, ts)
    iso_now = ts.replace(tzinfo=timezone.utc).isoformat()
    deadline = candidate.get("submission_deadline")
    posted = candidate.get("date_posted")
    row: dict[str, Any] = {
        "uid": uid,
        "form_id": uid,
        "source": "auto",
        "submitted_by": "auto-scan",
        # Default contact for auto-scanned rows. Avoids leaving the column
        # NULL — useful for filtering / replies on shared records.
        "submitted_by_email": "bdt@taadom.org",
        "submitted_at": iso_now,
        "search_date": iso_now,
        "opportunity_title": candidate["opportunity_title"],
        "opportunity_id": candidate.get("opportunity_id"),
        "opportunity_link": candidate.get("opportunity_link"),
        "funding_agency": candidate.get("funding_agency"),
        "brief_description": candidate.get("brief_description"),
        "date_posted": posted.isoformat() if hasattr(posted, "isoformat") else None,
        "submission_deadline": (
            deadline.isoformat() if hasattr(deadline, "isoformat") else None
        ),
        "review_week": upcoming_review_week_label(),
        # ---- Pipeline defaults for auto-scanned rows ---------------------
        # Every newly-inserted scan row enters the workflow with a known
        # starting state so reviewers see a coherent Decision & Pipeline
        # tab instead of a bunch of blanks. Each of these has its own
        # dropdown vocabulary (see config/dropdowns.yaml) — values below
        # must match those options verbatim or the Edit UI surfaces them
        # as stray rogue options (same trap as the old feasibility="No"
        # bug). Human override always wins on later edits.
        "stage": "Identification & screening",
        "progress_status": "Not Started",
        "donor_decision": "Not submitted",
        "assigned_to": "TBD",
    }
    row.update(auto_score(candidate, policies))
    return row


def _build_merge_payload(
    candidate: dict[str, Any], existing_row: dict[str, Any],
    policies: dict[str, Any],
) -> dict[str, Any]:
    """Compute the UPDATE payload for an existing row matched on rescan.

    Rules:
      * Scrape-managed fields: only set if the existing row's value is
        NULL/empty AND the candidate has a value (i.e. fill-the-gap).
      * Auto-score fields: refreshed ONLY if existing alignment_score is
        NULL (= row has never been reviewed). Otherwise human work wins.
      * Title is never overwritten — humans may have cleaned it up.
    """
    payload: dict[str, Any] = {}
    deadline = candidate.get("submission_deadline")
    posted = candidate.get("date_posted")
    candidate_normalized = {
        "opportunity_link": candidate.get("opportunity_link"),
        "opportunity_id": candidate.get("opportunity_id"),
        "funding_agency": candidate.get("funding_agency"),
        "brief_description": candidate.get("brief_description"),
        "date_posted": posted.isoformat() if hasattr(posted, "isoformat") else None,
        "submission_deadline": (
            deadline.isoformat() if hasattr(deadline, "isoformat") else None
        ),
    }
    for field in _SCRAPE_MANAGED_FIELDS:
        new_val = candidate_normalized.get(field)
        old_val = existing_row.get(field)
        if new_val is None or new_val == "":
            continue
        if old_val is None or old_val == "":
            payload[field] = new_val

    # Refresh auto-scoring only when the existing row has no alignment_score
    # (the human hasn't reviewed it). If the existing description was empty
    # and we just filled it, that gives auto_scorer more text to work with.
    if existing_row.get("alignment_score") in (None, "", 0):
        # Build a fresh candidate view that merges already-known + new fields,
        # so auto_score sees the fullest possible context.
        merged_for_scoring = {
            "opportunity_title": existing_row.get("opportunity_title") or candidate.get("opportunity_title"),
            "brief_description": (
                existing_row.get("brief_description")
                or candidate.get("brief_description")
            ),
            "funding_agency": existing_row.get("funding_agency") or candidate.get("funding_agency"),
            "geographic_scope": existing_row.get("geographic_scope") or [],
            "focus_theme": existing_row.get("focus_theme"),
        }
        scored = auto_score(merged_for_scoring, policies)
        for field in _AUTOSCORE_FIELDS:
            if field in scored:
                payload[field] = scored[field]

    # Always refresh search_date — useful for "last seen" diagnostics.
    payload["search_date"] = datetime.now(timezone.utc).isoformat()
    return payload


def _payload_meaningful(payload: dict[str, Any]) -> bool:
    """A merge payload that only updates search_date is a no-op for the user."""
    return any(k != "search_date" for k in payload.keys())


def ingest_candidates(
    candidates: list[dict[str, Any]],
    *,
    existing: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Process a list of candidate dicts.

    Returns (new_or_updated, true_duplicate, rejected_by_policy).
      * new_or_updated — inserted + merge-updates (counts as "rfps_new")
      * true_duplicate — matched existing canonical, no new info
      * rejected_by_policy — failed the country / theme / deadline /
        feasibility eligibility gate; never touched the DB
    """
    if not candidates:
        return (0, 0, 0)

    sb = None if dry_run else get_client()

    # Fetch existing canonicals ONCE per scan. We pull the FULL row now so
    # the merge logic can decide which fields are still empty.
    if existing is None and not dry_run:
        existing = (
            sb.table("rfp_submissions")
            .select(
                "id,uid,opportunity_title,opportunity_link,opportunity_id,"
                "funding_agency,brief_description,date_posted,"
                "submission_deadline,estimated_value,alignment_score,"
                "geographic_scope,focus_theme,submitted_at,is_duplicate"
            )
            .eq("is_duplicate", False)
            .execute()
            .data
            or []
        )
    existing = existing or []

    policies = get_policies()

    inserted = 0
    updated = 0
    duplicate_unchanged = 0
    rejected = 0
    ts = datetime.now()

    for i, cand in enumerate(candidates):
        if not (cand.get("opportunity_title") or "").strip():
            continue
        # Country + theme gate
        ok, reason = is_eligible(cand, policies)
        if not ok:
            rejected += 1
            log.info("reject: %s — %s", cand.get("opportunity_title", "")[:60], reason)
            continue

        # Find duplicates using a minimal projection (find_duplicates only
        # reads these keys).
        probe = {
            "opportunity_title": cand["opportunity_title"],
            "opportunity_link": cand.get("opportunity_link"),
            "funding_agency": cand.get("funding_agency"),
            "submission_deadline": (
                cand["submission_deadline"].isoformat()
                if hasattr(cand.get("submission_deadline"), "isoformat") else None
            ),
            "estimated_value": None,
        }
        matches = find_duplicates(probe, existing=existing)

        if matches:
            # MERGE PATH — fill gaps on the existing row.
            existing_row = matches[0]
            # find_duplicates returns rows annotated with _reason. We need the
            # actual row from `existing` for the FULL field set.
            match_uid = existing_row.get("uid")
            full_existing = next(
                (e for e in existing if e.get("uid") == match_uid),
                existing_row,
            )
            payload = _build_merge_payload(cand, full_existing, policies)
            if not _payload_meaningful(payload):
                duplicate_unchanged += 1
                log.info(
                    "skip unchanged: %s — already has all scraped data (%s)",
                    cand["opportunity_title"][:60],
                    existing_row.get("_reason"),
                )
                continue
            if not dry_run:
                try:
                    sb.table("rfp_submissions").update(payload).eq(
                        "uid", match_uid
                    ).execute()
                    # Reflect the merge into our in-memory cache so subsequent
                    # candidates dedup against fresh data.
                    full_existing.update(payload)
                except Exception as exc:
                    log.error(
                        "merge update failed for %s: %s",
                        match_uid, exc,
                    )
                    continue
            updated += 1
            log.info(
                "merge update: %s — filled %d field(s) on %s",
                cand["opportunity_title"][:60],
                len([k for k in payload if k != "search_date"]),
                match_uid,
            )
            continue

        # INSERT PATH — totally new RFP.
        row = _build_row(cand, serial=i, ts=ts, policies=policies)
        if not dry_run:
            try:
                sb.table("rfp_submissions").insert(row).execute()
                existing.append({
                    "id": None,
                    "uid": row["uid"],
                    "opportunity_title": row["opportunity_title"],
                    "opportunity_link": row.get("opportunity_link"),
                    "opportunity_id": row.get("opportunity_id"),
                    "funding_agency": row.get("funding_agency"),
                    "brief_description": row.get("brief_description"),
                    "date_posted": row.get("date_posted"),
                    "submission_deadline": row.get("submission_deadline"),
                    "estimated_value": None,
                    "alignment_score": row.get("alignment_score"),
                    "geographic_scope": None,
                    "focus_theme": None,
                    "submitted_at": row["submitted_at"],
                    "is_duplicate": False,
                })
                inserted += 1
            except Exception as exc:
                log.error("insert failed for %s: %s", row["opportunity_title"][:60], exc)
        else:
            inserted += 1

    # Return the rejected count up the stack so it lands in scan_logs.
    log.info(
        "scan ingest: inserted=%d updated=%d unchanged_dups=%d rejected=%d",
        inserted, updated, duplicate_unchanged, rejected,
    )
    return (inserted + updated, duplicate_unchanged, rejected)
