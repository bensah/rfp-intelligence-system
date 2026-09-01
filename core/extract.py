"""Extraction orchestrator (DATA_SCHEMA_ETL.md §2–§4, A-2).

Turns a scraped candidate into an `extracted_solicitations` record:
  1. EXTRACTION GATE — auto_scorer.is_eligible(..., geo_org_gates=False): the hard
     gates MINUS geography/org (not-an-rfp, opportunity-type, language, off-theme,
     deadline-past). Geography is captured but NOT gated (moves to the scorer).
  2. STRUCTURAL EXTRACTION — regex/heuristics reused from the existing pipeline:
     solicitation/instrument type, amount+currency, geographic scope (exact, all
     geos), and the confidence-gated deadline (core.deadline_extract).
  3. WRITE — upsert into the global store (core.extracted_store).

LLM SYNTHESIS fields (brief/full description, eligibility bullets, focus themes,
applicant fit) are populated by a later shadow-mode pass — left None here so this
stage is deterministic and offline-testable. An optional `llm_arbiter` only
resolves ambiguous deadlines.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date
from typing import Any

from core import deadline_extract, extracted_store, geographies, type_detect
from core.auto_scorer import is_eligible

log = logging.getLogger(__name__)

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _blob(c: dict[str, Any]) -> str:
    return " ".join(str(c.get(k) or "") for k in
                    ("opportunity_title", "brief_description", "_page_text"))


def _as_iso(v: Any) -> str | None:
    """Coerce a date-ish value to YYYY-MM-DD, or None (so a non-ISO string never
    breaks a Postgres `date` insert)."""
    if not v:
        return None
    s = str(v).strip()
    if _ISO_RE.match(s):
        return s
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", s)   # DD/MM/YYYY
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if d > 12 and mo <= 12:
            d, mo = d, mo
        elif mo > 12:
            d, mo = mo, d
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None


def _amount(title: str, text: str) -> tuple[float | None, str | None]:
    """Reuse the scraper's amount/currency regex (best-effort)."""
    try:
        from core.scraper import _extract_amount
        return _extract_amount(title or "", text or "")
    except Exception:
        return None, None


# Anti-hallucination grounding: a money expression must actually appear in the
# source text before we trust an LLM-reported amount. Matches a number attached
# to a currency cue ($/€/£/USD/…) OR a scale word (million/billion/k), so a bare
# count like "over a billion people" never reads as money.
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
          "bn": 1e9, "billion": 1e9}
_MONEY_RE = re.compile(
    r"(?:US?\$|USD|EUR|GBP|CAD|AUD|CHF|ZAR|€|£|\$)\s?([\d.,]+)\s*"
    r"(k|thousand|m|mn|million|bn|billion)?"
    r"|([\d.,]+)\s*(k|thousand|m|mn|million|bn|billion)\b",
    re.I)


def _money_values_in_text(text: str) -> set[float]:
    out: set[float] = set()
    for m in _MONEY_RE.finditer(text or ""):
        num = m.group(1) or m.group(3)
        scale = (m.group(2) or m.group(4) or "").lower()
        if not num:
            continue
        try:
            v = float(num.replace(",", "").rstrip("."))
        except ValueError:
            continue
        out.add(v * _SCALE.get(scale, 1))
    return out


def _amount_val(v) -> float | None:
    """Coerce a value to a positive float, or None. Shared by the synthesis
    range-fill so "1,200,000" / "$300000" / 800000 all normalise."""
    if v in (None, "", 0, "0") or isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _amount_grounded(amount, text: str) -> bool:
    """True if `amount` (±1%) matches a real money expression in the text."""
    if amount in (None, 0, "0", ""):
        return True
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return True
    return any(v > 0 and abs(v - a) / max(v, a) <= 0.01
              for v in _money_values_in_text(text))


