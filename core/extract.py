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
    if not cand.get("submission_deadline") and dl["deadline"]:
        cand["submission_deadline"] = dl["deadline"]
    if cand.get("call_award_value") in (None, "", 0, "0") and amt is not None:
        cand["call_award_value"] = amt

    # llm_theme=True: let the LLM adjudicate AMBIGUOUS theme calls (excluded/
    # required conflict or a thin incidental keyword match) at the extraction
    # gate. Dev-side path (latency OK); the verdict is cached for the enrichment
    # call below, so it's effectively one LLM call.
    ok, reason = is_eligible(cand, policies, geo_org_gates=False, llm_theme=True)
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
    geo = handler_geo | set(geographies.regions_in_text(blob.lower()))

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
    tiers = _llm("funding_tiers") or []
    floor = ceil = None
    if tiers:
        _mins = [t.get("amount_min") for t in tiers if isinstance(t.get("amount_min"), (int, float))]
        _maxs = [t.get("amount_max") for t in tiers if isinstance(t.get("amount_max"), (int, float))]
        floor = min(_mins) if _mins else None
        ceil = max(_maxs) if _maxs else None
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
    _cand_dl = _as_iso(candidate.get("submission_deadline"))
    if _cand_dl:
        d_val, d_conf, d_method = _cand_dl, "high", "handler"
        d_window = d_window or "One-off"
    elif (d_val is None or d_conf == "low") and _llm("submission_deadline"):
        d_val, d_conf, d_method = _llm("submission_deadline"), "medium", "llm"
        d_window = d_window or "One-off"
    prov["deadline"] = {"method": d_method, "confidence": d_conf, "source_tier": "T1"}

    # LLM scope (when consulted) is more accurate than regex at telling the CALL's
    # geography from incidental mentions → it REPLACES the regex guesses (handler
    # scope is always kept). e.g. GC India RFP that name-drops "South Africa".
    _llm_geo = {str(g).strip() for g in (_llm("call_geographic_scope") or []) if str(g).strip()}
    if _llm_geo:
        geo = handler_geo | _llm_geo

    funding_status = "Closed" if (llm and llm.get("is_closed")) else "Open"
    overall_conf = "high" if (llm and llm.get("confidence") == "high") else d_conf

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
        "grant_amount": g_amt,
        "currency": g_cur,
        "call_award_floor": floor,
        "call_award_ceiling": ceil,
        "funding_tiers": tiers,
        "deadline": d_val,
        "deadline_confidence": d_conf,
        "funding_window": d_window,
        "funding_status": funding_status,
        # brief kept as-is for now; LLM narrative synthesis (full/eligibility/fit) later
        "brief_description": candidate.get("brief_description"),
        "raw_text": (text or None) and str(text)[:20000],
        "content_hash": hashlib.sha1(blob.encode("utf-8")).hexdigest(),
        "extraction_confidence": overall_conf,
        "field_provenance": prov,
        "solicitation_language": "English",
    }
    return rec, reason


def extract_and_store(candidate: dict[str, Any], policies: dict[str, Any], *,
                      scan_year: int | None = None, use_llm: bool = True,
                      llm_arbiter=None) -> tuple[str | None, str]:
    """Build + upsert into extracted_solicitations. Returns (uid|None, reason)."""
    rec, reason = build_record(candidate, policies, scan_year=scan_year,
                               use_llm=use_llm, llm_arbiter=llm_arbiter)
    if rec is None:
        return None, reason
    return extracted_store.upsert_extracted(rec), reason


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
