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
import re
from datetime import datetime, timezone
from typing import Any

from core import deep_read, source_resolver, live_check, seen_ledger
from core import aggregators, source_registry, scraper, type_detect
from core import extract as extraction        # extraction-first global store (shadow)
from core import deadline_extract             # confidence-gated deadline backstop
from core.auto_scorer import (auto_score, is_eligible, is_index_page,
                              theme_eligible, insufficient_data_reject)
from core.deduplicator import find_duplicates
from core.policies import get_policies
from core.review_week import review_week_label
from db.supabase_client import get_client

log = logging.getLogger(__name__)


def _iso_date(v: Any) -> str | None:
    """Coerce a date/datetime OR an ISO-ish string to 'YYYY-MM-DD', else None.
    Handles BOTH the scraper (date objects) and screening (Supabase returns dates
    as ISO strings). The old `.isoformat() if hasattr(...)` pattern silently
    dropped string deadlines — which is why screened rows lost their deadlines."""
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    s = str(v).strip()
    return s[:10] if (len(s) >= 10 and s[4] == "-" and s[7] == "-"
                      and s[:4].isdigit()) else None


def _apply_llm_judgment(cand: dict[str, Any]) -> None:
    """Fill missing structured fields from a stashed LLM judgment (set by
    auto_scorer when it consulted the judge during the gate). ADDITIVE only —
    regex/handler values always win; we just stop throwing away what the judge
    already extracted (deadline / amount / type / geography). Mutates `cand`."""
    j = cand.get("_llm_judgment")
    if not isinstance(j, dict):
        return
    if not cand.get("call_submission_deadline") and j.get("call_submission_deadline"):
        cand["call_submission_deadline"] = j["call_submission_deadline"]
    if cand.get("call_award_value") in (None, "", 0, "0") and j.get("call_award_value") is not None:
        cand["call_award_value"] = j["call_award_value"]
        if not cand.get("currency") and j.get("currency"):
            cand["currency"] = j["currency"]
    if not cand.get("solicitation_type") and j.get("solicitation_type"):
        cand["solicitation_type"] = j["solicitation_type"]
    if not cand.get("instrument_type") and j.get("instrument_type"):
        cand["instrument_type"] = j["instrument_type"]
    _geo = cand.get("call_geographic_scope")
    _has_geo = (bool(_geo) if isinstance(_geo, (list, tuple))
                else bool(str(_geo or "").strip()))
    if not _has_geo and j.get("call_geographic_scope"):
        cand["call_geographic_scope"] = j["call_geographic_scope"]


# Fields that the scraper provides. Re-scans may fill these in if currently
# NULL on the existing row, but NEVER overwrite a populated value (which
# might have been edited by a human).
_SCRAPE_MANAGED_FIELDS = (
    "opportunity_link",
    "opportunity_id",
    "funding_agency",
    "brief_description",
    "date_posted",
    "call_submission_deadline",
)

# Donor-extracted structured fields that auto_score does NOT emit. `_build_row`
# used to drop these, leaving Value / Program area / Geography empty on every
# scanned row even though the scraper had them (e.g. grants.gov estimatedFunding
# = 60000000, fundingActivityCategories = ["Health"]). Carried into the insert
# row and gap-filled on rescan. Mostly non-string (numeric / list), so they use
# a blank-aware check rather than the string-only rule.
_SCRAPE_STRUCTURED_FIELDS = (
    "call_award_value",
    "currency",
    "call_domain_areas",
    "call_geographic_scope",
    "funding_window",
    "funding_type",
    "project_duration",
    "submission_format",
    "focus_theme",
    "notes",
    # Distinct award-scope fields (migration 036) — grants.gov + LLM extractor.
    "call_award_floor",
    "call_award_ceiling",
    "total_program_funding",
    "expected_awards",
    "funding_opportunity_number",
)


def _is_blank(v: Any) -> bool:
    """True for None, empty string, or empty list — the 'no value yet' cases
    across string, numeric and list columns."""
    return v is None or v == "" or v == []


_URLISH_RE = re.compile(
    r"^(?:www\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}(?:[:/]\S*)?$", re.I)


def _normalize_link(link: Any) -> str | None:
    """Clean a candidate's opportunity_link into a real http(s) URL.

    JS-rendered SPA pages sometimes hand the anchor extractor a stray text / CSS node
    instead of an href (e.g. Coefficient Giving's "no pop-up should run above the
    sticky header…"). Returns:
      * the URL (https:// prepended to a scheme-less but valid host), or
      * "" when the link is empty (legit — it may be resolved later), or
      * None when it's present but NOT a URL → the caller drops the candidate."""
    s = str(link or "").strip()
    if not s:
        return ""
    if any(c in s for c in (" ", "\n", "\t")):
        return None
    if s.lower().startswith(("http://", "https://")):
        return s
    return ("https://" + s) if _URLISH_RE.match(s) else None


def _derive_duration(candidate: dict[str, Any]) -> None:
    """Fill candidate['project_duration'] (months) from the call's title + description
    when the source didn't provide it — most calls state duration only inline
    ('12-18 month research program'), so nothing set it before. Mutates in place;
    no-op when a duration is already present. See scraper.duration_months_from_text
    for the range/max policy (ceiling of the longest advertised engagement)."""
    if not _is_blank(candidate.get("project_duration")):
        return
    text = " ".join(str(candidate.get(k) or "") for k in
                    ("opportunity_title", "brief_description", "notes"))
    dur = scraper.duration_months_from_text(text)
    if dur:
        candidate["project_duration"] = dur