def tiers_to_bounds(tiers: Any) -> tuple[float | None, float | None]:
    """Overall award (floor, ceiling) across staged/tiered funding.

    Each tier contributes its OWN representative edges. A "Tier 1: up to US$300,000"
    tier gives amount_max=300000 with amount_min null-or-0, so the lower edge of the
    award SPECTRUM is that tier's cap (300k), not None — giving a real 300k–800k range
    across ["up to 300k", "up to 600k", "up to 800k"] rather than a lone 800k figure.
    0 / negative are treated as "no bound" (models emit amount_min=0 for "up to X")."""
    if not isinstance(tiers, (list, tuple)) or not tiers:
        return None, None

    def _num(x):
        return (x if isinstance(x, (int, float)) and not isinstance(x, bool)
                and x > 0 else None)

    lo_edges, hi_edges = [], []
    for t in tiers:
        if not isinstance(t, dict):
            continue
        lo, hi = _num(t.get("amount_min")), _num(t.get("amount_max"))
        if lo is None and hi is None:
            continue
        lo_edges.append(lo if lo is not None else hi)   # this tier's lower edge
        hi_edges.append(hi if hi is not None else lo)   # this tier's upper edge
    return (min(lo_edges) if lo_edges else None,
            max(hi_edges) if hi_edges else None)


