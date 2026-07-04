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
                              theme_eligible)
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
        "submitted_by_email": "bdt@taadom.org",
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
    _derive_duration(candidate)          # mine inline "12-18 month" durations
    payload: dict[str, Any] = {}
    deadline = candidate.get("call_submission_deadline")
    posted = candidate.get("date_posted")
    candidate_normalized = {
        "opportunity_link": candidate.get("opportunity_link"),
        "opportunity_id": candidate.get("opportunity_id"),
        "funding_agency": candidate.get("funding_agency"),
        "brief_description": candidate.get("brief_description"),
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
    for field in _SCRAPE_MANAGED_FIELDS + _SCRAPE_STRUCTURED_FIELDS:
        new_val = candidate_normalized.get(field)
        old_val = existing_row.get(field)
        if _is_blank(new_val):
            continue
        if _is_blank(old_val):
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
            "call_geographic_scope": existing_row.get("call_geographic_scope") or [],
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
    extract_only: bool = False,
    llm_adjudicate: bool = False,
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
        if not dry_run and source_resolver.available() and _is_agg:
            try:
                if theme_eligible(cand, policies)[0]:
                    source_resolver.resolve_and_enrich(cand)
            except Exception as exc:
                log.debug("source resolve skipped: %s", exc)
        # Classify on both axes — solicitation (how to apply: NOFO/RFP/CFA/EOI/…)
        # and instrument (the contract: Grant/Cooperative Agreement/Loan/…). Carried
        # onto the inserted row + the reject record, and aggregated onto the source.
        cand["solicitation_type"] = (cand.get("solicitation_type")
                                     or type_detect.detect_solicitation(cand))
        cand["instrument_type"] = (cand.get("instrument_type")
                                   or type_detect.detect_instrument(cand))
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
                        and is_eligible(cand, policies, geo_org_gates=False)[0]):
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
                    _crit = {k: row.get(k) for k in (
                        "qualification", "strategic_fit", "capacity", "geographic_fit",
                        "cofinancing", "funding_quality", "funder_relationship",
                        "competitiveness", "bid_effort")}
                    _syn = llm_synthesis.synthesize(
                        cand, _orgp.get_profile(), row.get("auto_recommendation"), _crit)
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
                                    _dq = (sb.table("donor_intel").select("*")
                                           .ilike("donor", _fa).limit(1).execute().data or [])
                                    _dn = _dq[0] if _dq else None
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
                                    _cv, _mres["composite"], fatal=_isf)
            except Exception as _exc:
                log.debug("llm_synthesis skipped: %s", _exc)
        if not dry_run:
            try:
                sb.table("rfp_submissions").insert(row).execute()
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

    # Return the rejected count up the stack so it lands in scan_logs.
    log.info(
        "scan ingest: inserted=%d updated=%d unchanged_dups=%d "
        "suppressed_seen=%d rejected=%d",
        inserted, updated, duplicate_unchanged, suppressed_seen, rejected,
    )
    if extract_only:
        # "new" column carries the extracted count; nothing inserted into Screened.
        return (extracted, 0, rejected)
    # Previously-seen suppressions are de-dup outcomes, not new rows — fold them
    # into the duplicate count so KPIs/logs don't read them as fresh finds.
    return (inserted + updated, duplicate_unchanged + suppressed_seen, rejected)


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
    geo = row.get("call_geographic_scope")
    if not isinstance(geo, (list, tuple)):
        geo = [geo] if geo else []
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
        "date_posted": row.get("date_posted"),
        "source": row.get("source"),
        "_source_origin": row.get("source"),
        "_source_class": "primary",       # already extracted from a primary source
        "extraction_uid": row.get("uid"),
    }


# Marker stored in scan_logs.source for a screening ("Find my matches") run, so the
# UI can split Extraction history (the crawl) from Found-matches history (screening).
MATCH_RUN_LABEL = "🎯 Find my matches"


def run_screening(*, dry_run: bool = False, status: str = "Open",
                  triggered_by: str = "manual") -> dict:
    """Re-screen the internal extracted store against this tenant's policies.
    Returns {considered, eligible, added, already_tracked, rejected}. `eligible` =
    rows that passed the gate; `added` = TRULY new rfp_submissions inserts (vs
    `already_tracked` = eligible ones merge-updated because they were already in the
    pipeline). No external network calls."""
    import time as _time
    from core import extracted_store
    t0 = _time.time()

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
        eligible, dup, rejected = ingest_candidates(
            cands, dry_run=dry_run, llm_adjudicate=True)
        added = max(0, (_count() - before)) if not dry_run else 0
        res = {"considered": len(cands), "eligible": eligible, "added": added,
               "already_tracked": max(0, eligible - added), "rejected": rejected}
    if not dry_run:                              # log for the Eligible-funding history
        try:
            get_client().table("scan_logs").insert({
                "source": MATCH_RUN_LABEL, "triggered_by": triggered_by,
                "rfps_found": res["considered"], "rfps_new": res["eligible"],
                "rfps_duplicate": res["already_tracked"], "rfps_rejected": res["rejected"],
                "duration_sec": round(_time.time() - t0, 3), "errors": None,
            }).execute()
        except Exception as exc:
            log.debug("run_screening log failed: %s", exc)
    return res