# Auto-scoring outputs. Refreshed only when the existing row is still
# "unreviewed" (alignment_score IS NULL). Once a human touches the Review
# tab, we treat the score & criteria as theirs.
_AUTOSCORE_FIELDS = (
    "feasibility",
    "qualification",
    "strategic_fit",
    "capacity",
    "geographic_fit",
    "cofinancing",
    "funding_quality",
    "funder_relationship",
    "competitiveness",
    "bid_effort",
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
    _derive_duration(candidate)          # mine inline "12-18 month" durations
    uid = _generate_auto_uid(serial, ts)
    iso_now = ts.replace(tzinfo=timezone.utc).isoformat()
    deadline = candidate.get("call_submission_deadline")
    posted = candidate.get("date_posted")
    row: dict[str, Any] = {
        "uid": uid,
        "form_id": uid,
        "source": "auto",
        "submitted_by": "auto-scan",
        # Default contact for auto-scanned rows. Avoids leaving the column
        # NULL — useful for filtering / replies on shared records.
        "submitted_by_email": "autoscan@example.org",
        "submitted_at": iso_now,
        "search_date": iso_now,
        "opportunity_title": candidate["opportunity_title"],
        "opportunity_id": candidate.get("opportunity_id"),
        "opportunity_link": candidate.get("opportunity_link"),
        # Canonicalisation: opportunity_link holds the PRIMARY url (the resolver
        # rewrote it); keep the aggregator we discovered it through for provenance.
        "aggregator_url": candidate.get("_aggregator_link"),
        "funding_agency": candidate.get("funding_agency"),
        "brief_description": candidate.get("brief_description"),
        "date_posted": _iso_date(posted),
        "call_submission_deadline": _iso_date(deadline),
        "review_week": review_week_label(),
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
        "solicitation_type": candidate.get("solicitation_type"),
        "instrument_type": candidate.get("instrument_type"),
    }
    row.update(auto_score(candidate, policies))
    # Carry over the donor's OWN structured fields. auto_score only emits the
    # criteria + a keyword-derived program_area/geography GUESS; the scraper's
    # donor-provided values (grants.gov estimatedFunding / "Health" category /
    # eligibility text) are richer and win. Without this, Value / Program area
    # / Geography were always empty on scanned rows.
    for col in _SCRAPE_STRUCTURED_FIELDS:
        val = candidate.get(col)
        if not _is_blank(val):
            row[col] = val
    ead = _iso_date(candidate.get("expected_award_date"))
    if ead:
        row["expected_award_date"] = ead
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
    from core.records import clean_brief          # never gap-fill a RAW brief
    _derive_duration(candidate)          # mine inline "12-18 month" durations
    payload: dict[str, Any] = {}
    deadline = candidate.get("call_submission_deadline")
    posted = candidate.get("date_posted")
    candidate_normalized = {
        "opportunity_link": candidate.get("opportunity_link"),
        "opportunity_id": candidate.get("opportunity_id"),
        "funding_agency": candidate.get("funding_agency"),
        # Only backfill an existing blank brief with a CLEAN one — clean_brief returns ""
        # for raw attachment/legalese text, which _is_blank() then skips (leaving NULL for a
        # later synthesis backfill; empty beats surfacing General-Conditions clauses).
        "brief_description": clean_brief(candidate.get("brief_description"),
                                         candidate.get("raw_text")) or None,
        "date_posted": _iso_date(posted),
        "call_submission_deadline": _iso_date(deadline),
        # Structured donor fields — gap-filled on rescan so EXISTING empty rows
        # get backfilled (estimated_value / program_area / geography / …).
        "call_award_value": candidate.get("call_award_value"),
        "currency": candidate.get("currency"),
        "call_domain_areas": candidate.get("call_domain_areas"),
        "call_geographic_scope": candidate.get("call_geographic_scope"),
        "funding_window": candidate.get("funding_window"),
        "funding_type": candidate.get("funding_type"),
        "project_duration": candidate.get("project_duration"),
        "submission_format": candidate.get("submission_format"),
        "focus_theme": candidate.get("focus_theme"),
        "notes": candidate.get("notes"),
    }
    conflicts: dict[str, Any] = {}
    for field in _SCRAPE_MANAGED_FIELDS + _SCRAPE_STRUCTURED_FIELDS:
        new_val = candidate_normalized.get(field)
        old_val = existing_row.get(field)
        if _is_blank(new_val):
            continue
        if _is_blank(old_val):
            payload[field] = new_val
        elif str(new_val).strip() != str(old_val).strip():
            # CONTRADICTION. The stored value WINS (a human may have curated it, and an
            # earlier scrape of the live page is not automatically worse than a later one),
            # but silently discarding the difference hid real changes — a funder moving a
            # deadline or restating an award size. Record it for human review instead.
            conflicts[field] = {"kept": old_val, "incoming": new_val,
                                "seen_at": datetime.now(timezone.utc).isoformat()}

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
            "call_geographic_scope": existing_row.get("call_geographic_scope") or [],
            "focus_theme": existing_row.get("focus_theme"),
        }
        scored = auto_score(merged_for_scoring, policies)
        for field in _AUTOSCORE_FIELDS:
            if field in scored:
                payload[field] = scored[field]

    # search_date is the FIRST-discovery date and is IMMUTABLE — a rescan must never
    # rewrite history (it used to, so months-old rows all showed today and every
    # search->submission cycle-time metric was wrong). "Last seen" gets its own column.
    payload["last_seen_at"] = datetime.now(timezone.utc).isoformat()
    if _is_blank(existing_row.get("search_date")):
        payload["search_date"] = payload["last_seen_at"]     # backfill only if never set
    if conflicts:
        # Merge over anything already flagged so earlier unreviewed conflicts aren't lost.
        prior = existing_row.get("merge_conflicts")
        if isinstance(prior, str):
            try:
                import json as _cj
                prior = _cj.loads(prior or "{}")
            except Exception:
                prior = {}
        payload["merge_conflicts"] = {**(prior if isinstance(prior, dict) else {}), **conflicts}
    return payload


# Bookkeeping written on EVERY rescan match — a payload carrying only these changed nothing
# the user cares about, so the run must not report it as an update.
_MERGE_BOOKKEEPING = {"search_date", "last_seen_at"}


def _payload_meaningful(payload: dict[str, Any]) -> bool:
    """A merge payload that only stamps last-seen bookkeeping is a no-op for the user.
    A recorded CONTRADICTION (merge_conflicts) IS meaningful — it needs human review."""
    return any(k not in _MERGE_BOOKKEEPING for k in payload.keys())


