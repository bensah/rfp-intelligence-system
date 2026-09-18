"""Automated web-search discovery — the channel that reaches calls no registry can.

WHY THIS EXISTS. The source registry is DONOR-KEYED: we list a funder, then crawl their
site. That model has a structural blind spot, and it has now cost us three live calls:

  * Fondation Pierre Fabre announced a call whose application calendar lived on odess.io.
  * UBS Optimus Foundation's "Outcomes Accelerator Cohort 5" mental-health CfP was
    published on outcomesaccelerator.org — a programme microsite run with FCDO and others
    — and never on the funder's own domain. It reached us through a LinkedIn share, two
    weeks before its EOI deadline.
  * The Audacious Project (TED-hosted) had no registry entry at all.

No amount of donor coverage reaches a call published on a partner's microsite under a
programme's name. A search engine does not care whose domain a call sits on, which makes
search the one discovery channel that is indifferent to the thing our registry keys on.

WHAT THIS IS NOT. It is not a second scraper, gate, or vocabulary. `web_search.search()`
already fans out across Serper / Tavily / Exa, expands one keyword across every health
pivot, and VERIFIES each hit by fetching the page and testing it with
`auto_scorer._RFP_STRONG_PHRASES` + `_has_rfp_acronym` + body-geography
(`web_search._fetch_signals`), dropping confidently-expired, very-old-with-no-future-
deadline, off-theme and foreign-geography results. All of that was reachable only from a
button in the UI. This module turns it into a crawl phase: it asks for the candidates and
hands them to `scan_pipeline.ingest_candidates`, which applies exactly the same gates,
enrichment, dedup and store-write as a candidate found by crawling a registered source.

SO A DISCOVERED CALL IS NOT PRIVILEGED. It must clear not-an-rfp, theme, deadline and the
rest on its own merits, and it lands in the SHARED extracted store, where every tenant's
own eligibility screening then decides whether it belongs in that tenant's pipeline. That
is the same route an extraction-crawl candidate takes.

COST. `all_pivots()` is 14 terms, so one sweep is 14 queries per configured provider
(Serper + Tavily today) — about 1,500 Serper queries a year against a 2,500 free tier.
Bounded further by `max_candidates`, and candidates already fresh in the store are dropped
before any enrichment is paid for.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# The label that marks a store row's provenance as the search channel rather than a
# registered source's crawl, so "where did this come from" stays answerable.
SOURCE_LABEL = "🔎 Web search — discovery"

# One broad term is deliberate: web_search.build_queries() expands a broad term across
# every health pivot (built-in + admin custom), so this is a full sweep, not one query.
# Overridable for a targeted run.
DEFAULT_TERMS = ("health",)


def enabled() -> bool:
    """Off only when no provider key is set, or explicitly disabled."""
    if os.environ.get("RFPIS_SEARCH_DISCOVERY", "1") == "0":
        return False
    try:
        from core import web_search
        return bool(web_search.available())
    except Exception:
        return False


def _norm(url: str) -> str:
    u = (url or "").strip().split("#", 1)[0]
    return u[:-1] if u.endswith("/") else u


def _provisional_funder(url: str) -> str | None:
    """A first guess at the funder from the host — e.g. 'outcomesaccelerator.org' →
    'Outcomesaccelerator'. PROVISIONAL on purpose.

    A search result carries no funder, and the store's funder column should not be left
    blank (the dedup matcher and the donor-intel join both read it). Enrichment and the LLM
    extractor run AFTER this and overwrite it with the real name off the page, so this only
    has to be non-empty and honest about where it came from. Deliberately NOT a
    registry lookup: inventing a confident funder name from a domain is how an aggregator
    ends up recorded as a donor, which `scan_pipeline`'s funder gate exists to stop.
    """
    host = urlsplit(url or "").netloc.lower().removeprefix("www.")
    if not host:
        return None
    label = host.split(".")[0]
    return label.replace("-", " ").title() or None


def discover(*, terms: tuple[str, ...] | list[str] | None = None,
             per_query: int = 10,
             max_candidates: int = 80,
             skip_fresh_days: int = 7,
             max_age_days: int = 540) -> dict[str, Any]:
    """Run a search sweep and return candidates ready for `ingest_candidates`.

    Returns {"candidates": [...], "stats": {...}} — never raises. `stats` is what the scan
    log prints, so a sweep that found nothing is distinguishable from one that could not
    run (the distinction the source-health telemetry exists to make elsewhere).
    """
    stats: dict[str, Any] = {"configured": False, "queries": 0, "raw": 0,
                             "verified": 0, "already_fresh": 0, "no_link": 0,
                             "candidates": 0, "errors": []}
    if not enabled():
        return {"candidates": [], "stats": stats}

    try:
        from core import web_search
        from core.found_loader import candidate_from_web_result
    except Exception as exc:
        stats["errors"].append(f"import: {type(exc).__name__}: {exc}")
        return {"candidates": [], "stats": stats}

    # Skip anything the store already refreshed recently — before enrichment, which is
    # where the cost is. Best-effort: an empty set just means nothing is skipped.
    fresh: set[str] = set()
    try:
        from core.extracted_store import recent_uids
        fresh = recent_uids(skip_fresh_days) or set()
    except Exception as exc:
        log.debug("search_discovery: recent_uids unavailable (%s)", exc)

    try:
        from core.extracted_store import make_uid
    except Exception:
        make_uid = None                       # type: ignore[assignment]

    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for term in (terms or DEFAULT_TERMS):
        try:
            res = web_search.search(str(term), num=per_query,
                                    max_age_days=max_age_days)
        except Exception as exc:
            stats["errors"].append(f"{term}: {type(exc).__name__}: {exc}")
            continue
        if not res or not res.get("ok"):
            if res and res.get("error"):
                stats["errors"].append(f"{term}: {str(res['error'])[:120]}")
            continue
        stats["configured"] = True
        stats["queries"] += len(res.get("queries") or [])
        stats["raw"] += int(res.get("raw_count") or 0)
        hits = res.get("results") or []
        stats["verified"] += len(hits)
        for wr in hits:
            link = _norm(wr.get("link") or "")
            if not link.lower().startswith("http"):
                stats["no_link"] += 1
                continue
            if link in seen:
                continue
            seen.add(link)
            if make_uid and fresh and make_uid(link) in fresh:
                stats["already_fresh"] += 1
                continue
            cand = candidate_from_web_result(wr)
            cand["opportunity_link"] = link
            if not cand.get("funding_agency"):
                cand["funding_agency"] = _provisional_funder(link)
            cand["_source_origin"] = SOURCE_LABEL
            # Stamp the host's real class so an aggregator hit is resolved to its primary
            # source by the pipeline instead of being recorded as a donor's own page. A
            # search sweep hits DevelopmentAid and fundsforNGOs constantly.
            try:
                from core import aggregators
                cand["_source_class"] = (
                    "aggregator" if aggregators.is_non_primary(link)[0] else "primary")
            except Exception:
                cand["_source_class"] = "primary"
            candidates.append(cand)
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break

    stats["candidates"] = len(candidates)
    return {"candidates": candidates, "stats": stats}


def summarize(stats: dict[str, Any]) -> str:
    """One log line. Says explicitly when the channel could not run, because a silent
    zero is the failure mode this whole area keeps producing."""
    if not stats.get("configured"):
        why = "; ".join(stats.get("errors") or []) or "no provider key configured"
        return f"Search discovery · NOT RUN ({why})"
    line = (f"Search discovery · {stats['queries']} queries · {stats['raw']} raw · "
            f"{stats['verified']} verified · {stats['already_fresh']} already fresh · "
            f"{stats['candidates']} candidate(s)")
    if stats.get("errors"):
        line += f" · {len(stats['errors'])} error(s)"
    return line
