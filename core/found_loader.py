"""Load a discovered opportunity (web-search result or recovered false-reject)
into rfp_submissions as a first-class, tracked candidate (Workstream A3 + A1
recovery).

The candidate is run through the SAME objective scorer the scan uses
(core.auto_scorer.auto_score) so it lands with derived criteria, an
alignment_score and an auto_recommendation — exactly like a scanned row — and is
tombstoned in the seen-ledger. CRUCIAL: the human `decision` column is left NULL.
A loaded result is a candidate AWAITING human review, not a human-coded label;
populating `decision` with the rule's output would make
scripts/harvest_human_decisions.py harvest it as a fake human label (leakage).
The real label is captured later when a reviewer decides on the Review screen.

Best-effort and assistive: dedupes against live records so the same opportunity
isn't tracked twice, and never raises into the caller (returns a status dict).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Criteria + derived fields auto_score returns that we carry onto the row.
_CARRY = (
    "qualification", "strategic_fit", "capacity", "geographic_fit", "cofinancing",
    "funding_quality", "funder_relationship", "competitiveness", "bid_effort",
    "feasibility", "alignment_score", "auto_recommendation",
    "decline_flags_present", "call_geographic_scope", "call_domain_areas",
)


def _gate_class(reason: str) -> str:
    """Classify an is_eligible rejection reason for the search-Track flow:
      source — a listing/aggregator/search page (route to Sources registry)
      hard   — clearly not an RFP (news/error/wrong type/blacklist) → reject
      soft   — theme/geo/country/deadline/language/feasibility/eligibility →
               allow as a human override, but note it."""
    r = (reason or "").lower()
    # Hard checks FIRST — a not-an-rfp reason can mention "blog"/"news" in its
    # text ("news / press / blog page") and must not be mistaken for a source.
    if any(k in r for k in ("not-an-rfp", "not an rfp", "type:", "blacklist",
                            "past-tense", "error", "unavailable")):
        return "hard"
    if any(k in r for k in ("aggregator", "blog", "listing", "lists / indexes",
                            "indexes calls", "calls-listing", "generic calls",
                            "search / filter")):
        return "source"
    return "soft"


def _parse_deadline(v: Any) -> str | None:
    if not v:
        return None
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def candidate_from_web_result(wr: dict) -> dict:
    """Map a web_search result dict → a candidate dict the scorer understands."""
    return {
        "opportunity_title": (wr.get("title") or "").strip()[:300] or None,
        "opportunity_link": wr.get("link"),
        "brief_description": (wr.get("snippet") or "").strip() or None,
        "call_submission_deadline": _parse_deadline(wr.get("deadline")),
        "date_posted": _parse_deadline(wr.get("page_date")),
        "funding_agency": (wr.get("funder") or "").strip() or None,
    }


def load_candidate(candidate: dict, user: dict | None = None, *,
                   provenance: str = "search") -> dict:
    """Score `candidate` and insert it into rfp_submissions (source='manual',
    decision NULL). Returns a status dict:
        {ok: bool, uid|None, skipped: bool, reason: str}
    skipped=True means it duplicates a record already tracked (not an error)."""
    title = (candidate.get("opportunity_title") or "").strip()
    if not title:
        return {"ok": False, "uid": None, "skipped": False,
                "reason": "no title"}
    try:
        from core.auto_scorer import auto_score
        from core.deduplicator import find_duplicates
        from core.policies import get_policies
        from core.review_week import review_week_label
        from core.uid_generator import generate_uid
        from db.supabase_client import get_client

        # Skip if it duplicates a live record (any source) — assistive, not a gate.
        try:
            if find_duplicates(candidate):
                return {"ok": False, "uid": None, "skipped": True,
                        "reason": "already tracked (duplicate)"}
        except Exception:
            pass

        policies = {}
        try:
            policies = get_policies()
        except Exception:
            pass

        # GATE search-Tracks before they reach Found Records (the final RFP store).
        # A listing/source page → route to the Sources registry to be CRAWLED; a
        # clear non-RFP (news, error, wrong type) → reject; soft misses (theme /
        # geo / deadline) are allowed through as a human override but noted.
        # reject-recovery is exempt — there the human is overriding the gate.
        _gate_note = None
        if provenance == "search":
            try:
                from core.auto_scorer import is_eligible
                _ok, _reason = is_eligible(candidate, policies)
            except Exception:
                _ok, _reason = True, ""
            if not _ok:
                cls = _gate_class(_reason)
                link = candidate.get("opportunity_link") or ""
                if cls == "source":
                    try:
                        from core import source_registry as _sr, aggregators as _ag
                        _k = _ag.classify(link)[0] if link else "unknown"
                        _sr.add_row(link, {
                            "classification": _k if _k in ("primary", "aggregator")
                            else "unknown", "status": "pending",
                            "sample_title": title[:300]},
                            by=(user or {}).get("email") or "web-track")
                    except Exception:
                        pass
                    return {"ok": False, "uid": None, "skipped": False,
                            "routed": "registry",
                            "reason": "source/listing page — added to the Sources "
                                      "registry to crawl (not a single RFP)."}
                if cls == "hard":
                    return {"ok": False, "uid": None, "skipped": False,
                            "rejected": True,
                            "reason": f"not a valid RFP — {_reason}"}
                _gate_note = _reason          # soft miss → allow but record why

        scored = {}
        try:
            scored = auto_score(candidate, policies) or {}
        except Exception as exc:
            log.debug("found_loader.auto_score failed: %s", exc)

        who = (user or {}).get("name") or (user or {}).get("email") or "search"
        now = datetime.now(timezone.utc).isoformat()
        uid = generate_uid(str(who))
        row: dict[str, Any] = {
            "uid": uid, "form_id": uid,
            "source": "manual",                 # human-selected; NOT 'auto'
            "submitted_by": who,
            "submitted_by_email": (user or {}).get("email"),
            "submitted_at": now,
            "search_date": now,
            "opportunity_title": title,
            "opportunity_link": candidate.get("opportunity_link"),
            "aggregator_url": candidate.get("_aggregator_link"),
            "brief_description": candidate.get("brief_description"),
            "call_submission_deadline": candidate.get("call_submission_deadline"),
            "date_posted": candidate.get("date_posted"),
            "funding_agency": candidate.get("funding_agency"),
            "review_week": review_week_label(),
            # provenance marker (the column is a free-text note) so these stay
            # identifiable as web/recovery loads in later analysis.
            "notes": (f"loaded via {provenance}"
                      + (f" · soft-gate: {_gate_note}" if _gate_note else "")),
            "decision": None,                   # awaiting human review — NO label
        }
        for k in _CARRY:
            if k in scored and scored[k] is not None:
                row[k] = scored[k]
        # Don't let the scorer's rule decision leak into the human column.
        row.pop("decision", None)
        row["decision"] = None

        from core.funder_names import canonical_funder as _canon_funder
        if row.get("funding_agency"):
            row["funding_agency"] = _canon_funder(row["funding_agency"])
        sb = get_client()
        sb.table("rfp_submissions").insert(row).execute()
        try:
            from core import seen_ledger
            seen_ledger.record_one(row, reason=provenance)
        except Exception:
            pass
        return {"ok": True, "uid": uid, "skipped": False,
                "reason": scored.get("auto_recommendation") or "loaded"}
    except Exception as exc:
        log.debug("found_loader.load_candidate failed: %s", exc)
        return {"ok": False, "uid": None, "skipped": False, "reason": str(exc)}