def build_record(candidate: dict[str, Any], policies: dict[str, Any], *,
                 scan_year: int | None = None, use_llm: bool = True,
                 llm_arbiter=None) -> tuple[dict[str, Any] | None, str]:
    """Build an extracted_solicitations record (NOT yet stored), or (None, reason)
    if the extraction gate rejects it. `reason` is the gate verdict either way.
    `use_llm=True` activates the LLM fallback (regex-first) for fields regex misses
    — amounts in prose, staged tiers, missed deadlines, type, geography."""
    scan_year = scan_year or date.today().year
    title = (candidate.get("opportunity_title") or "").strip()
    url = candidate.get("opportunity_link") or ""
    text = candidate.get("_page_text") or candidate.get("brief_description") or ""
    blob = _blob(candidate)

    # Extract structural details FIRST, then enrich a candidate copy so the gate's
    # _has_request_details / deadline checks see them — mirrors the real pipeline,
    # where the scraper sets submission_deadline / estimated_value before the gate.
    amt, cur = _amount(title, text)
    # A source handler may already carry a STRUCTURED amount (e.g. EU F&T
    # budgetOverview) — that's authoritative; prefer it over regex/LLM.
    _cand_val = candidate.get("call_award_value")
    if _cand_val not in (None, "", 0, "0"):
        try:
            amt = float(_cand_val)
            cur = candidate.get("currency") or cur
        except (TypeError, ValueError):
            pass
    dl = deadline_extract.extract_deadline(text, scan_year=scan_year, title=title,
                                           llm_arbiter=llm_arbiter)
    cand = dict(candidate)
    if not cand.get("call_submission_deadline") and dl["deadline"]:
        cand["call_submission_deadline"] = dl["deadline"]
    if cand.get("call_award_value") in (None, "", 0, "0") and amt is not None:
        cand["call_award_value"] = amt

    # The global store is theme-AGNOSTIC (owner 2026-07-06): keep every real RFP; the
    # sector/theme match happens later, per-tenant, in the eligibility screener (a
    # construction RFP is fundable for a construction org). So the gate here enforces
    # only "is this a genuine RFP" — theme_gate=False.
    ok, reason = is_eligible(cand, policies, geo_org_gates=False, theme_gate=False)
    if not ok:
        return None, reason

    # geography — EXACT, all geos. Handler-provided scope is authoritative; the regex
    # region-detection is a base but prone to INCIDENTAL mentions (e.g. a India RFP
    # that name-drops "South Africa") — so the LLM scope, when consulted, OVERRIDES
    # the regex part below (handler scope is always kept).
    handler_geo: set[str] = set()
    cand_geo = candidate.get("call_geographic_scope")
    if isinstance(cand_geo, (list, tuple)):
        handler_geo = {str(g).strip() for g in cand_geo if str(g).strip()}
    elif isinstance(cand_geo, str) and cand_geo.strip():
        handler_geo = {cand_geo.strip()}
    # broad_geos_in_text covers UN regions AND income/development TIERS (LMICs, LDCs,
    # Global South) — regions_in_text missed the tiers, so those signals were lost.
    geo = handler_geo | set(geographies.broad_geos_in_text(blob.lower()))

    # LLM FALLBACK (regex-first) — call the LLM when regex left a real gap (no
    # amount, or no/low-confidence deadline), OR when the text looks STAGED /
    # multi-amount (regex finds only the first figure, so tiers need the LLM).
    # It reads amounts hidden in prose, staged tiers, missed deadlines, type, geo.
    # Free via Ollama Cloud; is_enabled() guards on creds+SDK (else stays regex-only).
    _staged = bool(re.search(
        r"\b(proof of concept|transition to scale|stage\s*\d|phase\s*\d|"
        r"tier\s*\d|seed (?:fund|grant)|scale[-\s]?up)\b", text, re.I))
    _multi_amt = len(re.findall(r"(?:US?\$|EUR|GBP|€|£|\$)\s?\d", text)) >= 2
    # Pre-compute the regex/handler type so the LLM ALSO fires when type /
    # instrument / geography is missing — not only amount/deadline gaps. Maximises
    # enrichment (more high-quality structured fields) on the developer-side
    # extraction scans, where latency is not user-facing. Free/cheap via Ollama.
    _regex_type = (candidate.get("solicitation_type")
                   or type_detect.detect_solicitation(candidate))
    _regex_instr = (candidate.get("instrument_type")
                    or type_detect.detect_instrument(candidate))
    _gap = ((amt is None) or (not dl["deadline"]) or (dl["confidence"] == "low")
            or _staged or _multi_amt
            or not _regex_type or not _regex_instr or not geo)
    llm = None
    if use_llm and _gap and len(text) >= 120:
        try:
            from core import llm_judge
            if llm_judge.is_enabled():
                llm = llm_judge.judge(
                    {"opportunity_title": title, "opportunity_link": url,
                     "brief_description": text}, policies)
        except Exception as _exc:
            log.debug("llm extraction fallback skipped: %s", _exc)

    def _llm(k):
        return llm.get(k) if llm else None

    prov: dict[str, Any] = {}
    # Handler-provided type wins (structured), then regex detect, then LLM.
    sol_type = _regex_type or _llm("solicitation_type")
    instr_type = _regex_instr or _llm("instrument_type")

    # amount + currency: regex first, LLM fallback.
    g_amt, g_cur = amt, cur
    _amt_is_llm = False
    if g_amt is None and _llm("call_award_value") is not None:
        g_amt = _llm("call_award_value")
        _amt_is_llm = True
        prov["grant_amount"] = {"method": "llm", "confidence": "medium", "source_tier": "T1"}
    elif g_amt is not None:
        prov["grant_amount"] = {"method": "regex", "confidence": "medium", "source_tier": "T1"}
    if not g_cur:
        g_cur = _llm("currency")

    # staged / tiered amounts (LLM only) -> derive floor/ceiling + headline amount.
    # Each tier contributes its OWN representative bounds: a "Tier 1: up to US$300,000"
    # gives amount_max=300k with amount_min null, so the lower edge of the AWARD SPECTRUM
    # is the smallest tier cap (300k) and the upper edge the largest (800k). Using
    # min(amount_min) alone collapsed the floor to None for these "up to X" tier tables
    # (the GC pathogen-sequencing case) — the call publishes a 300k–800k range, not a
    # single 800k figure.
    tiers = _llm("funding_tiers") or []
    floor, ceil = tiers_to_bounds(tiers)
    if tiers:
        if g_amt is None and ceil is not None:        # headline = largest tier
            g_amt = ceil
            _amt_is_llm = True
        prov["funding_tiers"] = {"method": "llm", "source_tier": "T1"}

    # ── Anti-hallucination: drop any LLM-sourced amount NOT grounded in the source
    # text (a real money expression of the same magnitude). Regex/handler amounts
    # are grounded by construction, so only the LLM paths are double-checked.
    if g_amt is not None and _amt_is_llm and not _amount_grounded(g_amt, text):
        log.info("extract: dropping ungrounded LLM amount %s for %s", g_amt, url)
        g_amt = None
        prov.pop("grant_amount", None)
    if floor is not None and not _amount_grounded(floor, text):
        floor = None
    if ceil is not None and not _amount_grounded(ceil, text):
        ceil = None
        tiers = []          # tiers we can't ground are untrustworthy → drop
        prov.pop("funding_tiers", None)

    # deadline: HANDLER-provided structured date wins (e.g. EU F&T deadlineDate);
    # else regex (high/med); else LLM; else regex low/none.
    d_val, d_conf, d_method, d_window = (dl["deadline"], dl["confidence"],
                                         dl["method"], dl["funding_window"])
    _cand_dl = _as_iso(candidate.get("call_submission_deadline"))
    if _cand_dl:
        d_val, d_conf, d_method = _cand_dl, "high", "handler"
        d_window = d_window or "One-off"
    elif (d_val is None or d_conf == "low") and _llm("call_submission_deadline"):
        d_val, d_conf, d_method = _llm("call_submission_deadline"), "medium", "llm"
        d_window = d_window or "One-off"
    prov["deadline"] = {"method": d_method, "confidence": d_conf, "source_tier": "T1"}

    # LLM scope (when consulted) is more accurate than regex at telling the CALL's
    # geography from incidental mentions → it REPLACES the regex guesses (handler
    # scope is always kept). e.g. GC India RFP that name-drops "South Africa".
    _llm_geo = {str(g).strip() for g in (_llm("call_geographic_scope") or []) if str(g).strip()}
    if _llm_geo:
        geo = handler_geo | _llm_geo
    # Normalise EVERY captured term to the broad-geography vocabulary so scope is
    # comparable across sources (canonical countries + LMIC/SSA/Global-South signals);
    # terms not in the library are kept verbatim (free-text scope is allowed).
    geo = {geographies.canonical_geo(g) for g in geo}
    geo.discard("")
    # A bare "global"/"international" is almost always part of an ORG/PLATFORM NAME
    # ("United Nations Global Marketplace", "Global Fund", "International Labour
    # Organization"), NOT a worldwide-eligibility scope. Keep the worldwide tier only when
    # the text has a GENUINE worldwide phrase — otherwise a country-restricted call gets
    # falsely opened worldwide (the geo-leak class). Guards ALL geo sources at one point.
    if "Global / worldwide" in geo and not geographies.worldwide_ok(blob):
        geo.discard("Global / worldwide")

    funding_status = "Closed" if (llm and llm.get("is_closed")) else "Open"
    overall_conf = "high" if (llm and llm.get("confidence") == "high") else d_conf

    # BRIEF — a CLEAN, sentence-case, plain-language synthesis of THIS call for the global
    # store (org-NEUTRAL). Grounded on the full page text (up to 20k chars). This is the
    # single store choke point: screening copies this synthesised brief, so the raw
    # attachment text ("[General_conditions.pdf] GENERAL CONDITIONS OF CONTRACT…") never
    # reaches a reviewer. NEVER fall back to raw — if synthesis is disabled / capped /
    # fails, leave brief_description NULL for a later backfill (empty beats ALL-CAPS
    # legalese). The org-specific reasoning (key_risks / decision) is added per tenant at
    # the rfp_submissions insert.
    _store_brief = None
    _syn: dict[str, Any] = {}
    if use_llm and len(text) >= 120:
        try:
            from core import llm_synthesis
            _neutral = llm_synthesis.synthesize_store(cand)
            if _neutral:
                _syn = _neutral
                if _neutral.get("brief_description"):
                    _store_brief = _neutral["brief_description"]
        except Exception as _sexc:
            log.debug("store synthesis skipped (%s): %s", url, _sexc)

    # KEEP WHAT THE SYNTHESIS ALREADY GAVE US. This call returns the whole org-neutral
    # read of the RFP — programme areas, eligibility specifics, compliance requirements,
    # how to apply, and a structured award value / duration it recovered from ranged text —
    # and only `brief_description` was ever stored. Everything else was computed, paid for
    # in tokens, and thrown away, which is why `call_domain_areas`, `submission_format`,
    # `eligibility_other` and `project_duration` read 0-of-500 across the catalogue and the
    # opportunity page looked empty. Nothing here changes the prompt or the cost; it stops
    # discarding the answer.
    def _syn_text(*keys) -> str | None:
        """First non-blank synthesis value across `keys`, joined when several are set."""
        parts = []
        for k in keys:
            v = _syn.get(k)
            if v is None:
                continue
            v = str(v).strip()
            if v and v.lower() not in ("none stated", "none", "n/a", "not stated"):
                parts.append(v)
        return "\n".join(parts) or None

    _syn_areas = [a for a in (_syn.get("call_domain_areas") or []) if str(a).strip()]
    # The LLM reads a RANGED award / duration the regex cannot ("up to $2M over 24-36
    # months"). It fills ONLY where the structural extractor came back blank, so a figure
    # read straight off the page always wins.
    _syn_amount = _syn.get("call_award_value")
    _syn_duration = _syn.get("project_duration")
    # RANGE fallback: when the structured extractor found no tiers, the synthesis may still
    # have read a ranged award ("grants of EUR 1–3 million", "up to $2M"). Fill floor/ceiling
    # from it, grounded like every LLM amount. Only when the judge left them blank.
    _sf = _amount_val(_syn.get("call_award_floor"))
    _sc = _amount_val(_syn.get("call_award_ceiling"))
    if floor is None and _sf is not None and _amount_grounded(_sf, text):
        floor = _sf
    if ceil is None and _sc is not None and _amount_grounded(_sc, text):
        ceil = _sc

    rec = {
        "uid": extracted_store.make_uid(url, title),
        "opportunity_name": title or None,
        "opportunity_url": url or None,
        "opportunity_id": candidate.get("opportunity_id"),
        "funding_opportunity_number": candidate.get("funding_opportunity_number"),
        "funder_name": candidate.get("funding_agency"),
        "source": candidate.get("source") or candidate.get("_source_origin"),
        "source_uid": candidate.get("source_uid"),
        "date_posted": _as_iso(candidate.get("date_posted")),
        "solicitation_type": sol_type,
        "instrument_type": instr_type,
        "opportunity_type": candidate.get("opportunity_type"),
        "call_geographic_scope": sorted(geo),
        "eligibility_applicant_types": candidate.get("eligibility_applicant_types") or [],
        # Structural extraction first; the synthesis fills a blank, never overrides.
        "grant_amount": g_amt if g_amt else _syn_amount,
        "project_duration": _syn_duration or None,
        "currency": g_cur,
        "call_award_floor": floor,
        "call_award_ceiling": ceil,
        "funding_tiers": tiers,
        "deadline": d_val,
        "deadline_confidence": d_conf,
        "funding_window": d_window,
        "funding_status": funding_status,
        # CLEAN synthesised brief (org-neutral, sentence-case) — NEVER the raw attachment
        # text. NULL when synthesis is unavailable (backfilled later). The raw page text is
        # preserved separately in raw_text for grounding + backfill.
        "brief_description": _store_brief,
        # Schema §4.3-§4.6 fields the synthesis produces (see the note above).
        "call_domain_areas": _syn_areas or None,
        "eligibility_other": _syn_text("eligibility_specifics",
                                       "compliance_requirements"),
        "submission_format": _syn_text("how_to_apply"),
        "raw_text": (text or None) and str(text)[:20000],
        "content_hash": hashlib.sha1(blob.encode("utf-8")).hexdigest(),
        "extraction_confidence": overall_conf,
        "field_provenance": prov,
        "solicitation_language": "English",
    }
    return rec, reason