def ingest_candidates(
    candidates: list[dict[str, Any]],
    *,
    existing: list[dict[str, Any]] | None = None,
    dry_run: bool = False,
    extract_only: bool = False,
    llm_adjudicate: bool = False,
) -> tuple[int, int, int, int]:
    """Process a list of candidate dicts.

    Returns (new_or_updated, true_duplicate, rejected_by_policy, store_errors).
      * new_or_updated — inserted + merge-updates (counts as "rfps_new")
      * true_duplicate — matched existing canonical, no new info
      * rejected_by_policy — failed the country / theme / deadline /
        feasibility eligibility gate; never touched the DB
      * store_errors — extract_only rows that passed the gate but whose DB write
        failed (RLS/connectivity) — an infra error, surfaced apart from declines
    """
    # Per-SCAN, not per-process: the ceiling is a budget for this run, so a long-lived worker
    # does not carry a spent counter into the next scan and silently stop synthesising.
    try:
        extraction.reset_scan_synthesis()
    except Exception:
        pass

    if not candidates:
        return (0, 0, 0, 0)

    sb = None if dry_run else get_client()

    # Fetch existing canonicals ONCE per scan. We pull the FULL row now so
    # the merge logic can decide which fields are still empty.
    if existing is None and not dry_run:
        existing = (
            sb.table("rfp_submissions")
            .select(
                "id,uid,opportunity_title,opportunity_link,opportunity_id,"
                "funding_agency,brief_description,date_posted,"
                "call_submission_deadline,call_award_value,alignment_score,"
                "call_geographic_scope,focus_theme,submitted_at,is_duplicate"
            )
            .eq("is_duplicate", False)
            .execute()
            .data
            or []
        )
    existing = existing or []

    # Permanent tombstone ledger — the backstop that stops a previously-found RFP
    # re-entering after its live row was DELETED (live `existing` only sees rows
    # still in rfp_submissions). Loaded once per scan; best-effort ([] if the
    # table/migration 033 isn't there yet, so the scan still runs).
    seen = [] if dry_run else seen_ledger.fetch_all()

    policies = get_policies()

    inserted = 0
    updated = 0
    duplicate_unchanged = 0
    suppressed_seen = 0
    rejected = 0
    extracted = 0           # extract_only mode: rows written to the global store
    store_errors = 0        # extract_only: passed the gate but the DB WRITE failed
                            # (RLS/connectivity) — an infra error, NOT a gate decline
    _reject_records: list[dict] = []   # ML Phase 1 — labeled rejects for learning
    _source_encounters: list[dict] = []  # host registry — aggregator vs primary log
    _live_checks = 0                   # bounded HTTP liveness fetches this run
    _live_check_max = live_check.max_checks()
    ts = datetime.now()

    # LISTING-CHILDREN CRAWL: a candidate that is an index/listing/aggregator page
    # is a crawl SEED, not a single call. Walk it for its child opportunity links
    # so the actual calls get evaluated (aggregator children → resolved to primary
    # below; donor listing children → the real calls). Bounded + best-effort; the
    # original index still falls through to is_eligible and is rejected.
    if not dry_run:
        _MAX_LISTING_EXPAND = 8
        extra: list[dict] = []
        expanded = 0
        for cand in list(candidates):
            link = cand.get("opportunity_link") or ""
            if not link:
                continue
            if not (cand.get("_source_class") == "aggregator"
                    or aggregators.is_aggregator(link) or is_index_page(cand)):
                continue
            if expanded >= _MAX_LISTING_EXPAND:
                break
            expanded += 1
            try:
                kids = scraper.expand_listing(
                    link, source_name=cand.get("funding_agency") or "listing")
                extra.extend(kids)
            except Exception as exc:
                log.debug("listing expansion failed for %s: %s", link, exc)
        if extra:
            log.info("listing expansion: +%d child candidates from %d index page(s)",
                     len(extra), expanded)
            candidates = list(candidates) + extra

    for i, cand in enumerate(candidates):
        if not (cand.get("opportunity_title") or "").strip():
            continue
        # Scraped briefs (esp. WordPress RSS content:encoded) can be raw HTML — strip it
        # to clean text so no display ever shows literal <p>/<a> markup.
        if isinstance(cand.get("brief_description"), str) and "<" in cand["brief_description"]:
            from core.records import strip_html
            cand["brief_description"] = strip_html(cand["brief_description"])
        # Aggregator hits (DevelopmentAid) hide the real call behind a paywalled
        # listing. For theme-relevant ones, resolve to the donor's OWN source
        # page (Google/Serper) and fetch THAT, so the gate below sees the real
        # deadline / eligibility. Bounded to relevant hits to limit API spend.
        # Log the host we met (aggregator vs primary learning ledger), keyed off
        # the ORIGINAL link before any resolve rewrites it.
        _orig_link = cand.get("opportunity_link")
        try:
            _kind, _ = aggregators.classify(_orig_link, cand.get("opportunity_title"))
        except Exception:
            _kind = "unknown"
        # Aggregator (by curated source class OR host heuristic) → resolve the
        # title to the donor's OWN primary page before gating, so we EXTRACT the
        # primary rather than reject the aggregator. Primary sources skip this.
        _is_agg = (cand.get("_source_class") == "aggregator"
                   or aggregators.is_aggregator(_orig_link))
        # Aggregators are SEARCH BOOSTERS, not extraction targets (owner 2026-07-06):
        # we never store an aggregator URL. Use the listing's title + funder to find the
        # call's OWN primary page and extract THAT — but only for calls the PRIMARY
        # sources didn't already surface. Primaries ingest first (class-ordered), so
        # `existing` (DB + this run's inserts) already holds them; a match means "search
        # only what primaries missed", saving the paid lookup and keeping the primary as
        # canonical. Theme-agnostic in pure extraction (the store keeps every sector);
        # tenant screening keeps the theme pre-filter so we don't spend searches off-theme.
        if not dry_run and _is_agg and source_resolver.available():
            _agg_probe = {
                "opportunity_title": cand.get("opportunity_title"),
                "funding_agency": cand.get("funding_agency"),
                "opportunity_link": _orig_link,
                "call_submission_deadline": _iso_date(cand.get("call_submission_deadline")),
            }
            if find_duplicates(_agg_probe, existing=existing):
                duplicate_unchanged += 1
                log.info("aggregator: already found via a primary — skip search: %s",
                         (cand.get("opportunity_title") or "")[:60])
                continue
            if extract_only or theme_eligible(cand, policies)[0]:
                try:
                    source_resolver.resolve_and_enrich(cand)
                except Exception as exc:
                    log.debug("source resolve skipped: %s", exc)
        # AGGREGATOR = SEARCH BOOSTER, never a stored call. If this aggregator hit did
        # NOT resolve to the donor's OWN primary page (no Serper key, no match, or the
        # fetch failed), DROP it now — never insert an aggregator URL/funder (owner rule).
        # Belt-and-suspenders with the is_eligible non-primary reject; also skips the
        # wasted live-check / deep-read on a seed we're going to drop anyway.
        if _is_agg and not cand.get("_resolved_from_aggregator"):
            rejected += 1
            log.info("reject (unresolved aggregator): %s — %s",
                     (cand.get("funding_agency") or "")[:40],
                     (cand.get("opportunity_title") or "")[:50])
            _reject_records.append({**cand, "_reject_reason":
                                    "aggregator: not resolved to a primary source (dropped)"})
            continue
        # THE FUNDER IS PART OF THE SAME RULE. Dropping the aggregator's URL is only half
        # of "never store an aggregator": its LABEL was still riding along as the donor,
        # because resolution replaced the link and left funding_agency alone. 20 catalogue
        # rows and 7 pipeline rows ended up reading "DevelopmentAid Aggregator",
        # "FundsForNGOs", or a bare host as the funder — which is what a reviewer sees on
        # the opportunity page and in the rail. source_resolver now re-derives the name from
        # the resolved page; this is the backstop for any path that does not go through it.
        if source_resolver.is_aggregator_funder(cand.get("funding_agency")):
            rejected += 1
            log.info("reject (aggregator named as the funder): %s — %s",
                     (cand.get("funding_agency") or "")[:40],
                     (cand.get("opportunity_title") or "")[:50])
            _reject_records.append({**cand, "_reject_reason":
                                    "aggregator/host named as the funder (not a donor)"})
            continue
        # Classify on both axes — solicitation (how to apply: NOFO/RFP/CFA/EOI/…)
        # and instrument (the contract: Grant/Cooperative Agreement/Loan/…). Carried
        # onto the inserted row + the reject record, and aggregated onto the source.
        cand["solicitation_type"] = (cand.get("solicitation_type")
                                     or type_detect.detect_solicitation(cand))
        cand["instrument_type"] = (cand.get("instrument_type")
                                   or type_detect.detect_instrument(cand))
        # The coarse pursuit class the eligibility gate opts out of (procurement /
        # consultancy / …). Nothing populated this before, so the gate's type opt-out
        # never fired and goods tenders reached grant pipelines.
        cand["opportunity_type"] = (cand.get("opportunity_type")
                                    or type_detect.detect_opportunity_type(cand))
        # Deadline backstop: if the scraper captured no deadline, run the
        # confidence-gated extractor on the page text. A HIGH/MEDIUM date lets the
        # gate reject expired calls that would otherwise slip through (the scraper
        # misses deadlines in prose / FR "date limite" / mixed formats). Low-
        # confidence guesses are ignored so a genuinely rolling call isn't dropped.
        if not cand.get("call_submission_deadline") and not cand.get("extraction_uid"):
            try:
                from datetime import date as _date
                _dl = deadline_extract.extract_deadline(
                    cand.get("_page_text") or cand.get("brief_description") or "",
                    scan_year=_date.today().year,
                    title=cand.get("opportunity_title") or "")
                if (_dl["deadline"] and _dl["confidence"] in ("high", "medium")
                        and _dl["method"] != "default-rolling"):
                    cand["call_submission_deadline"] = _dl["deadline"]
            except Exception as _exc:
                log.debug("deadline backstop skipped: %s", _exc)
        # Link sanity: after any resolve rewrite, the opportunity_link must be a real
        # URL. Drop candidates whose link is a JS-SPA scrape artifact (stray text/CSS,
        # not an href) so the UI never shows an unclickable "Apply" link; normalise a
        # scheme-less-but-valid host in place.
        _norm_link = _normalize_link(cand.get("opportunity_link"))
        if _norm_link is None:
            rejected += 1
            log.info("reject (non-URL link): %r — %s",
                     str(cand.get("opportunity_link"))[:60],
                     (cand.get("opportunity_title") or "")[:50])
            _reject_records.append({**cand, "_reject_reason": "opportunity_link is not a URL"})
            continue
        cand["opportunity_link"] = _norm_link

        # First-pass eligibility gate (cheap: URL/title/keyword/deadline/scope).
        ok, reason = is_eligible(cand, policies, geo_org_gates=not extract_only,
                                 theme_gate=not extract_only,
                                 llm_adjudicate=llm_adjudicate)
        _source_encounters.append({
            "url": _orig_link, "title": cand.get("opportunity_title"),
            "detected": _kind, "accepted": ok,
            "solicitation_type": cand.get("solicitation_type"),
            "instrument_type": cand.get("instrument_type")})
        if not ok:
            # Geography / org rejects are still valid GLOBAL rows (geography is NOT
            # an extraction gate — DATA_SCHEMA_ETL.md §3). Shadow-capture them to
            # extracted_solicitations before dropping from THIS tenant's Screened
            # flow. Best-effort; never affects the scan.
            try:
                if (not cand.get("extraction_uid")
                        and is_eligible(cand, policies, geo_org_gates=False,
                                         theme_gate=False)[0]):
                    extraction.extract_and_store(cand, policies)
            except Exception as _exc:
                log.debug("shadow extract (reject path) skipped: %s", _exc)
            # Learn donor intel even from calls THIS org won't pursue by TYPE (the
            # capacity/CSA/training/loan/prize rejects) — the funder + its call docs are
            # still real donor intelligence. Restricted to `type:` rejects on purpose:
            # a geography/eligibility reject means the call's scope EXCLUDES us, so
            # blank-filling the donor's geographic profile from it could freeze a
            # partial/out-of-scope scope. ensure_donor stays conservative (on-theme,
            # namable only). Best-effort; never affects the scan.
            if not dry_run and reason.startswith("type:"):
                try:
                    from core import donor_enrich as _de
                    _de.enrich_donor_from_call(cand)
                except Exception as _dexc:
                    log.debug("donor enrichment (reject path) skipped: %s", _dexc)
            rejected += 1
            log.info("reject: %s — %s", cand.get("opportunity_title", "")[:60], reason)
            _reject_records.append({**cand, "_reject_reason": reason})
            continue

        # Cheap liveness + re-enrich (plain HTTP — runs on Cloud too, unlike the
        # Chromium deep-read below). For a thin candidate (no deadline / no
        # description, often a stale search hit), confirm the link is live and
        # pull a missing deadline / description from the FULL body, then RE-GATE.
        # Catches dead links (404 / soft-404 "page not found" bodies) and expired
        # deadlines buried in prose the listing snippet didn't carry.
        _thin = not (cand.get("brief_description") or "").strip()
        if (not dry_run and not cand.get("extraction_uid")
                and (_thin or not cand.get("call_submission_deadline"))
                and _live_checks < _live_check_max):
            _live_checks += 1
            try:
                fetched = live_check.recheck_and_enrich(cand)
            except Exception as exc:
                fetched = False
                log.debug("live-check skipped: %s", exc)
            if fetched:
                ok, reason = is_eligible(cand, policies, geo_org_gates=not extract_only,
                                 theme_gate=not extract_only,
                                 llm_adjudicate=llm_adjudicate)
                if not ok:
                    rejected += 1
                    log.info("reject (post live-check): %s — %s",
                             cand.get("opportunity_title", "")[:60], reason)
                    _reject_records.append({**cand, "_reject_reason": reason})
                    continue

        # Deep-read survivors that lack a deadline OR are too thin to judge
        # eligibility (no description) — render the page in Chromium to recover
        # the deadline / eligibility / geography, then RE-GATE on the accurate
        # data so a freshly found past deadline / excluded scope / non-call now
        # rejects. No-ops where Chromium isn't available (Streamlit Cloud);
        # active in the GitHub Actions scan (bounded by RFPIS_DEEP_READ_MAX).
        _thin = not (cand.get("brief_description") or "").strip()
        if ((not cand.get("call_submission_deadline") or _thin)
                and not cand.get("extraction_uid") and deep_read.available()):
            if deep_read.enrich(cand):
                ok, reason = is_eligible(cand, policies, geo_org_gates=not extract_only,
                                 theme_gate=not extract_only,
                                 llm_adjudicate=llm_adjudicate)
                if not ok:
                    rejected += 1
                    log.info("reject (post deep-read): %s — %s",
                             cand.get("opportunity_title", "")[:60], reason)
                    _reject_records.append({**cand, "_reject_reason": reason})
                    continue

        # Persist anything the gate's LLM judge already extracted (deadline /
        # amount / type / geography) instead of discarding it — additive, regex
        # values win. Flows into BOTH the global store and the Screened insert.
        _apply_llm_judgment(cand)

        # DATA-SUFFICIENCY hard gate (tenant Screened pipeline only): now that ALL
        # enrichment has run (aggregator resolve + live-check + deep-read + LLM
        # judgment), a call we STILL can't verify as a real, currently-open opportunity
        # — a blank stub, or no parseable/rolling/current deadline — is rejected rather
        # than inserted as a Decline the team has to wade through. Pure extraction
        # (extract_only) keeps everything for the shared store.
        if not extract_only:
            _bad, _why = insufficient_data_reject(cand)
            if _bad:
                rejected += 1
                log.info("reject (insufficient data): %s — %s",
                         (cand.get("opportunity_title") or "")[:60], _why)
                _reject_records.append({**cand, "_reject_reason": f"insufficient: {_why}"})
                continue

        # Capture the fully-enriched candidate to the GLOBAL extracted store —
        # UNLESS it already came FROM the store (screening / "My eligible funding"
        # carries extraction_uid). Skipping re-extraction here is what keeps
        # screening fast: no crawl, no LLM re-extraction, just scoring + insert.
        _stored_uid, _store_reason = None, "extraction_uid (already stored)"
        if not cand.get("extraction_uid"):
            try:
                _stored_uid, _store_reason = extraction.extract_and_store(cand, policies)
            except Exception as _exc:
                _store_reason = f"error: {_exc}"
                log.debug("extract_and_store skipped: %s", _exc)
        if extract_only:
            # PURE extraction (Run Extraction): write to the global store only —
            # no per-tenant Screened insert/scoring. Screening is the separate
            # "My eligible funding" run (run_screening). DATA_SCHEMA_ETL.md §2-3.
            # The store has its OWN gate (incl. LLM theme adjudication), so honour
            # its verdict: a row the store rejected (e.g. off-theme) is NOT counted
            # as extracted.
            if _stored_uid or cand.get("extraction_uid"):
                extracted += 1
            elif str(_store_reason).startswith(("store-error:", "error:")):
                # Passed the gate but the DB write failed (e.g. RLS 42501) — an infra
                # error, NOT a "not a fundable opportunity". Count + log it separately so
                # a store outage is unmistakable and never hides as a policy decline.
                store_errors += 1
                log.error("extract STORE ERROR (passed gate, write failed): %s — %s",
                          cand.get("opportunity_title", "")[:60], _store_reason)
            else:
                rejected += 1
                log.info("extract reject: %s — %s",
                         cand.get("opportunity_title", "")[:60], _store_reason)
                _reject_records.append({**cand, "_reject_reason": _store_reason})
            continue

        # Find duplicates using a minimal projection (find_duplicates only
        # reads these keys).
        probe = {
            "opportunity_id": cand.get("opportunity_id"),
            "opportunity_title": cand["opportunity_title"],
            "opportunity_link": cand.get("opportunity_link"),
            "funding_agency": cand.get("funding_agency"),
            "call_submission_deadline": _iso_date(cand.get("call_submission_deadline")),
            "call_award_value": None,
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
                    from db.supabase_client import safe_execute
                    safe_execute(sb.table("rfp_submissions").update(payload).eq(
                        "uid", match_uid))
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
                len([k for k in payload if k not in _MERGE_BOOKKEEPING]),
                match_uid,
            )
            continue

        # SUPPRESS PATH — no LIVE match, but this RFP was seen before and since
        # deleted. The permanent ledger remembers it, so never re-add it.
        if not dry_run and seen and find_duplicates(probe, existing=seen):
            suppressed_seen += 1
            log.info(
                "suppress (previously seen / deleted): %s",
                cand["opportunity_title"][:60],
            )
            continue

        # INSERT PATH — totally new RFP.
        row = _build_row(cand, serial=i, ts=ts, policies=policies)
        # Direct application-portal URL (from extraction) so Tracking can show an
        # "Apply" button; fall back to the opportunity link.
        row["apply_url"] = (cand.get("apply_url") or cand.get("opportunity_link"))
        # LLM review-synthesis — ONLY for gate-passed rows we're about to insert
        # (Decline/Park/Proceed); rejected candidates never reach here, so we
        # never spend tokens on them. One call writes the reasoning fields a
        # reviewer needs: synthesised brief, focus areas, top risk, decision
        # rationale (drafts; human edits win — risk/rationale only set if blank).
        if not dry_run:
            try:
                from core import llm_synthesis, org_profile as _orgp
                if llm_synthesis.is_enabled():
                    # Ensure synthesis reads the FULL RFP, not just the listing
                    # preamble. Deep-read only ran above for thin/undated candidates, so
                    # a rich call that arrived with a deadline + snippet never got its
                    # full page → the model summarised the generic boilerplate (the two
                    # near-identical Grand Challenges briefs). Fetch the page now
                    # (cron/Chromium only) when we don't already have it.
                    if not (cand.get("_page_text") or "").strip() and deep_read.available():
                        try:
                            deep_read.enrich(cand)
                        except Exception as _dre:
                            log.debug("deep-read for synthesis skipped: %s", _dre)
                    _crit = {k: row.get(k) for k in (
                        "qualification", "strategic_fit", "capacity", "geographic_fit",
                        "cofinancing", "funding_quality", "funder_relationship",
                        "competitiveness", "bid_effort")}
                    _syn = llm_synthesis.synthesize(
                        cand, _orgp.get_profile(), row.get("auto_recommendation"), _crit)
                    if not _syn:
                        log.warning(
                            "llm_synthesis produced nothing (LLM error/timeout?) — "
                            "storing RAW brief for %s",
                            (row.get("opportunity_title") or "")[:60])
                    if _syn:
                        if _syn.get("brief_description"):
                            row["brief_description"] = _syn["brief_description"]
                        if _syn.get("call_domain_areas"):
                            row["call_domain_areas"] = _syn["call_domain_areas"]
                        if _syn.get("key_risks") and not row.get("key_risks"):
                            row["key_risks"] = _syn["key_risks"]
                        if _syn.get("decision_rationale") and not row.get("decision_note"):
                            row["decision_note"] = _syn["decision_rationale"]
                        if _syn.get("how_to_apply"):
                            row["how_to_apply"] = _syn["how_to_apply"]
                        if _syn.get("compliance_requirements"):
                            row["compliance_requirements"] = _syn["compliance_requirements"]
                        if _syn.get("application_checklist"):
                            row["application_checklist"] = _syn["application_checklist"]
                        if _syn.get("eligibility_specifics"):
                            row["eligibility_specifics"] = _syn["eligibility_specifics"]
                        # Structured award value / duration the LLM read from a (possibly
                        # ranged) call — FILL ONLY when the scraper/regex extractor left
                        # them blank, so the LLM is a fallback and never clobbers a
                        # regex-parsed figure. Feeds PREFER-6 / MUST-3 sizing.
                        if _syn.get("call_award_value") and not row.get("call_award_value"):
                            row["call_award_value"] = _syn["call_award_value"]
                            # The LLM figure is ALREADY in USD (prompt: call_award_value_usd),
                            # so stamp currency=USD — otherwise _usd() would re-apply the
                            # row's stale native FX rate and double-convert the amount.
                            row["currency"] = "USD"
                        if _syn.get("project_duration") and not row.get("project_duration"):
                            row["project_duration"] = _syn["project_duration"]
                        # CLOSE THE LOOP: feed the LLM-extracted RFP compliance flags
                        # into MUST-5, then re-derive cofinancing + re-score so the
                        # stored decision reflects hard-gates the call itself states.
                        _flags = _syn.get("call_compliance_flags") or {}
                        if _flags:
                            import json as _json
                            row["call_compliance_flags"] = _json.dumps(_flags)   # persist for Review re-merge
                            from core import criteria_derive as _cdv, matching as _mm
                            from core import settings as _settings
                            from core.scorer import CRITERIA as _CR
                            from core.auto_scorer import recommend_from_composite as _rec
                            _prof = _orgp.get_profile()
                            _dn = None
                            try:
                                _fa = (row.get("funding_agency") or "").strip()
                                if _fa:
                                    # Robust resolution (acronym / short / full name) so a
                                    # call whose funder string differs from the stored donor
                                    # name (e.g. "Grand Challenges" → "Bill & Melinda Gates
                                    # Foundation") still joins its donor intel — an exact
                                    # ilike missed these, starving the donor-fallback.
                                    from core.donor_intel import match_donor as _match_donor
                                    _dn = _match_donor(_fa, fuzzy=False)
                            except Exception:
                                _dn = None
                            # Re-derive BOTH call-flag-sensitive labels: MUST-1
                            # qualification + MUST-5 cofinancing. If EITHER changed,
                            # recompute the composite + fatal gate so the stored
                            # decision reflects hard-gates the call itself states.
                            _changed = False
                            _newqual = _cdv.derive_qualification(
                                _prof, row, _dn, _settings.get_org(), rfp_compliance=_flags)
                            if _newqual and _newqual != row.get("qualification"):
                                row["qualification"] = _newqual
                                _changed = True
                            _newcap = _cdv.derive_capacity(
                                _prof, row, _dn, _settings.get_org(), rfp_compliance=_flags)
                            if _newcap and _newcap != row.get("capacity"):
                                row["capacity"] = _newcap
                                _changed = True
                            _newcof = _cdv.derive_cofinancing(
                                _prof, row, _dn, rfp_compliance=_flags,
                                org_settings=_settings.get_org())
                            if _newcof and _newcof != row.get("cofinancing"):
                                row["cofinancing"] = _newcof
                                _changed = True
                            if _changed:
                                _cv = {k: row.get(k) for k in _CR}
                                _mres = _mm.composite_match({**row, **_cv}, _prof, _dn,
                                                            _settings.get_org())
                                row["alignment_score"] = round(_mres["composite"], 1)
                                # Re-evaluate the fatal gate with the RFP's own
                                # compliance flags folded in (a call-stated floor
                                # can now flip the decision even with no donor row).
                                _isf, _ = _cdv.fatal_decline(
                                    _prof, row, _dn, _settings.get_org(),
                                    rfp_compliance=_flags)
                                row["auto_recommendation"] = _rec(
                                    _cv, _mres["composite"], fatal=_isf,
                                    below_award_floor=_cdv.below_award_floor(row, _prof))
                else:
                    # Visible, not debug: a mis-set cron env (LLM base URL/key) silently
                    # disables synthesis, so every brief is the raw scraped preamble.
                    log.warning(
                        "llm_synthesis DISABLED (set LLM_JUDGE_BASE_URL + "
                        "LLM_JUDGE_API_KEY, or LLM_SYNTH_*) — storing RAW brief for %s",
                        (row.get("opportunity_title") or "")[:60])
            except Exception as _exc:
                log.debug("llm_synthesis skipped: %s", _exc)
        # ── RE-GATE AFTER ENRICHMENT ──────────────────────────────────────────
        # The gate ran on the SCRAPED candidate. Synthesis (and the deep-read above) can
        # then LEARN gate-relevant facts the listing never showed — the geographic scope,
        # the programme areas, a fuller brief. A row whose true scope only appears at this
        # point was admitted on evidence that no longer reflects it: that is how a
        # Honduras-only tender reached a Cameroon pipeline (its scope was learned AFTER
        # the gate and never re-checked). Re-run the SAME gate on the enriched row and drop
        # it if it no longer qualifies. Only fires when enrichment actually changed a
        # gate-relevant field, so a normal row costs nothing.
        if not extract_only:
            _gate_fields = ("call_geographic_scope", "call_domain_areas",
                            "brief_description", "opportunity_type", "focus_theme")
            _enriched = {k: row.get(k) for k in _gate_fields
                         if row.get(k) not in (None, "", [], cand.get(k))}
            if _enriched:
                _recheck = {**cand, **_enriched}
                try:
                    _ok2, _why2 = is_eligible(_recheck, policies, geo_org_gates=True,
                                              theme_gate=True,
                                              llm_adjudicate=llm_adjudicate)
                except Exception as _rexc:
                    _ok2, _why2 = True, ""        # never let a re-gate error drop a row
                    log.debug("re-gate after enrichment skipped: %s", _rexc)
                if not _ok2:
                    rejected += 1
                    log.info("reject (post-enrichment): %s — %s",
                             (cand.get("opportunity_title") or "")[:60], _why2)
                    _reject_records.append({**_recheck,
                                            "_reject_reason": f"post-enrichment: {_why2}"})
                    continue

        if not dry_run:
            try:
                from db.supabase_client import safe_execute
                # FINAL brief guard: synthesis above should have replaced the raw brief,
                # but on an LLM failure/timeout/cap it leaves the raw attachment text in
                # place ("[General_conditions.pdf] … 1.1 …"). Never persist that — clean it,
                # and NULL it when it's still raw so the render shows a neutral line and the
                # synthesis backfill can fill it later. (raw_text/_page_text kept for grounding.)
                from core.records import clean_brief as _clean_brief
                row["brief_description"] = _clean_brief(
                    row.get("brief_description"),
                    cand.get("raw_text") or cand.get("_page_text")) or None
                safe_execute(sb.table("rfp_submissions").insert(row))
                # Tombstone immediately so it's remembered even if later deleted.
                seen_ledger.record_one(row, reason="ingested")
                # Enrich the donor CRM with any contacts the call carried (e.g. UNGM
                # notice Contacts tab). No-op unless the candidate has _contacts AND its
                # funder resolves to a donor_intel row. Best-effort — never breaks ingest.
                try:
                    from core import donor_contacts as _dc
                    _dc.push_from_candidate(cand)
                except Exception as _dexc:
                    log.debug("donor_contacts push skipped: %s", _dexc)
                # E3: fill BLANK donor requirements from this call's compliance signals
                # (from_call provenance, never overwrites human/non-blank). Best-effort.
                try:
                    from core import donor_enrich as _de
                    _de.enrich_donor_from_call(cand)
                except Exception as _eexc:
                    log.debug("donor enrichment skipped: %s", _eexc)
                existing.append({
                    "id": None,
                    "uid": row["uid"],
                    "opportunity_title": row["opportunity_title"],
                    "opportunity_link": row.get("opportunity_link"),
                    "opportunity_id": row.get("opportunity_id"),
                    "funding_agency": row.get("funding_agency"),
                    "brief_description": row.get("brief_description"),
                    "date_posted": row.get("date_posted"),
                    "call_submission_deadline": row.get("call_submission_deadline"),
                    "call_award_value": None,
                    "alignment_score": row.get("alignment_score"),
                    "call_geographic_scope": None,
                    "focus_theme": None,
                    "submitted_at": row["submitted_at"],
                    "is_duplicate": False,
                })
                inserted += 1
            except Exception as exc:
                log.error("insert failed for %s: %s", row["opportunity_title"][:60], exc)
        else:
            inserted += 1

    # ML Phase 1 — persist the rejects as labeled training data (best-effort,
    # deduped by link; never breaks the scan if the table/DB is unavailable).
    if _reject_records and not dry_run:
        try:
            from core import decision_log
            decision_log.log_rejects(_reject_records)
        except Exception as exc:
            log.debug("decision_log unavailable: %s", exc)

    # Host registry — record every source we met this scan (aggregator vs primary),
    # so new hosts surface for human confirmation. Best-effort (no-op pre-mig-034).
    if _source_encounters and not dry_run:
        try:
            source_registry.record_encounters(_source_encounters)
        except Exception as exc:
            log.debug("source_registry unavailable: %s", exc)

    # Return the rejected count up the stack so it lands in scan_logs. The 4th value,
    # store_errors, is DB-write failures (extract_only) — surfaced apart from declines.
    # Say how much of the scan's time went on synthesis, and whether the ceiling was reached —
    # a scan that stops synthesising halfway should say so rather than look complete.
    try:
        _synth = extraction.scan_synthesis_calls()
        if _synth:
            log.info("scan ingest: synthesised %d row(s) at scan time%s", _synth,
                     "  (per-scan ceiling reached — the rest is for the backfill)"
                     if _synth >= extraction._SCAN_SYNTH_MAX else "")
    except Exception:
        pass
    log.info(
        "scan ingest: inserted=%d updated=%d unchanged_dups=%d "
        "suppressed_seen=%d rejected=%d store_errors=%d",
        inserted, updated, duplicate_unchanged, suppressed_seen, rejected, store_errors,
    )
    if extract_only:
        # "new" column carries the extracted count; nothing inserted into Screened.
        return (extracted, 0, rejected, store_errors)
    # Previously-seen suppressions are de-dup outcomes, not new rows — fold them
    # into the duplicate count so KPIs/logs don't read them as fresh finds.
    return (inserted + updated, duplicate_unchanged + suppressed_seen, rejected, store_errors)


# ---------------------------------------------------------------------------
# Screening run (extraction-first, tenant-side) — DATA_SCHEMA_ETL.md §2.
# "Find my matches": read the GLOBAL extracted_solicitations store and re-screen
# it against THIS tenant's policies — NO external crawl, so it's fast (seconds).
# Reuses ingest_candidates (same gate + scoring + dedup + insert), so Screened
# results are identical to a live scan, minus the slow network round-trips.
# ---------------------------------------------------------------------------
def _candidate_from_extracted(row: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct a scan-candidate dict from a stored extracted_solicitations row
    so it can flow back through ingest_candidates. raw_text is supplied as
    _page_text so the thin-candidate enrichment (live-check / deep-read) is skipped
    — the data is already extracted, so screening stays crawl-free."""
    # Repair double-encoded scope (some legacy store rows hold a JSON-array STRING) so
    # the geo gate sees real terms, not one opaque '["Europe", ...]' string.
    from core import geographies as _geos
    geo = _geos.flatten_scope_terms(row.get("call_geographic_scope"))
    # Stamp _source_class from the ACTUAL host, not a blanket "primary". Stamping
    # every store row "primary" used to bypass the is_eligible non-primary reject and
    # let aggregator URLs (DevelopmentAid / fundsforNGOs) re-enter screening. A row
    # whose URL is a known aggregator/blog host is tagged non-"primary" so the reject
    # fires; genuine primary/unknown hosts keep flowing through.
    _url = row.get("opportunity_url") or ""
    try:
        from core import aggregators as _aggr
        _src_class = "aggregator" if _aggr.is_non_primary(_url)[0] else "primary"
    except Exception:
        _src_class = "primary"
    # Donor's DECLARED geography — the SILENT-CALL fallback for the geo gate (owner rule:
    # gate a geo-silent call on the donor's declared geography; pass permissively only
    # when the donor is geo-silent too). Transient (underscore) — never persisted.
    try:
        from core import donor_intel as _di
        _donor_geo = _di.declared_geo(row.get("funder_name"))
    except Exception:
        _donor_geo = None
    return {
        "opportunity_title": row.get("opportunity_name"),
        "opportunity_link": row.get("opportunity_url"),
        "opportunity_id": row.get("opportunity_id"),
        "brief_description": row.get("brief_description"),
        # Use the SHORT synthesized brief for the gate (the row already passed the
        # extraction theme/not-rfp gate) — avoids re-running heavy regex over the
        # full 20k-char raw_text, which is what made screening slow.
        "_page_text": (row.get("brief_description")
                       or (row.get("raw_text") or "")[:3000]),
        "funding_agency": row.get("funder_name"),
        "call_submission_deadline": row.get("deadline"),
        "call_award_value": row.get("grant_amount"),
        "currency": row.get("currency"),
        "call_geographic_scope": list(geo),
        "solicitation_type": row.get("solicitation_type"),
        "instrument_type": row.get("instrument_type"),
        "opportunity_type": row.get("opportunity_type"),
        # Restore applicant types for the applicant-type match gate during screening.
        "_applicant_types": row.get("eligibility_applicant_types") or None,
        # ...and the applicant COUNTRIES, which were extracted, stored, then dropped here.
        # The store held ["Finland"] for a Finnish government scheme and ["Vietnam"] for the
        # Viet Nam programme; because this mapping had no entry for the column, the geo gate
        # never saw either and both reached a tenant registered in neither. The gate reads a
        # single field, `call_geographic_scope`, so the correct answer was sitting one column
        # away the whole time. Plumbing gap, not an extraction gap.
        "eligibility_countries": row.get("eligibility_countries") or None,
        "eligibility_other": row.get("eligibility_other") or None,
        "date_posted": row.get("date_posted"),
        "source": row.get("source"),
        "_source_origin": row.get("source"),
        "_source_class": _src_class,       # host-derived (see above), NOT blanket primary
        "_donor_geo": _donor_geo,          # donor's declared geo (silent-call fallback)
        "extraction_uid": row.get("uid"),
    }


# Marker stored in scan_logs.source for a screening ("Find my matches") run, so the
# UI can split Extraction history (the crawl) from Found-matches history (screening).
MATCH_RUN_LABEL = "🎯 Find my matches"


def run_screening(*, dry_run: bool = False, status: str = "Open",
                  triggered_by: str = "manual", tenant_id: str | None = None) -> dict:
    """Re-screen the internal extracted store against this tenant's policies.
    Returns {considered, eligible, added, already_tracked, rejected}. `eligible` =
    rows that passed the gate; `added` = TRULY new rfp_submissions inserts (vs
    `already_tracked` = eligible ones merge-updated because they were already in the
    pipeline). No external network calls.

    `tenant_id` runs the screen HEADLESSLY as that tenant (no Streamlit session) — used
    by the cron per-tenant loop. It forces the tenant context for the whole run so
    get_policies(), org_profile, the get_client scoping wrapper (insert-stamp) and the
    scan_logs entry all target that tenant. When omitted, it defaults to the SESSION
    tenant (current_tenant_id) so an in-session run always stamps inserts to a tenant —
    including a super_user's run, which would otherwise insert tenant_id=NULL orphans
    (the get_client wrapper leaves super_user writes unscoped)."""
    from auth import tenant_context as _tc
    _tid = tenant_id
    if not _tid:
        # Default the write-target to the tenant this session is SCOPED to, which for a
        # super_user is the tenant they're VIEWING (su_view_tenant) — NOT current_tenant_id()
        # (their HOME). Otherwise a super_user who runs "Find my matches" while viewing
        # tenant X would screen the store into their own home pipeline instead of X's
        # (the cross-tenant write leak, H5). Falls back to current_tenant_id() when the
        # scope helper can't resolve (e.g. it's importable but returns None).
        try:
            from db.supabase_client import _tenant_scope_tid
            _tid = _tenant_scope_tid()
        except Exception:
            _tid = None
        if not _tid:
            try:
                _tid = _tc.current_tenant_id()
            except Exception:
                _tid = None
    _tok = _tc.set_tenant_override(_tid) if _tid else None
    try:
        return _run_screening_body(dry_run=dry_run, status=status,
                                   triggered_by=triggered_by)
    finally:
        if _tok is not None:
            _tc.reset_tenant_override(_tok)


def unscoped_screening_reason(policies: dict | None = None) -> str:
    """Why this tenant must not be auto-screened yet, or "" when it is safe to screen.

    A TENANT WITH NO DECLARED PROGRAMME AREAS HAS NO THEME FILTER AT ALL. `_blank_policies`
    sets `themes.required_any = []` (policies.py, "no theme gate -> every sector
    populates"), `_seed_themes_from_profile` cannot repair it because the profile declares
    no areas, and `theme_eligible` short-circuits on an empty list with "no theme
    requirements set" - a pass for every candidate. Geography alone then decides, so a
    health-focused tenant is offered anti-slavery funds, road-resurfacing tenders and
    foreign trade-promotion schemes.

    The docstring on `screen_all_tenants` describes the opposite intent - "a fresh tenant
    with a minimal profile gets many rows, mostly Decline" (Option C). Measured on the run
    that prompted this: 31 rows, none decided, 28 with no readable description, and roughly
    19 of them rejectable on theme alone had a theme list existed. That is not a gentle
    default, it is an unreadable review week, and the owner asked for it to stop
    (2026-08-16): do not screen a tenant that has declared nothing to screen against.

    Deliberately checks the RESOLVED policy rather than the profile directly, so a tenant
    that sets an explicit scan policy instead of programme areas still screens.
    """
    from core.policies import get_policies
    pol = policies if policies is not None else get_policies()
    required = ((pol.get("themes") or {}).get("required_any") or [])
    if not required:
        return ("the organisation has declared no programme areas, so there is no theme "
                "filter to screen against - set programme areas in the organisation "
                "profile (or an explicit scan policy) first")
    return ""


def _run_screening_body(*, dry_run: bool, status: str, triggered_by: str) -> dict:
    import time as _time
    from core import extracted_store
    t0 = _time.time()

    # Refuse rather than flood. Recorded in scan_logs with the reason in `errors`, so the
    # run reads as a blocked onboarding step rather than as a week that found nothing.
    _blocked = unscoped_screening_reason()
    if _blocked:
        log.warning("run_screening: skipped - %s", _blocked)
        if not dry_run:
            _srow = {"source": MATCH_RUN_LABEL, "triggered_by": triggered_by,
                     "rfps_found": 0, "rfps_new": 0, "rfps_duplicate": 0,
                     "rfps_rejected": 0, "duration_sec": round(_time.time() - t0, 3),
                     "errors": f"screening skipped: {_blocked}"}
            try:
                from auth.tenant_context import current_tenant_id as _ctid
                _t = _ctid()
                if _t:
                    _srow["tenant_id"] = _t
            except Exception:
                pass
            try:
                get_client().table("scan_logs").insert(_srow).execute()
            except Exception as _exc:                       # logging must not break the run
                log.debug("run_screening: could not log the skip: %s", _exc)
        return {"considered": 0, "eligible": 0, "added": 0, "already_tracked": 0,
                "rejected": 0, "skipped": _blocked}

    def _count() -> int:
        try:
            return get_client().table("rfp_submissions").select(
                "id", count="exact").limit(1).execute().count or 0
        except Exception:
            return 0

    rows = extracted_store.list_extracted(status=status, limit=5000)
    cands = [_candidate_from_extracted(r) for r in rows if r.get("opportunity_url")]
    if not cands:
        res = {"considered": 0, "eligible": 0, "added": 0,
               "already_tracked": 0, "rejected": 0}
    else:
        before = 0 if dry_run else _count()
        # llm_adjudicate=True: regex-first, then LLM ONLY for silent-geography
        # survivors (bounded by the per-process LLM call cap).
        eligible, dup, rejected, _store_err = ingest_candidates(
            cands, dry_run=dry_run, llm_adjudicate=True)   # screening: no store writes
        added = max(0, (_count() - before)) if not dry_run else 0
        res = {"considered": len(cands), "eligible": eligible, "added": added,
               "already_tracked": max(0, eligible - added), "rejected": rejected}
    if not dry_run:                              # log for the Eligible-funding history
        # A screening / "Find my matches" run is TENANT-SPECIFIC — stamp the current
        # tenant so its notification is shown only to that tenant (migration 074). The
        # system-wide discovery crawl (run_scan.py) leaves tenant_id NULL → shown to all.
        _row = {
            "source": MATCH_RUN_LABEL, "triggered_by": triggered_by,
            "rfps_found": res["considered"], "rfps_new": res["eligible"],
            "rfps_duplicate": res["already_tracked"], "rfps_rejected": res["rejected"],
            "duration_sec": round(_time.time() - t0, 3), "errors": None,
        }
        try:
            from auth.tenant_context import current_tenant_id as _ctid
            _tid = _ctid()
            if _tid:
                _row["tenant_id"] = _tid
        except Exception:
            pass
        try:
            get_client().table("scan_logs").insert(_row).execute()
        except Exception as exc:
            # Retry without tenant_id in case migration 074 isn't applied yet, so the
            # run is still logged (just without tenant scoping).
            if "tenant_id" in _row:
                _row.pop("tenant_id", None)
                try:
                    get_client().table("scan_logs").insert(_row).execute()
                except Exception as exc2:
                    log.debug("run_screening log failed: %s", exc2)
            else:
                log.debug("run_screening log failed: %s", exc)
    return res


def _active_tenants() -> list[dict[str, Any]]:
    """Active tenants (id, name, is_platform), name-ordered. Empty on any error."""
    try:
        from db.supabase_client import service_client
        return (service_client().table("tenants")
                .select("id,name,status,is_platform")
                .eq("status", "active").order("name").execute().data or [])
    except Exception as exc:
        log.error("could not list tenants: %s", exc)
        return []


def is_multitenant_deploy(tenants: list[dict[str, Any]] | None = None) -> bool:
    """True for a MULTI-TENANT deployment. Two signals:
      * the JWT master switch is on (multitenant_enabled), OR
      * the DB has >= 2 active NON-platform tenants.

    The second signal matters for the CRON: per-tenant screening writes via the headless
    override (a ContextVar), which is JWT-INDEPENDENT — so the Friday scan can populate
    every tenant's pipeline even when SUPABASE_JWT_SECRET isn't in the Actions env, and the
    app (which DOES have the secret) then reads those tenant-tagged rows scoped correctly.
    A genuine single-tenant deploy (JWT off, < 2 real tenants) returns False, so run_scan
    falls back to a normal full ingest instead of tenant-tagging a lone seeded tenant."""
    try:
        from auth.tenant_context import multitenant_enabled
        if multitenant_enabled():
            return True
    except Exception:
        pass
    rows = tenants if tenants is not None else _active_tenants()
    return sum(1 for t in rows if not t.get("is_platform")) >= 2


def screen_all_tenants(*, dry_run: bool = False, triggered_by: str = "cron",
                       status: str = "Open") -> dict[str, dict]:
    """Headless per-tenant screening (the cron's Option-C step). For each ACTIVE tenant,
    screen the SHARED extracted store against THAT tenant's own policies + profile and
    insert tenant-tagged rfp_submissions — so the Friday discovery reaches every tenant
    automatically (a fresh tenant with a minimal profile gets many rows, mostly Decline).
    Run AFTER an --extract-only crawl. Returns {tenant_id: screening result}."""
    tenants = _active_tenants()
    # No-op only in a genuine SINGLE-tenant deploy — NOT merely because SUPABASE_JWT_SECRET
    # is absent from this (cron) env. Screening stamps tenant_id via the headless override,
    # which works without the JWT switch, so a multi-tenant deploy (>=2 active non-platform
    # tenants) is populated even when the cron env lacks the secret. Guarding on the JWT
    # switch alone silently skipped the Friday auto-populate whenever the secret wasn't set
    # as an Actions secret.
    if not is_multitenant_deploy(tenants):
        log.info("screen_all_tenants: single-tenant deploy — skipping per-tenant screening")
        return {}
    out: dict[str, dict] = {}
    for t in tenants:
        tid = t.get("id")
        if not tid:
            continue
        # Skip the platform/owner tenant (super_user's intentionally-blank home) — its
        # empty policy would flood it with Decline rows every run.
        if t.get("is_platform"):
            log.info("screen_all_tenants: skipping platform tenant %s", t.get("name"))
            continue
        try:
            out[str(tid)] = run_screening(dry_run=dry_run, status=status,
                                          triggered_by=triggered_by, tenant_id=str(tid))
            log.info("screen_all_tenants: %s (%s) → %s", t.get("name"), tid, out[str(tid)])
        except Exception as exc:
            log.error("screen_all_tenants: tenant %s (%s) failed: %s",
                      t.get("name"), tid, exc)
            out[str(tid)] = {"error": str(exc)}
    return out