# ---------------------------------------------------------------------------
# synthesis at SCAN TIME
# ---------------------------------------------------------------------------
# The nine §4 narrative/eligibility fields had a writer (core.catalog_synthesis) that only ever
# ran from a backfill script. So every row a scan added arrived EMPTY and stayed empty until
# somebody remembered to run the backfill by hand — the opportunity page showed dashes for the
# newest calls, which are the ones anyone is actually looking at.
#
# Synthesis now runs inside the store path, before the upsert, so ONE write lands a populated
# row rather than an empty one that a later pass has to come back and fill.
#
# THREE THINGS BOUND IT, because this is on the ingest path and a model call is ~12 seconds:
#
#   1. A PER-SCAN CEILING. Default 30 calls — about six minutes of added wall clock. A scan that
#      brings in more than that leaves the remainder to the backfill, which is what it is for.
#      `RFPIS_SCAN_SYNTH_MAX=0` turns scan-time synthesis off entirely.
#   2. NOTHING IS RE-PAID FOR. `build_record` rebuilds these fields as None every time, so on a
#      re-scan they look missing even when the stored row has them. Without checking the stored
#      row first, every weekly scan would re-synthesise the whole catalogue. The existing values
#      are read once and carried onto the record, which also means the upsert cannot regress
#      them.
#   3. IT NEVER RAISES. A synthesis failure — rate limit, timeout, bad JSON — must not cost the
#      scan the row it just extracted. The row is stored either way; the fields stay blank and
#      the backfill can fill them later.
#
# No HTML is available here (the scan carries page TEXT only), so `attachments` and
# `resource_links` still need the backfill's `--fetch-html`. The reading fields do not.
_SCAN_SYNTH_MAX = int(os.environ.get("RFPIS_SCAN_SYNTH_MAX", "30"))
_scan_synth_calls = 0

# Present on the stored row ⇒ the model has already read this call.
_ALREADY_SYNTHESISED = ("full_description", "applicant_fit_profile", "what_is_funded")


def scan_synthesis_calls() -> int:
    """Model calls spent on synthesis during this scan — for the scan log."""
    return _scan_synth_calls


def reset_scan_synthesis() -> None:
    global _scan_synth_calls
    _scan_synth_calls = 0


def _synthesise_in_place(rec: dict[str, Any]) -> None:
    """Fill the §4 fields on `rec` before it is written. Best-effort; never raises."""
    global _scan_synth_calls
    if _SCAN_SYNTH_MAX <= 0 or _scan_synth_calls >= _SCAN_SYNTH_MAX:
        return
    try:
        from core import catalog_synthesis as _cs
        if not _cs.is_enabled():
            return
        # Carry what is already stored, so a re-scan neither re-pays for it nor regresses it.
        existing = extracted_store.get_extracted(rec.get("uid") or "") or {}
        # CARRY FIRST, then decide whether to spend a call. `extracted_store._clean` happens to
        # drop None so an absent field could not regress a stored one anyway — but that is a
        # guarantee living in another module, and this record should be correct on its own terms.
        for k, v in existing.items():
            if v is not None and rec.get(k) is None:
                rec[k] = v
        if any(str(existing.get(f) or "").strip() for f in _ALREADY_SYNTHESISED):
            return                                 # the model has already read this call
        got = _cs.synthesize_row(rec)
        if got:
            _scan_synth_calls += 1
            rec.update(got)
            log.info("extract: synthesised %d field(s) at scan time for %s",
                     len(got), rec.get("uid"))
    except Exception as exc:                       # never cost the scan the row
        log.warning("extract: scan-time synthesis skipped for %s: %s",
                    rec.get("uid"), exc)


def extract_and_store(candidate: dict[str, Any], policies: dict[str, Any], *,
                      scan_year: int | None = None, use_llm: bool = True,
                      llm_arbiter=None) -> tuple[str | None, str]:
    """Build + upsert into extracted_solicitations. Returns (uid|None, reason).

    A None uid with a `store-error:` reason means the record PASSED the gate but the DB
    WRITE failed (RLS/connectivity) — an infra error the caller must count apart from a
    gate DECLINE (a genuine "not a fundable opportunity"). A gate reject returns the
    gate's own verdict as the reason."""
    rec, reason = build_record(candidate, policies, scan_year=scan_year,
                               use_llm=use_llm, llm_arbiter=llm_arbiter)
    if rec is None:
        return None, reason                       # gate DECLINE — reason = gate verdict
    _synthesise_in_place(rec)
    uid = extracted_store.upsert_extracted(rec)
    if uid is None:                               # gate PASSED but the write failed
        return None, "store-error: write to extracted_solicitations failed (see log)"
    return uid, reason


if __name__ == "__main__":  # offline smoke test (gate + build, no store write)
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass
    from core.policies import get_policies
    samples = [
        {"opportunity_title": "Nexa Funding Opportunity: Africa, Latin America and the Caribbean",
         "opportunity_link": "https://www.grandchallenges.ca/funding-opportunity-nexa/",
         "funding_agency": "Grand Challenges Canada", "_source_class": "primary",
         "source": "Grand Challenges Canada",
         "brief_description": "Call for proposals. We are investing in bold health "
         "solutions across Africa enabling local health actors to act on climate-driven "
         "health risks. Grants up to US$250,000. Apply by July 22, 2026."},
        {"opportunity_title": "Call for research projects on adaptation in the Mediterranean",
         "opportunity_link": "https://www.afd.fr/en/calls/mediterranean", "_source_class": "primary",
         "source": "AFD",
         "funding_agency": "AFD",
         "brief_description": "Call for proposals: funding for climate and health "
         "adaptation research across the Mediterranean basin (Southern Europe, North "
         "Africa). Award up to EUR 500,000. Deadline 30 September 2026."},
    ]
    pol = get_policies()
    for c in samples:
        rec, reason = build_record(c, pol, scan_year=2026)
        print(f"\n### {c['opportunity_title'][:55]}  -> {reason}")
        if rec:
            for k in ("call_geographic_scope", "deadline", "deadline_confidence",
                      "funding_window", "grant_amount", "currency",
                      "solicitation_type", "funding_status"):
                print(f"   {k}: {rec[k]}")
