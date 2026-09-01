"""RFP scrapers — fetch listing pages / feeds / APIs and return candidates.

Public entry point: `scan_source(source)` dispatches on `method` and returns
a list of candidate dicts. Each candidate has the shape:

    {
      "opportunity_title": str,        # REQUIRED
      "opportunity_link":  str | None, # canonical URL
      "funding_agency":    str | None, # donor / agency
      "brief_description": str | None,
      "date_posted":       date | None,
      "call_submission_deadline": date | None,
      "_source_origin":    str,        # human-readable (where we found it)
    }

The pipeline in `core/scan_pipeline.py` is responsible for:
  * generating UID
  * running find_duplicates
  * inserting into rfp_submissions

Scrapers themselves stay pure — they don't touch the DB.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

import threading

from core import http as _http  # polite shared session: per-host throttle + 429 backoff + TTL cache
from core.extracted_store import make_uid  # stable content key for incremental-skip

log = logging.getLogger(__name__)

# Incremental extraction bookkeeping. `scan_source` runs once per source, each in
# its own worker thread (see scripts/run_scan.py ThreadPool), so a thread-local
# counter lets each source tally how many already-fresh candidates it skipped
# without any cross-thread races. run() reads it via `last_scan_skipped()`
# immediately after the source's scan_source returns.
_scan_state = threading.local()


def _reset_skipped() -> None:
    _scan_state.skipped = 0


def _record_skipped(n: int) -> None:
    _scan_state.skipped = getattr(_scan_state, "skipped", 0) + int(n or 0)


def last_scan_skipped() -> int:
    """How many already-fresh candidates the most recent scan_source() on THIS
    thread skipped (incremental extract-only mode). 0 when not applicable."""
    return int(getattr(_scan_state, "skipped", 0) or 0)

# Network defaults — keep per-request timeout aggressive so the orchestrator
# doesn't hang on slow donor sites.
HTTP_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; RFPIS/1.0; "
    f"+contact: {os.environ.get('SCRAPER_CONTACT_EMAIL', 'contact@example.org')})"
)

# Health-relevant keyword filter applied to candidate titles + descriptions.
# Keeps generic donor listings from flooding the inbox with totally
# off-mission opportunities.
HEALTH_KEYWORDS = [
    "health", "disease", "infection", "epidemic", "pandemic",
    "hiv", "aids", "tuberculosis", "tb", "malaria",
    "vaccine", "immuni", "amr", "antimicrobial",
    "maternal", "newborn", "child health", "nutrition",
    "global health", "primary care", "ncd", "non-communicable",
    "diagnostic", "treatment", "therapeutic", "outbreak",
    "essential medicine", "lmic", "low- and middle",
    "drr", "tropical", "neglected disease",
]

# HTML anchors whose href / text look like opportunity / grant pages.
# Extraction filter — broad on purpose (high recall); the eligibility gate trims
# noise downstream. Recognises every solicitation form by ACRONYM and FULL wording.
_GRANTY_RE = re.compile(
    r"(grant|funding|fund|propos|applic|award|opportunit|challenge|innovation|"
    r"solicit|tender|bid\b|notice|procure|prequalif|"
    # acronyms
    r"rfp|rfa|rfq|rfi|rfei|cfp|cfa|cfn|nofo|nofa|foa|baa|aps|eoi|reoi|ceoi|"
    r"loi|itb|ifb|itt|spn|gpn|"
    # full wordings / phrases
    r"call for|request for|expression of interest|invitation to|invitation for|"
    r"letter of intent|notice of funding|quotation)",
    re.IGNORECASE,
)

# URLs pointing at blog / guidance / news / GRANTEE-PROFILE pages —
# never RFPs. Negative filter: reject any anchor whose path matches.
# `/grantee/<name>/`, `/grantees/`, `/recipients/`, `/awardees/` etc. are
# pages ABOUT past recipients of grants, not calls for new applications.
_BLOG_URL_RE = re.compile(
    r"/(guidance|guide|guides|about|news|blog|policy|policies|"
    r"learn|team|teams|people|contact|help|faq|faqs|story|stories|"
    r"insight|insights|article|articles|press|media|publication|publications|"
    r"event|events|webinar|webinars|video|videos|report|reports|"
    r"grantee|grantees|recipient|recipients|awardee|awardees|"
    r"our-grantees|our-partners|portfolio|portfolios|"
    r"who-we-fund|where-we-work|impact-stories)/",
    re.IGNORECASE,
)

# URLs that are themselves SEARCH or FILTER pages, not the actual grant /
# call detail. Common signals:
#   * `?filter_<field>=…` (Rockefeller, WordPress facet plugins)
#   * `?post_type=…` plus other filters
#   * `?search=…`, `?keyword=…`, `?q=…`
#   * a path segment of `/search/` paired with a query string
#   * `?submit=Submit` (the form's submit value lingering in the URL)
# Each candidate matched here is the result-page URL, not a navigable
# RFP. Reject before enrichment / scoring — they'd waste a fetch and
# enter the DB with no useful detail page to point reviewers at.
_SEARCH_PAGE_URL_RE = re.compile(
    r"(?:"
    r"[?&]filter_\w+="
    r"|[?&]search="
    r"|[?&]keyword="
    r"|[?&]q="
    r"|[?&]submit="
    r"|[?&]post_type="
    r"|/search/?[?&]"
    r"|/(?:catalog|listing|results?)/?\?"
    r")",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Detail-page enrichment — fetch each HTML candidate's URL and try to extract
# a deadline, a brief description, and eligibility text. Best-effort: any
# extraction failure leaves the candidate untouched. Cap per scan to keep
# scan time manageable.
# ---------------------------------------------------------------------------
# Per-source detail-page enrichment caps.
#   ENRICH_MAX_PAGES — how many candidates to enrich per source. Higher =
#     more deadline/description coverage but linearly slower.
#   ENRICH_TIMEOUT  — seconds per detail-page fetch. Lower trades some
#     extraction for speed against slow donor sites.
# Tightened from (25, 5) → (15, 4) on 2026-06-04 after the source list
# grew past 35: full-scan wall time was hitting the 5-min timeout.
ENRICH_MAX_PAGES = 15
ENRICH_TIMEOUT = 4

# Deadline / closing-date label patterns. Captures whatever follows the
# label up to a sentence terminator or newline. We then run the captured
# string through dateutil for robust parsing of free-form date strings.
_DEADLINE_LABEL_RE = re.compile(
    r"(?:"
    # "Extended deadline" wins over plain deadline since donors usually
    # extend rather than shorten — but both patterns match; we resolve
    # winner in _extract_deadline_from_text() (latest match wins).
    r"extended\s+deadline"
    r"|revised\s+deadline"
    r"|new\s+deadline"
    r"|deadline"
    r"|application\s+(?:due|deadline|closing|close)\s+date"
    r"|applications?\s+close[sd]?\s+(?:on\s+|by\s+)?"
    r"|applications?\s+accepted\s+until"
    r"|applications?\s+(?:must\s+be\s+)?submitted\s+(?:by|no\s+later\s+than)"
    r"|accepting\s+submissions?\s+(?:through|until|up\s+to)"
    r"|submissions?\s+(?:through|until|by|no\s+later\s+than)"
    r"|submit\s+(?:by|before|no\s+later\s+than)"
    r"|submission\s+(?:deadline|due\s+date|date)"
    r"|closing\s+date"
    # "Closes: …", "Closed on …", "Close date …", "Closes 10 May / May 10" —
    # but NOT a bare "closed calls" / "closed competitions" (a non-deadline use
    # that used to false-trigger and let its capture window swallow the REAL
    # "Submission deadline: <date>" that followed). Require a date context.
    r"|close[sd]?(?:\s*[:\-–]|\s+(?:on|date|by)\b|(?=\s+\d)|(?=\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)))"
    r"|date\s+closed"
    r"|due\s+(?:date|by|on)"
    r"|apply\s+(?:by|before|no\s+later\s+than|until)"
    r"|open\s+until"
    r"|expected\s+(?:closing|close|due)\s+date"
    r"|estimated\s+application\s+due\s+date"
    r"|response\s+(?:due|deadline)"
    r")"
    # Separator is OPTIONAL — BMGF writes "Date Closed May 21, 2026" with
    # no colon, "Apply by 15 March 2026" with just a space, etc.
    r"\s*[:\-–]?\s+"
    # STOP AT A SENTENCE BREAK. The capture used to be a flat 60-char run that included
    # ".", so it read straight through into the NEXT sentence and swallowed dates that
    # have nothing to do with this deadline:
    #
    #   "Applications close on 1 June 2026. Evaluation runs 1 July to 30 August 2026."
    #        captured -> "1 June 2026. Evaluation runs 1 July 2026 to 30 August 2026."
    #        so the evaluation window outranked the actual deadline.
    #
    # It also swallowed the FOLLOWING LABEL, which silently disabled the extended-deadline
    # behaviour this module documents: in "Deadline: 23 March. Extended deadline: 30
    # March", the first match consumed "Extended deadline", so that label never matched on
    # its own and 30 March was never seen at all.
    #
    # The tempered class refuses any position that begins ". <Capital>", which ends the
    # clause without breaking dates that legitimately contain a period ("Mar. 15, 2026",
    # "15.03.2026" — neither is a period + space + capital).
    r"((?:(?!\.\s+[A-Z])[A-Za-z0-9 ,\-/.]){6,60})",
    re.IGNORECASE,
)

# Year-in-URL/title heuristic. RFP pages typically embed the year in the
# slug or title — "2020-call-for-projects", "year/2024/alliance-tsc-…".
# When the URL clearly says a past year, treat the candidate as expired
# even if no explicit deadline phrase parsed.
_URL_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")

# Eligibility / geography label patterns.
_ELIGIBILITY_LABEL_RE = re.compile(
    r"(?:"
    r"eligibility|eligible\s+(?:countries?|applicants?|entities|geographies?)"
    r"|who\s+(?:can|may)\s+apply"
    r"|geographic(?:al)?\s+(?:focus|scope|eligibility|coverage)"
    r"|target\s+(?:countries?|geographies?|regions?)"
    r"|country\s+focus"
    r")"
    r"\s*[:\-–]?\s*\n?\s*"
    r"([^\n]{20,800})",
    re.IGNORECASE,
)


def _parse_freeform_date(s: str) -> date | None:
    """Try every known date format on a candidate string."""
    if not s:
        return None
    s = s.strip().rstrip(".,;:")
    # Strip common trailing time annotations.
    s = re.sub(r"\s+(?:at|by)\s+\d{1,2}[:.]\d{2}.*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+\d{1,2}[:.]\d{2}\s*(?:am|pm|et|pdt|pst)?.*$", "", s, flags=re.IGNORECASE)
    fmts = [
        "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
        "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s[:len(fmt)+10].strip(", ."), fmt).date()
        except (ValueError, TypeError):
            continue
    # dateutil fallback — but ONLY when the string actually contains a real
    # date anchor (a 4-digit 20xx year OR a month name). Without this guard,
    # fuzzy parsing turns fragments like "grant term of up to 36 months" into
    # 2036-<today>, which then wins max() over the real deadline. Require an
    # anchor, then let fuzzy skip the surrounding words.
    has_year = re.search(r"(?<!\d)20\d{2}(?!\d)", s)
    has_month = re.search(
        r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", s, re.IGNORECASE)
    if not (has_year or has_month):
        return None
    try:
        from dateutil import parser as _du  # type: ignore
        return _du.parse(s, fuzzy=True, dayfirst=False).date()
    except Exception:
        return None


# "From <date> to <date>" / "Open from <date> to <date>" patterns.
# Donors like Pierre Fabre publish application windows without a
# "Deadline:" label, e.g. "APPLICATIONS — FROM OCT 9TH TO NOV 7TH 2025".
# We want the END date as the deadline.
_DATE_RANGE_RE = re.compile(
    r"(?:from|open(?:s|ing)?(?:\s+from)?|between|applications?)"
    r"[\s:\-–—]*"
    r"(?:[A-Za-z0-9.,/\- ]{4,40}?)"   # start date (non-greedy)
    r"\s+(?:to|until|through|and|–|—|until\s+the)\s+"
    r"([A-Za-z0-9.,/\- ]{4,40})",      # end date (captured)
    re.IGNORECASE,
)

# Trailing-label window — the trigger word comes AFTER the range, e.g. the
# ODESS / Fondation Pierre Fabre calendar "9 october to 7 november 2025:
# applications open". Capture the END (close) date of the window.
_DATE_RANGE_TRAILING_RE = re.compile(
    r"(?:[A-Za-z0-9.,/\- ]{4,40}?)"               # start date
    r"\s+(?:to|until|through|–|—)\s+"
    r"([A-Za-z0-9.,/\- ]{4,40}?)"                 # end date (captured)
    r"\s*[:\-–—]?\s*applications?\b",
    re.IGNORECASE,
)


# A single date token anywhere inside a captured deadline blob. We scan the
# blob for ALL of these rather than parsing only its prefix, because a label
# capture is greedy and can hold several dates (e.g. a year-less "16 December"
# next to an explicit "Date Closed Dec 16, 2025").
_DATE_IN_TEXT_RE = re.compile(
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+20\d{2})?"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?(?:,?\s+20\d{2})?"
    r"|20\d{2}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}/\d{1,2}/20\d{2}",
    re.IGNORECASE,
)


def _extract_deadline_from_text(text: str) -> date | None:
    """The SUBMISSION deadline in `text`, or None.

    Two kinds of date show up, and they must not compete on equal terms:

      LABELLED   the text names it a deadline ("Deadline:", "applications close",
                 "submission deadline", "closing date"). Authoritative. The LATEST wins,
                 which is deliberate extended-deadline behaviour: donors say
                 "Deadline: Mar 23" and later "Extended deadline: Mar 30", and Mar 30 is
                 the real one.

      UNLABELLED a date window with no deadline wording ("9 October to 7 November 2025:
                 applications open"). The range patterns capture the window's END, which
                 is the date an applicant must meet.

    A LABELLED date always beats an unlabelled one. Among UNLABELLED windows the EARLIEST
    end wins, not the latest — because a call that publishes a calendar lists the whole
    selection process:

        9 October to 7 November 2025 : applications open      <- the deadline
        14 November 2025             : confirmation
        18 December 2025 to 23 January 2026 : expert evaluation

    Taking the latest picked 23 January — an evaluation milestone, ~11 weeks after
    submissions closed. That is the dangerous direction: it makes a closed call look open,
    so an expired call passes the deadline gate. The earliest window end is the submission
    close. (Extended deadlines are unaffected: those are LABELLED, and still take max.)
    """
    if not text:
        return None
    # (explicit year in the token, came from a deadline LABEL) -> dates
    buckets: dict[tuple[bool, bool], list[date]] = {
        (True, True): [], (True, False): [], (False, True): [], (False, False): []}

    def _add(raw: str | None, labelled: bool) -> None:
        # Scan the captured blob for every date token, not just its prefix — the prefix
        # may be unparseable noise ("Tuesday, 16 December 1700HRS") while the real,
        # year-bearing date sits later in the same capture.
        for tok in _DATE_IN_TEXT_RE.findall(raw or ""):
            d = _parse_freeform_date(tok)
            if not d:
                continue
            has_year = bool(re.search(r"(?<!\d)20\d{2}(?!\d)", tok))
            buckets[(has_year, labelled)].append(d)

    for m in _DEADLINE_LABEL_RE.finditer(text):
        _add(m.group(1), True)
    # Unlabelled date ranges — "APPLICATIONS: FROM OCT 9TH TO NOV 7TH 2025"
    for m in _DATE_RANGE_RE.finditer(text):
        _add(m.group(1), False)
    # Trailing-label windows — "9 october to 7 november 2025: applications open"
    for m in _DATE_RANGE_TRAILING_RE.finditer(text):
        _add(m.group(1), False)

    # Sanity window: drop absurd far-future dates (a stray year in a strategy PDF, or a
    # "36 months" -> 2036 misparse). Real RFP deadlines are within a couple of years.
    cutoff = date.today().year + 2

    def _ok(ds: list[date]) -> list[date]:
        return [d for d in ds if d.year <= cutoff]

    # Prefer dates that carried an EXPLICIT year. A year-less phrase like "Deadline: 16
    # December" gets defaulted to the current year by the parser, which can turn a PAST
    # deadline (the page's "Date Closed Dec 16, 2025") into a spurious FUTURE one and leak
    # an expired call through. Only fall back to year-less dates when no explicit-year
    # date is present at all.
    for has_year in (True, False):
        labelled = _ok(buckets[(has_year, True)])
        if labelled:
            return max(labelled)                 # extended deadline wins
        windows = _ok(buckets[(has_year, False)])
        if windows:
            return min(windows)                  # earliest window end = submission close
    return None


def _detect_url_year(url: str, title: str = "") -> int | None:
    """Find the latest year mentioned in the URL or title. Used as a
    fallback past-deadline signal when no explicit deadline phrase
    parsed (donor pages frequently have the year in the slug)."""
    blob = f"{url or ''} {title or ''}"
    years = [int(y) for y in _URL_YEAR_RE.findall(blob)]
    return max(years) if years else None


def _extract_eligibility_from_text(text: str) -> str | None:
    m = _ELIGIBILITY_LABEL_RE.search(text or "")
    if not m:
        return None
    return _clean(m.group(1))[:500]


# Project-duration mining. Calls advertise duration inline ("12-18 month research
# program", "up to 24 months", "36-month project", "1-2 years"), often with a
# NON-BREAKING hyphen (U+2011) or en/em dash that a naïve "12-18" regex misses —
# the reason the AI/GHW call's "12‑18 month" slipped through. Every dash variant is
# in the class below; year units convert to months; weeks/days are ignored (duration
# is counted in months).
_DUR_DASH = "‐‑‒–—―-"          # incl. non-breaking hyphen (U+2011); plain "-" LAST
_DUR_UNIT = r"(?:months?|mos?|years?|yrs?)"
_DUR_RANGE_RE = re.compile(
    rf"(\d{{1,3}})\s*(?:[{_DUR_DASH}]|to)\s*(\d{{1,3}})\s*({_DUR_UNIT})\b", re.I)
_DUR_SINGLE_RE = re.compile(
    rf"(?:up\s+to\s+)?(\d{{1,3}})[\s{_DUR_DASH}]*({_DUR_UNIT})\b", re.I)


def _dur_to_months(n: int, unit: str) -> int:
    return n * (12 if unit.lower().startswith(("year", "yr")) else 1)


def duration_months_from_text(text: str | None, *, mode: str = "max") -> int | None:
    """Longest project duration in MONTHS advertised anywhere in a call's text.

    Handles ranges ("12-18 months"), "up to N months", single values, and year units
    — across every hyphen/dash variant incl. the non-breaking hyphen. For a range,
    ``mode='max'`` (default) takes the ceiling (what the call permits — aligned with
    PREFER-6 "longer preferred"); ``mode='avg'`` takes the midpoint. When a call lists
    several tracks of different lengths, the LONGEST advertised engagement wins.
    Weeks/days are ignored. Returns an int or None."""
    if not text:
        return None
    t = str(text)
    found: list[int] = []
    range_spans: list[tuple[int, int]] = []
    for m in _DUR_RANGE_RE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = min(a, b), max(a, b)
        pick = hi if mode == "max" else round((lo + hi) / 2)
        found.append(_dur_to_months(pick, m.group(3)))
        range_spans.append(m.span())
    for m in _DUR_SINGLE_RE.finditer(t):     # single values not already inside a range
        if any(s <= m.start() < e for s, e in range_spans):
            continue
        found.append(_dur_to_months(int(m.group(1)), m.group(2)))
    return max(found) if found else None


# FIRST-POSTED date meta keys (exclude modified/updated so a CMS re-touch can't
# make an old call look recent — recency is judged by when it was POSTED).
_PUB_META_KEYS = {
    "article:published_time", "datepublished", "dc.date", "dcterms.date",
    "dcterms.created", "dcterms.issued", "date", "pubdate", "publishdate",
    "publication_date", "sailthru.date", "parsely-pub-date",
}


def _extract_page_date(soup: "BeautifulSoup | None") -> date | None:
    """Earliest first-posted date from <meta> publish keys + JSON-LD
    datePublished / dateCreated. Used as a recency signal for the deadline gate
    when no deadline parses (a non-rolling call posted long ago is expired —
    e.g. the Fondation Pierre Fabre 'Seven Winners' page, published 2017)."""
    if soup is None:
        return None
    import json as _json
    found: list[date] = []
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name")
               or tag.get("itemprop") or "").strip().lower()
        if key in _PUB_META_KEYS:
            raw = tag.get("content") or ""
            d = _parse_iso_date(raw) or _parse_freeform_date(raw)
            if d:
                found.append(d)
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = _json.loads(sc.string or sc.get_text() or "")
        except Exception:
            continue
        for obj in (data if isinstance(data, list) else [data]):
            if not isinstance(obj, dict):
                continue
            for k in ("datePublished", "dateCreated"):
                v = obj.get(k)
                if v:
                    d = _parse_iso_date(str(v)) or _parse_freeform_date(str(v))
                    if d:
                        found.append(d)
    return min(found) if found else None


def _extract_description_from_soup(soup: BeautifulSoup) -> str | None:
    """Prefer og:description / meta description, fall back to first long <p>."""
    for sel in (
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "description"}),
        ("meta", {"name": "twitter:description"}),
    ):
        el = soup.find(*sel)
        if el and el.get("content"):
            txt = _clean(el["content"])
            if len(txt) > 40:
                return txt[:800]
    # Fall back to the first paragraph with reasonable length.
    for p in soup.find_all("p"):
        txt = _clean(p.get_text(" ", strip=True))
        if 60 <= len(txt) <= 1200:
            return txt[:800]
    return None


# Max PDF size we'll download for enrichment (bytes). Full RFP packages are
# frequently 15-25 MB (e.g. the 21 MB Grand Challenges "Nexa" RFP), and we now
# deep-read the WHOLE RFP to exhaust the extraction data, so the cap is 30 MB.
ENRICH_PDF_MAX_BYTES = 30 * 1024 * 1024
# Below this many chars, a detail page is a "teaser" — a landing whose real RFP content
# (eligibility, scope, information requested, evaluation) lives in a linked application/RFI
# PDF. Trigger the FULL-PDF harvest so the LLM reads the whole document (MMV market-intel
# RFI: a ~5.5k-char table-of-contents landing → a 20-page instructions PDF). Set generously
# — the harvest only fires when the page ALSO links a guide-relevant document (see
# _find_application_pdf(require_keyword=True)), so a self-contained content page that
# happens to be short but links no RFP PDF is untouched.
_THIN_PAGE_CHARS = int(os.environ.get("RFPIS_THIN_PAGE_CHARS", "15000") or 15000)
# Pages to parse. 3 → 8 (2026-06-04, Pierre Fabre deadlines on p4-5) → 30
# (2026-07-02): the FULL RFP is the extraction target — eligibility, funding and
# duration detail routinely sit deep in a 40-page package. ~50ms/page of pypdf
# parsing is cheap vs the download.
ENRICH_PDF_MAX_PAGES = 30
# PDF downloads are much larger than an HTML detail page, so they get their own
# (longer) timeout instead of the tight per-page ENRICH_TIMEOUT.
ENRICH_PDF_TIMEOUT = 20


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from the first N pages of a PDF. Returns empty string
    on any failure (encrypted PDF, scanned image, parse error, etc.)."""
    try:
        from pypdf import PdfReader
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf_bytes))
        chunks: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= ENRICH_PDF_MAX_PAGES:
                break
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        return _clean(" ".join(chunks))
    except Exception as exc:
        log.debug("pdf parse failed: %s", exc)
        return ""


# Anchor text words that signal "this PDF is the application guide" —
# used to find the most relevant PDF on a landing page like Pierre Fabre.
_GUIDE_PDF_KEYWORDS = (
    "guide", "application", "applicant", "instruction",
    "call", "rfp", "rfa", "rfi", "information", "tender", "proposal", "submission",
    "terms", "conditions", "tor", "terms of reference",
    "download", "details",
)


# CMS "download document" links that serve a PDF without a .pdf extension (UNGM's
# DownloadDocument, generic getDocument/download endpoints). Used so the RFP PDF behind
# such a link is still followed for full-text / budget / deadline extraction.
_DOC_DOWNLOAD_RE = re.compile(
    r"/downloaddocument\b|[?&]documentid=|/getdocument\b|/download/[^?]*document", re.I)


def _find_application_pdf(soup: "BeautifulSoup | None", base_url: str,
                          require_keyword: bool = False) -> str | None:
    """Find the most likely 'application guide' PDF linked from an HTML
    page. Pierre Fabre (and many foundations) publish a landing page
    with the deadline buried inside a downloadable PDF. We follow that
    PDF for deadline extraction.

    Strategy: prefer PDFs whose anchor text mentions guide / application
    / call / etc. If none match, fall back to the first PDF on the page —
    UNLESS `require_keyword` (used by the full-body harvest, which must not
    pull a random logo/annual-report PDF), in which case return None when no
    anchor is guide-relevant.
    """
    if soup is None:
        return None
    relevant: list[tuple[int, str]] = []
    fallback: str | None = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        _low = href.lower().split("#", 1)[0]
        # Accept a real .pdf link OR a CMS document-DOWNLOAD link that serves a PDF without
        # a .pdf extension — notably UNGM's `/Public/Notice/DownloadDocument?...&documentId=`
        # (the full RFP with the budget lives there, not on the landing page). fetch_pdf_text
        # validates the bytes, so a non-PDF download just yields no text and is skipped.
        _is_pdf = _low.split("?", 1)[0].endswith(".pdf")
        _is_doc_dl = bool(_DOC_DOWNLOAD_RE.search(_low))
        if not (_is_pdf or _is_doc_dl):
            continue
        full = urljoin(base_url, href)
        text = (a.get_text(" ", strip=True) or "").lower()
        # Score: how many guide-relevant keywords appear in anchor text.
        score = sum(1 for kw in _GUIDE_PDF_KEYWORDS if kw in text)
        if score > 0:
            relevant.append((score, full))
        elif fallback is None:
            fallback = full
    if relevant:
        relevant.sort(reverse=True)  # highest score first
        return relevant[0][1]
    return None if require_keyword else fallback


def _try_pdf_guide_deadline(pdf_url: str) -> tuple[date | None, str | None]:
    """Fetch a linked guide PDF and try to extract (deadline, brief_text).
    Returns (None, None) on any failure. Heavily bounded — same caps as
    the main PDF enrichment path."""
    try:
        r = _http.get(
            pdf_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/pdf, */*"},
            timeout=ENRICH_PDF_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        r.raise_for_status()
        cl = int(r.headers.get("Content-Length") or 0)
        if cl and cl > ENRICH_PDF_MAX_BYTES:
            return None, None
        pdf_bytes = r.content[:ENRICH_PDF_MAX_BYTES]
    except Exception as exc:
        log.debug("pdf-guide fetch failed for %s: %s", pdf_url, exc)
        return None, None
    text = _extract_pdf_text(pdf_bytes)
    if not text:
        return None, None
    return _extract_deadline_from_text(text), text[:600] if text else None


def fetch_pdf_text(pdf_url: str) -> str:
    """Download a PDF and return its FULL extracted text (up to the page cap), for
    deep-reading a call whose real detail lives in an attached RFP package. Bounded by
    the same size/timeout/page caps; returns '' on any failure. Never raises."""
    try:
        r = _http.get(
            pdf_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/pdf, */*"},
            timeout=ENRICH_PDF_TIMEOUT, allow_redirects=True, stream=True,
        )
        r.raise_for_status()
        cl = int(r.headers.get("Content-Length") or 0)
        if cl and cl > ENRICH_PDF_MAX_BYTES:
            return ""
        return _extract_pdf_text(r.content[:ENRICH_PDF_MAX_BYTES])
    except Exception as exc:
        log.debug("fetch_pdf_text failed for %s: %s", pdf_url, exc)
        return ""


# Companion call / calendar pages. Some donors announce a call on one page (no
# dates) and host the application calendar elsewhere — e.g. Fondation Pierre
# Fabre's call page links to odess.io, where "9 october to 7 november 2025:
# applications open" gives the real (here, past) deadline. When the main page
# yields no deadline, follow ONE such companion link and read its deadline.
_COMPANION_HREF_RE = re.compile(
    r"(odess|call[-_]for[-_]project|appel[-_]a[-_]projet|candidater"
    r"|application[-_]form|/apply\b|/calendar|/timeline)",
    re.IGNORECASE,
)


# Strong companion signals — dedicated application / calendar pages. Ranked
# ABOVE the generic "call-for-project" match, which frequently hits a site's
# OWN nav/listing link (the Fondation Pierre Fabre bug: its call page links to
# odess.io for the calendar AND has an internal "current-initiatives/
# call-for-projects/" nav link that matched first and carried no date).
_COMPANION_STRONG_RE = re.compile(
    r"(odess|/calendar|/timeline|candidater|appel[-_]a[-_]projet"
    r"|application[-_]form|/apply\b)", re.I)
# Social / share links often embed the page URL (so they match the companion
# pattern) but are never a calendar page — never follow them.
_SOCIAL_SHARE_RE = re.compile(
    r"(linkedin\.com|facebook\.com|twitter\.com|//x\.com|sharer|sharearticle"
    r"|/share[?/]|wa\.me|whatsapp|t\.me|pinterest|reddit\.com|mailto:)", re.I)


def _find_companion_call_links(soup: "BeautifulSoup | None", base_url: str) -> list[str]:
    """Companion links worth following for a deadline, BEST FIRST. Prefers an
    EXTERNAL dedicated calendar/application page (e.g. a call that links out to
    odess.io) over a same-site 'call-for-projects' nav link, which usually
    carries no dates."""
    if soup is None:
        return []
    base = base_url.rstrip("/")
    base_host = urlsplit(base_url).netloc.lower()
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        nf = full.rstrip("/")
        if nf == base or nf in seen:
            continue
        low = full.lower()
        if _SOCIAL_SHARE_RE.search(low):
            continue
        if not _COMPANION_HREF_RE.search(low):
            continue
        seen.add(nf)
        external = bool(urlsplit(full).netloc.lower()) and urlsplit(full).netloc.lower() != base_host
        strong = bool(_COMPANION_STRONG_RE.search(low))
        scored.append(((2 if external else 0) + (1 if strong else 0), full))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [u for _, u in scored]


def _find_companion_call_link(soup: "BeautifulSoup | None", base_url: str) -> str | None:
    """Best single companion link (back-compat shim)."""
    links = _find_companion_call_links(soup, base_url)
    return links[0] if links else None


def _follow_companion_for_deadline(soup: "BeautifulSoup | None", base_url: str) -> date | None:
    """One-hop follow of a companion call/calendar page to read its deadline.
    Tries the best-ranked candidates in turn until one yields a date, so an
    early-but-dateless nav link can't shadow the real external calendar."""
    for link in _find_companion_call_links(soup, base_url)[:3]:
        try:
            r = _http.get(
                link, headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                timeout=ENRICH_TIMEOUT,
            )
            r.raise_for_status()
            ctext = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        except Exception as exc:
            log.debug("companion fetch failed for %s: %s", link, exc)
            continue
        dl = _extract_deadline_from_text(ctext)
        if dl:
            return dl
    return None


# DevelopmentAid detail-page title pattern.
# Their <title> element follows a STRICT pattern that packs four
# high-value fields we'd otherwise miss (donor / countries / sectors /
# brief). Variations handled:
#   * Status prefix: "Open" / "Awarded" / "Closed" — we filter past-tense.
#   * "by DONOR" is OPTIONAL (some grants don't name a funder there).
#   * "in X sector" (singular) and "in X sectors" (plural) both occur.
#   * The <title> is the FULL string — og:title is truncated at ~200 chars
#     and unusable for the donor / sectors fields at the tail.
# Deadline / project value DO NOT appear in plain text on these pages —
# they're paywalled behind a DevelopmentAid Pro membership. Best we can
# do without auth.
_DEVELOPMENTAID_TITLE_RE = re.compile(
    r"^(?P<status>Open|Awarded|Closed)\s+grant\s*[—-]\s*"
    r"(?P<title>.+?)\s*[—-]\s*"
    r"for\s+(?P<countries>.+?)"
    r"(?:\s+by\s+(?P<donor>.+?))?"
    r"\s+in\s+(?P<sectors>.+?)\s+sectors?\s*[—-]\s*DevelopmentAid$",
    re.IGNORECASE,
)


def _enrich_developmentaid(cand: dict[str, Any]) -> dict[str, Any] | None:
    """Fetch a DevelopmentAid detail page and merge in fields from the
    <title>-tag pattern.

    Returns:
      * the mutated candidate dict on success
      * None if the grant is in past-tense status ("Awarded" / "Closed")
        — these shouldn't enter the RFP pipeline at all
      * the candidate unchanged if the URL doesn't match or fetch fails
    """
    link = cand.get("opportunity_link") or ""
    if "developmentaid.org/grants/view/" not in link:
        return cand
    try:
        r = _http.get(link, headers={"User-Agent": USER_AGENT},
                         timeout=HTTP_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
    except Exception as exc:
        log.debug("DevelopmentAid fetch failed for %s: %s", link, exc)
        return cand
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return cand

    # Robust status gate FIRST. DevelopmentAid renders "Status: Closed" (or
    # Awarded / Expired) in the detail body. The <title> pattern below is
    # brittle and frequently doesn't match (the funder then shows as the
    # aggregator) — which used to let CLOSED grants slip into the pipeline
    # (e.g. the Velux Stiftung "Daylight Research Grant", closed + CHF). Read
    # the status straight from the body so past-tense grants are dropped
    # regardless of the title format.
    body_text = soup.get_text(" ", strip=True)
    _st = re.search(
        r"\bstatus\b\s*[:\-]?\s*"
        r"(closed|awarded|expired|cancell?ed|completed|finished)",
        body_text, re.I)
    if _st:
        log.info("DevelopmentAid skip (status=%s): %s", _st.group(1).lower(), link)
        return None

    full_title = soup.title.get_text() if soup.title else ""
    m = _DEVELOPMENTAID_TITLE_RE.match(full_title)
    if not m:
        log.debug("DevelopmentAid title pattern did not match: %s",
                  full_title[:120])
        return cand

    # Past-tense → drop entirely. These show up on the "all grants"
    # listing but are not active opportunities.
    status = (m.group("status") or "").lower()
    if status in ("awarded", "closed"):
        log.info("DevelopmentAid skip past-tense (%s): %s",
                 status, m.group("title")[:60])
        return None

    # Merge into the candidate, preferring extracted values over any
    # generic placeholders from the listing scrape.
    cand["opportunity_title"] = (m.group("title") or "").strip() or cand.get("opportunity_title")
    if m.group("donor"):
        cand["funding_agency"] = m.group("donor").strip()
    countries = [c.strip() for c in (m.group("countries") or "").split(",") if c.strip()]
    if countries:
        cand["call_geographic_scope"] = countries
    sectors = [s.strip() for s in (m.group("sectors") or "").split(",") if s.strip()]
    if sectors:
        cand["call_domain_areas"] = sectors
    # Brief description from og:description (full body text isn't
    # uniquely structured; og:description is the curated summary).
    og_desc_tag = soup.find("meta", attrs={"property": "og:description"})
    og_desc = (og_desc_tag.get("content") if og_desc_tag else "") or ""
    if og_desc and not cand.get("brief_description"):
        cand["brief_description"] = og_desc[:1800]
    return cand


# Award-amount extraction. Requires an explicit currency marker so bare numbers
# (years, counts, phone parts) never match. Used to fill estimated_value for HTML
# donor pages (e.g. Stanford seed funding) where the amount sits in the body.
_NUM = r"(?P<num>\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_MAGS = r"(?P<mag>billion|bn|million|mn|m|thousand|k)?"
# Currency BEFORE the number ($50,000 / USD 1.2 million / €50k).
_AMOUNT_RE = re.compile(
    r"(?P<cur>US\$|USD|\$|€|EUR|£|GBP|CHF|CAD|AUD)\s?" + _NUM + r"\s?" + _MAGS + r"\b",
    re.I)
# Currency AFTER the number (5 million USD / 50,000 euros / 1.2m dollars).
_AMOUNT_RE2 = re.compile(
    _NUM + r"\s?" + _MAGS +
    r"\s?(?P<cur>US dollars?|dollars?|USD|euros?|EUR|pounds?(?: sterling)?|GBP|CHF|CAD|AUD)\b",
    re.I)
_AMOUNT_MAG = {"billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6, "m": 1e6,
               "thousand": 1e3, "k": 1e3}
_AMOUNT_CUR = {"us$": "USD", "usd": "USD", "$": "USD", "us dollars": "USD",
               "us dollar": "USD", "dollars": "USD", "dollar": "USD",
               "€": "EUR", "eur": "EUR", "euros": "EUR", "euro": "EUR",
               "£": "GBP", "gbp": "GBP", "pounds": "GBP", "pound": "GBP",
               "pounds sterling": "GBP", "pound sterling": "GBP",
               "chf": "CHF", "cad": "CAD", "aud": "AUD"}
# Award-context words near a figure that mark it as the grant size (not some
# unrelated dollar figure on the page).
_AMOUNT_CTX_RE = re.compile(
    r"(up to|award|grant|funding|value|budget|maximum|max\b|each|per "
    r"(?:grant|award|project|year)|total|prize|stipend|amount)", re.I)

# A call page routinely states BOTH figures, and they differ by an order of magnitude:
#   "The total indicative budget for the topic is EUR 18 000 000"          <- envelope
#   "an EU contribution of around EUR 2.25 million per project"            <- the award
# Taking the largest award-context figure always picked the envelope. These two patterns
# tell them apart so the per-award figure wins when the page offers one.
_PER_AWARD_CTX_RE = re.compile(
    r"per\s+(?:grant|award|project|proposal|beneficiary|consortium|applicant|"
    r"successful\s+\w+)"
    r"|(?:each|any one|a single)\s+(?:grant|award|project|proposal)"
    r"|contribution\s+(?:of\s+around\s+)?[^.]{0,40}?per\s+project", re.I)
# The page saying, in one form or another, that the pot is shared out. Only used to
# decide whether an envelope figure may stand in for an award size when the page offers
# nothing else.
_MULTI_AWARD_RE = re.compile(
    r"(?:\d+|two|three|four|five|six|seven|eight|nine|ten|several|multiple|a\s+number\s+of)"
    r"\s+(?:projects|grants|awards|proposals|consortia)"
    r"\s*(?:are\s+|will\s+be\s+|to\s+be\s+|expected\s+)?"
    r"(?:funded|selected|supported|awarded|expected)?"
    r"|number\s+of\s+(?:projects|grants|awards)\s+(?:expected|to\s+be)"
    r"|(?:across|among|between)\s+\S+\s+(?:projects|grants|awards)", re.I)
_ENVELOPE_CTX_RE = re.compile(
    r"(?:total|overall|indicative|aggregate|combined|call|programme|program)\s+"
    r"(?:indicative\s+)?(?:budget|funding|envelope)"
    r"|budget\s+for\s+(?:the\s+)?(?:topic|call|programme|program)"
    r"|total\s+(?:funding\s+)?available"
    r"|(?:across|among|between)\s+\S+\s+(?:projects|grants|awards)", re.I)


def _one_amount(m: "re.Match") -> tuple[float | None, str | None]:
    try:
        num = float(m.group("num").replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None, None
    num *= _AMOUNT_MAG.get((m.group("mag") or "").lower(), 1.0)
    cur = _AMOUNT_CUR.get((m.group("cur") or "").lower())
    return num, cur


def _extract_amount(title: str, text: str) -> tuple[float | None, str | None]:
    """Best-effort grant size. Title first (high precision, e.g. '$5,000 Propel
    Grant'); else the LARGEST body figure sitting next to an award-context word.
    Requires a currency marker, and floors un-magnitude'd figures at 1000 so a
    '$50 fee' style number can't masquerade as the award."""
    def _matches(s):
        return list(_AMOUNT_RE.finditer(s or "")) + list(_AMOUNT_RE2.finditer(s or ""))

    for m in _matches(title):
        v, c = _one_amount(m)
        if v and (m.group("mag") or v >= 1000):
            return v, c
    # Largest-wins is right WITHIN a kind — "$50,000 to $100,000" should keep the
    # ceiling — but it is wrong ACROSS kinds, where the programme envelope is always the
    # bigger number and never the award. So collect the two kinds separately and let a
    # per-award figure beat an envelope however much smaller it is.
    per_v, per_c = None, None
    any_v, any_c = None, None
    env_v, env_c = None, None
    for m in _matches(text):
        ctx = text[max(0, m.start() - 40):m.end() + 25]   # award word either side
        if not _AMOUNT_CTX_RE.search(ctx):
            continue
        v, c = _one_amount(m)
        if not (v and (m.group("mag") or v >= 1000)):
            continue
        if _PER_AWARD_CTX_RE.search(ctx):
            if per_v is None or v > per_v:
                per_v, per_c = v, c
        elif _ENVELOPE_CTX_RE.search(ctx):
            if env_v is None or v > env_v:
                env_v, env_c = v, c
        elif any_v is None or v > any_v:
            # Neither marked per-award nor marked as a whole-programme pot: the ordinary
            # single-award page, which behaves exactly as it did before.
            any_v, any_c = v, c
    if per_v is not None:
        return per_v, per_c
    if any_v is not None:
        return any_v, any_c
    # Envelope only. On a single-award call "total funding available" IS the award, and
    # refusing to read it would throw away a figure we used to get right - so it still
    # counts, unless the page says in as many words that the pot is shared out.
    return (None, None) if (env_v and _MULTI_AWARD_RE.search(text or "")) else (env_v, env_c)


def _enrich_candidate(cand: dict[str, Any]) -> dict[str, Any]:
    """Fetch the candidate's detail page and fill in missing fields.

    Handles both HTML and PDF detail pages — many donor sites
    (e.g. WHO AHPSR) link to PDF call documents that hold the deadline
    and description in the body text, not on the listing page.

    Mutates and returns `cand`. Failures (timeout, 4xx, parse errors) are
    silently swallowed — the candidate stays in its original state.
    """
    link = cand.get("opportunity_link")
    if not link:
        return cand

    # DevelopmentAid runs a paywalled grants aggregator. Their detail
    # pages don't expose deadline / project value in plain text, but the
    # <title> tag packs donor / countries / sectors / brief reliably.
    # Run the bespoke enricher FIRST — it can either (a) merge those
    # four fields into the candidate, (b) leave it alone if the title
    # pattern doesn't match, or (c) return None to signal a past-tense
    # grant that should be rejected entirely.
    if "developmentaid.org/grants/view/" in link:
        result = _enrich_developmentaid(cand)
        if result is None:
            cand["_past_tense_grant"] = True  # signal to ingest
            return cand
        cand = result
        # Don't return — let the generic fetch run too, in case the
        # generic deadline regex picks something up.

    # Don't waste a fetch on search/filter pages — these won't yield a
    # deadline or eligibility section because they're query results, not
    # grant detail. Marker on the candidate so downstream can reject.
    if _SEARCH_PAGE_URL_RE.search(link):
        cand["_is_search_page"] = True
        return cand

    # Short-circuit on past-year URLs (e.g. /year/2022/foo.pdf) — no
    # point downloading something we'll reject. Stamp the deadline so
    # downstream eligibility cleanly logs WHY it was rejected.
    url_yr = _detect_url_year(link, cand.get("opportunity_title") or "")
    if url_yr and url_yr < date.today().year and not cand.get("call_submission_deadline"):
        cand["call_submission_deadline"] = date(url_yr, 12, 31)
        cand["_deadline_from_url_year"] = True
        return cand

    # Route PDFs to the dedicated parser.
    is_pdf = link.lower().split("?", 1)[0].endswith(".pdf")

    try:
        r = _http.get(
            link,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf, text/html, */*" if is_pdf else "text/html",
            },
            timeout=ENRICH_TIMEOUT,
            allow_redirects=True,
            stream=is_pdf,  # stream PDFs so we can size-cap before reading
        )
        r.raise_for_status()
    except requests.HTTPError as exc:
        # Detail page returned 4xx/5xx — a dead/error link. Flag it so the
        # eligibility gate rejects it instead of keeping the bare listing title.
        sc = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(sc, int) and sc >= 400:
            cand["_error_page"] = True
        log.debug("enrich HTTP %s for %s: %s", sc, link, exc)
        return cand
    except Exception as exc:
        log.debug("enrich fetch failed for %s: %s", link, exc)
        return cand

    if is_pdf or r.headers.get("Content-Type", "").lower().startswith("application/pdf"):
        try:
            # Size-cap to keep memory + CPU bounded.
            cl = int(r.headers.get("Content-Length") or 0)
            if cl and cl > ENRICH_PDF_MAX_BYTES:
                log.debug("pdf too large (%d bytes): %s", cl, link)
                return cand
            pdf_bytes = r.content[:ENRICH_PDF_MAX_BYTES]
        except Exception:
            return cand
        text = _extract_pdf_text(pdf_bytes)
        if not text:
            return cand
        # PDF path: no soup, just use the extracted text for the same
        # downstream extractors.
        soup = None
    else:
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(" ", strip=True)
        except Exception:
            return cand
        # Publication date — recency signal for the deadline gate when no
        # deadline parses (a non-rolling call posted long ago is expired).
        if not cand.get("date_posted"):
            _pd = _extract_page_date(soup)
            if _pd:
                cand["date_posted"] = _pd

    # Keep the FULL detail-page body so build_record's regex AND LLM see it — not just
    # the local one-shot _extract_amount below. Without this, an amount / tier table that
    # lives only on the detail page (and past the 1800-char brief_description cap) never
    # reached the LLM judge or the synthesis, so ranged/tiered awards read as blank.
    if text and not cand.get("_page_text"):
        cand["_page_text"] = _clean(text)[:20000] or None

    # TEASER detail page + a linked application/RFI DOCUMENT PDF → harvest the FULL PDF
    # body into _page_text (and the local `text` the deadline/eligibility/amount extractors
    # read below), so the whole document reaches regex AND the LLM. Many donors publish a
    # short landing page (a table of contents + a "Document pdf" link) whose real RFP
    # content — scope, eligibility, information requested, evaluation — lives only in the
    # linked instructions/RFI PDF (MMV market-intel RFI). require_keyword=True so this
    # follows a genuine RFP-document link, never a stray logo/annual-report PDF; the length
    # ceiling skips already-substantial pages that are likely self-contained. One bounded
    # PDF fetch, only for pages that link a guide-relevant document.
    if soup is not None and len(_clean(text or "")) < _THIN_PAGE_CHARS:
        _detail_pdf = _find_application_pdf(soup, link, require_keyword=True)
        if _detail_pdf:
            _pdf_full = fetch_pdf_text(_detail_pdf)
            if _pdf_full and len(_pdf_full) > 400:
                text = _clean(((text or "") + "\n\n" + _pdf_full))
                cand["_page_text"] = text[:20000] or None
                cand["_detail_pdf"] = _detail_pdf

    # Deadline detection — four sources, in priority order:
    #   1. Explicit deadline phrase in body text (most reliable).
    #   2. PDF guide linked from the HTML page (Pierre Fabre case —
    #      page shows publish date only; real deadline is in the
    #      downloadable Guide-*.pdf).
    #   3. Latest year mentioned in URL / title.
    if not cand.get("call_submission_deadline"):
        d = _extract_deadline_from_text(text)
        if d:
            cand["call_submission_deadline"] = d
        elif soup is not None:
            # Follow the most-likely application-guide PDF on the page
            # and try to extract the deadline from THERE.
            pdf_url = _find_application_pdf(soup, link)
            if pdf_url:
                pdf_deadline, pdf_brief = _try_pdf_guide_deadline(pdf_url)
                if pdf_deadline:
                    cand["call_submission_deadline"] = pdf_deadline
                    cand["_deadline_from_guide_pdf"] = pdf_url
                if pdf_brief and not cand.get("brief_description"):
                    cand["brief_description"] = pdf_brief
            # Still no deadline? Follow a companion call / calendar page (e.g.
            # Fondation Pierre Fabre -> odess.io application calendar).
            if not cand.get("call_submission_deadline"):
                companion_deadline = _follow_companion_for_deadline(soup, link)
                if companion_deadline:
                    cand["call_submission_deadline"] = companion_deadline
                    cand["_deadline_from_companion"] = True
        # Final fallback: year-in-URL heuristic.
        if not cand.get("call_submission_deadline"):
            yr = _detect_url_year(link, cand.get("opportunity_title", ""))
            today = date.today()
            if yr and yr < today.year:
                cand["call_submission_deadline"] = date(yr, 12, 31)
                cand["_deadline_from_url_year"] = True

    # Brief description — prefer meta tags / first paragraph for HTML,
    # else the leading text from the PDF body.
    if not cand.get("brief_description"):
        desc = _extract_description_from_soup(soup) if soup is not None else None
        if not desc and text:
            # PDF path: take the first ~600 chars of meaningful text.
            stripped = text.strip()
            if len(stripped) > 40:
                desc = stripped[:600]
        if desc:
            cand["brief_description"] = desc

    # Award amount — fill estimated_value from the title or body when the source
    # didn't provide one (Stanford seed funding et al. bury it in the page text).
    if cand.get("call_award_value") in (None, "", 0):
        amt, cur = _extract_amount(cand.get("opportunity_title") or "", text or "")
        if amt is not None:
            cand["call_award_value"] = amt
            if cur and not cand.get("currency"):
                cand["currency"] = cur

    # Eligibility — append to brief_description so the country gate in
    # auto_scorer.country_eligible() sees it. (The gate scans
    # title + brief_description + call_geographic_scope + focus_theme + funder.)
    elig = _extract_eligibility_from_text(text)
    if elig:
        existing = cand.get("brief_description") or ""
        if elig.lower() not in existing.lower():
            cand["brief_description"] = (
                existing + ("\n\n" if existing else "") + "Eligibility: " + elig
            )[:1800]

    # LAST RESORT — LLM-assisted extraction. Only runs when (a) the user
    # has set ANTHROPIC_API_KEY in .env, and (b) the regex pipeline left
    # the deadline OR brief_description empty after every other strategy
    # above. This is exactly the Pierre Fabre case: the deadline lives
    # in a banner image / on a companion site that no regex can read.
    #
    # Cost: ~$0.0008 per call at Haiku tier. Only triggered for the
    # subset of candidates the regex layer couldn't fully fill — usually
    # a handful per scan, not the full 100.
    missing_deadline = not cand.get("call_submission_deadline")
    missing_desc = not cand.get("brief_description") or len(
        (cand.get("brief_description") or "")
    ) < 60
    if (missing_deadline or missing_desc) and text:
        try:
            from core.llm_extractor import extract as _llm_extract, is_enabled as _llm_on
        except ImportError:
            _llm_on = lambda: False  # noqa: E731
            _llm_extract = None
        if _llm_on() and _llm_extract is not None:
            llm = _llm_extract(
                title=cand.get("opportunity_title", "") or "",
                url=link,
                page_text=text,
            )
            if llm:
                if missing_deadline and llm.get("call_submission_deadline"):
                    try:
                        cand["call_submission_deadline"] = date.fromisoformat(
                            llm["call_submission_deadline"]
                        )
                        cand["_deadline_from_llm"] = True
                    except ValueError:
                        pass
                if missing_desc and llm.get("brief_description"):
                    cand["brief_description"] = llm["brief_description"][:1800]
                    cand["_desc_from_llm"] = True

    return cand


# Title fragments that signal blog / explainer / FAQ content, NOT an RFP.
_BLOG_TITLE_RE = re.compile(
    r"(what we (do|don['']t|will|won['']t) ?(fund|support)?|"
    r"how (we|to) (work|apply|fund)|"
    r"guide(line)?s? (for|to|on)|"
    r"about (our|the|us)|"
    r"frequently asked|"
    r"read (about|what|more)|"
    r"learn (about|how|more)|"
    r"introduction to|"
    r"our (approach|process|priorities|values)|"
    r"meet (our|the) team|"
    r"contact us|"
    r"who we (are|fund))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
class ScraperNotImplemented(NotImplementedError):
    """Raised when a method dispatcher has no handler."""


def scan_source(source: dict[str, Any], *, extract_only: bool = False,
                fresh_uids: set[str] | None = None) -> list[dict[str, Any]]:
    """Return candidate RFP dicts for one source. Never raises — errors are
    logged and converted to an empty list so the orchestrator can move on.

    Incremental extraction (extract_only=True + fresh_uids): candidates whose
    opportunity_link is ALREADY in the global store and refreshed recently are
    dropped BEFORE the expensive detail-page deep-read + LLM enrichment, so a
    re-run doesn't re-crawl/re-extract still-fresh opportunities. When
    extract_only is False or fresh_uids is None, behaviour is unchanged (a new
    tenant / full scan still needs every opportunity). Only the HTML listing
    paths enrich per-candidate (that's where the LLM cost lives), so the skip is
    applied there; structured-API handlers are unaffected."""
    method = (source.get("method") or source.get("scrape_method") or "").lower()
    url = source.get("url")
    name = source.get("name") or url or "(unnamed)"
    # Reset this thread's incremental-skip tally for this source's scan.
    _reset_skipped()
    if not url:
        log.warning("scan_source: no URL for %s", name)
        return []

    try:
        # UNGM: route to the dedicated handler regardless of configured method —
        # the public /Public/Notice page is JS-loaded, but /Public/Notice/Search
        # is a keyless POST endpoint that returns the notices directly.
        if "ungm.org" in url.lower():
            return _scan_ungm(name, url)
        if "grantplus.unops.org" in url.lower():
            return _scan_unops(name, url)
        if "grants.chinnova.aau.org" in url.lower():
            return _scan_chinnova(name, url)
        # Grand Challenges family (Gates + GCGH): the /grant-opportunities page is
        # a Next.js app whose calls are embedded as __NEXT_DATA__ JSON — parse it
        # directly rather than rendering, regardless of configured method.
        if "grandchallenges.org" in url.lower() and "grant-opportunit" in url.lower():
            return _scan_grandchallenges(name, url)
        # RVO (Netherlands) — route the human subsidies page or the API to the
        # keyless JSON search endpoint.
        if "english.rvo.nl" in url.lower():
            return _scan_rvo(name, url)
        # Grand Challenges Canada — card-based listing (parse heading/body/deadline,
        # not the generic "View full details" button text).
        if "grandchallenges.ca" in url.lower():
            return _scan_grandchallenges_ca(name, url)
        # Coefficient Giving (ex-Open Philanthropy) — /funds/ lists ~14 fund pages
        # whose "Research & Updates" sections mix post types (Request for Proposals
        # / Blog / Cause Investigation / News). Crawl each fund page and keep ONLY
        # the RFP-type cards. Server-rendered HTML (needs a real browser UA).
        if "coefficientgiving.org" in url.lower():
            return _scan_coefficient_giving(name, url)
        # Packard — JS-rendered cards backed by the WordPress REST custom
        # post type `funding-opportunity`.
        if "packard.org" in url.lower():
            return _scan_packard(name, url)
        # World Bank "Business Opportunities" page is JS, but it's backed by the
        # keyless procnotices API — route either URL to that handler.
        if "worldbank.org" in url.lower() and ("opportunit" in url.lower()
                                               or "procnotices" in url.lower()):
            return _scan_worldbank_procurement(
                name, "https://search.worldbank.org/api/v2/procnotices")
        # Global Health EDCTP3 JU — the RSS feed carries title+link only (no budget
        # /deadline). Route to the SEDIA API filtered to EDCTP3 so we get structured
        # budget + deadline + identifier. (Per-source handler — DATA_SCHEMA_ETL §5.2.)
        if "global-health-edctp3.europa.eu" in url.lower() or "edctp3" in name.lower():
            return _scan_eu_funding_tenders(
                name, "https://api.tech.ec.europa.eu/search-api/prod/rest/search",
                text="EDCTP3")
        # Theme/country filtering now happens in core.scan_pipeline (policy-
        # driven, admin-configurable). Scrapers return raw candidates.
        if method == "rss":
            # Google Alerts feeds need a dedicated handler — entries link
            # to arbitrary third-party sites through Google's redirect,
            # so we unwrap URLs, set funder from destination domain, and
            # apply tighter noise filters.
            if "google.com/alerts/feeds" in url:
                return _scan_google_alerts(name, url)
            # ResearchNet (CIHR) feed carries real deadlines + funder per item.
            if "researchnet-recherchenet.ca" in url:
                return _scan_researchnet(name, url)
            return _scan_rss(name, url)
        if method == "rest_json":
            return _scan_rest_json(name, url)
        if method == "html":
            return _scan_html(name, url, extract_only=extract_only,
                              fresh_uids=fresh_uids)
        if method == "html_js":
            # Playwright-rendered scan for SPA donor portals (EC Funding
            # Portal, CZI, etc.) where the listing widget only appears
            # after JavaScript executes.
            return _scan_html_js(name, url, extract_only=extract_only,
                                 fresh_uids=fresh_uids)
        if method == "manual":
            return []  # nothing to scan; admin curates donor_sources directly
        raise ValueError(f"Unknown scrape method: {method!r}")
    except Exception as exc:  # pragma: no cover — surfaced via scan_logs.errors
        log.exception("scan_source error for %s (%s): %s", name, url, exc)
        # Re-raise so the orchestrator can record the error in scan_logs.
        raise


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------
def _scan_rss(name: str, url: str) -> list[dict[str, Any]]:
    # Some feeds (NIH Guide, ReliefWeb) return XML that feedparser flags as
    # malformed when fetched directly. Pre-fetch with requests so we get
    # browser-like headers, then hand the bytes to feedparser — which is more
    # forgiving with bytes input than with a URL it fetched itself.
    try:
        r = _http.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception:
        # Last-resort: try the URL directly.
        feed = feedparser.parse(
            url, agent=USER_AGENT,
            request_headers={"User-Agent": USER_AGENT},
        )
    if not feed.entries:
        # Quietly return [] — log via scan_logs.errors if truly broken
        # downstream, but don't crash the source if the feed is just empty.
        log.info("RSS %s returned 0 entries (bozo=%s)", name, getattr(feed, "bozo", False))
        return []
    out: list[dict[str, Any]] = []
    funder = _funder_from_source_name(name)
    for entry in feed.entries[:100]:  # cap per-source
        title = _clean(getattr(entry, "title", "") or "")
        link = _clean(getattr(entry, "link", "") or "")
        if not title or not link:
            continue
        summary = _clean(getattr(entry, "summary", "") or "")
        published = _parse_struct_time(getattr(entry, "published_parsed", None))
        out.append({
            "opportunity_title": title,
            "opportunity_link": link,
            "funding_agency": funder,
            "brief_description": summary[:1500] or None,
            "date_posted": published,
            "call_submission_deadline": None,
            "_source_origin": f"{name} (RSS)",
        })
    return out


# ---------------------------------------------------------------------------
# ResearchNet (CIHR) — special-case RSS handler
#
# The Canadian Institutes of Health Research funding portal publishes every
# current opportunity in one RSS feed (fodRss.do). Unlike a generic feed, each
# <item> description carries the REAL Application Deadline (ISO date, or "TBD"
# for rolling awards) plus the Registration/LOI deadline and the funder — so we
# populate submission_deadline straight from the feed. The detail pages
# (viewOpportunityDetails.do) render their Important-Dates table via AJAX, so
# the feed is the reliable static source and no Playwright is needed (works on
# Cloud Manual Scan too). Mapping → our extraction template:
#   <title>        -> opportunity_title   (keeps the "Team Grant: …" type prefix)
#   <guid>/<link>  -> opportunity_link     (the opportunity detail page)
#   "Application Deadline <date>" -> submission_deadline (skip "TBD")
#   <pubDate>      -> date_posted
#   funder tail    -> funding_agency (CIHR)
# ---------------------------------------------------------------------------
_RNET_APP_DEADLINE_RE = re.compile(
    r"Application\s*Deadline\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.I)
_RNET_DEADLINE_NOISE_RE = re.compile(
    r"(?:LOI|Outline|Registration|Application)\s*Deadline\s*(?:TBD|[0-9-]+)", re.I)


def _scan_researchnet(name: str, url: str) -> list[dict[str, Any]]:
    try:
        r = _http.get(
            url,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/rss+xml, application/xml, text/xml"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception:
        feed = feedparser.parse(
            url, agent=USER_AGENT, request_headers={"User-Agent": USER_AGENT})
    if not feed.entries:
        log.info("ResearchNet RSS %s returned 0 entries (bozo=%s)",
                 name, getattr(feed, "bozo", False))
        return []
    out: list[dict[str, Any]] = []
    for entry in feed.entries[:100]:
        title = _clean(getattr(entry, "title", "") or "")
        # guid is a permalink to the opportunity detail page.
        link = _clean(getattr(entry, "link", "")
                      or getattr(entry, "id", "") or "")
        if not title or not link:
            continue
        summary_html = getattr(entry, "summary", "") or ""
        # Real Application Deadline only; "TBD" rolling awards stay None so the
        # eligibility gate parks them rather than treating them as expired.
        m = _RNET_APP_DEADLINE_RE.search(summary_html)
        deadline = _parse_iso_date(m.group(1)) if m else None
        # Brief = description text with the deadline boilerplate + funder tail
        # stripped, so it reads as a real summary for the policy/theme gate.
        text = BeautifulSoup(summary_html, "html.parser").get_text(" ")
        text = _RNET_DEADLINE_NOISE_RE.sub("", text)
        text = re.sub(
            r"Canadian Institutes of Health Research\s*\|\s*Government of Canada",
            "", text, flags=re.I)
        brief = _clean(text)
        published = _parse_struct_time(getattr(entry, "published_parsed", None))
        out.append({
            "opportunity_title": title,
            "opportunity_link": link,
            "funding_agency": "Canadian Institutes of Health Research",
            "brief_description": brief[:1500] or None,
            "date_posted": published,
            "call_submission_deadline": deadline,
            # CIHR funds Canadian institutions by default; the "Canadian" cue lives
            # in the funder name (excluded from geo detection), so stamp Canada as
            # the default scope. The geo gate then drops Canada-only calls and
            # keeps only those whose text opens beyond Canada (international / LMIC
            # / a named in-scope region) — the "scope beyond Canada" rule.
            "call_geographic_scope": ["Canada"],
            "_source_origin": f"{name} (RSS)",
        })
    return out


# ---------------------------------------------------------------------------
# Google Alerts — special-case RSS handler
#
# Differences from curated donor RSS:
#   * Each entry links to an arbitrary third-party site (donor, news,
#     aggregator, social, government, foundation, etc.).
#   * Google wraps the link in its tracking redirect:
#       https://www.google.com/url?rct=j&sa=t&url=<REAL_URL>&usg=...
#     We need to unwrap to get the destination.
#   * The "funder" should be the destination domain, not "Google Alert".
#   * Some entries are news articles / LinkedIn / Twitter / YouTube
#     posts ABOUT a funding opportunity rather than the opportunity
#     itself — they're noise; filter them out.
#   * Google's snippet (entry.summary) is high-quality — keep it as the
#     starting brief_description so the policy gate has something to
#     work with even before detail-page enrichment.
# ---------------------------------------------------------------------------
from urllib.parse import unquote, parse_qs

# Domains that surface RFP mentions but aren't the RFP source themselves.
# Strip them at the alert level — they only inflate dedup work.
_GOOGLE_ALERT_NOISE_DOMAINS = (
    "twitter.com", "x.com",
    "facebook.com", "fb.com", "m.facebook.com",
    "linkedin.com", "lnkd.in",
    "youtube.com", "youtu.be",
    "instagram.com", "reddit.com",
    "tiktok.com",
    "news.google.com", "feedburner.com",
    "scholar.google.com",
)


def _unwrap_google_url(url: str) -> str:
    """Strip the google.com/url? redirect wrapper to get the real target."""
    if not url:
        return url
    if "google.com/url" not in url:
        return url
    try:
        parsed = urlsplit(url)
        qs = parse_qs(parsed.query)
        for key in ("url", "q"):  # Google uses either depending on context
            if key in qs and qs[key]:
                return unquote(qs[key][0])
    except Exception:
        pass
    return url


def _is_google_alert_noise(url: str) -> bool:
    """Return True if the URL is from a known noise domain."""
    try:
        host = urlsplit(url).netloc.lower().lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in _GOOGLE_ALERT_NOISE_DOMAINS)
    except Exception:
        return False


def _domain_to_funder(url: str) -> str:
    """Convert a URL host into a readable funder label, e.g.
    'https://www.fondation-mma.org/calls' → 'fondation-mma.org'."""
    try:
        host = urlsplit(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host or "(unknown)"
    except Exception:
        return "(unknown)"


def _scan_google_alerts(name: str, url: str) -> list[dict[str, Any]]:
    """Special-case RSS handler for Google Alerts feeds.

    Behavior differs from `_scan_rss` in three ways:
      1. Unwraps `google.com/url?` redirects on each entry's link.
      2. Sets funding_agency from the destination domain (not the
         alert name).
      3. Filters out social-media / news-aggregator noise domains.

    The cleaned candidate then flows into the standard enrichment +
    eligibility + scoring pipeline like any other source.
    """
    try:
        r = _http.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/atom+xml, application/rss+xml, text/xml",
            },
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        feed = feedparser.parse(r.content)
    except Exception:
        feed = feedparser.parse(
            url, agent=USER_AGENT,
            request_headers={"User-Agent": USER_AGENT},
        )
    if not feed.entries:
        log.info("Google Alert %s returned 0 entries", name)
        return []

    out: list[dict[str, Any]] = []
    skipped_noise = 0
    for entry in feed.entries[:100]:
        title = _clean(getattr(entry, "title", "") or "")
        raw_link = _clean(getattr(entry, "link", "") or "")
        link = _unwrap_google_url(raw_link)
        if not title or not link:
            continue
        if _is_google_alert_noise(link):
            skipped_noise += 1
            continue
        # Google's <summary> already strips bold tags etc. into clean
        # snippet text — use it as the starting description.
        summary = _clean(getattr(entry, "summary", "") or "")
        published = _parse_struct_time(getattr(entry, "published_parsed", None))
        out.append({
            "opportunity_title": title,
            "opportunity_link": link,
            "funding_agency": _domain_to_funder(link),
            "brief_description": summary[:1500] or None,
            "date_posted": published,
            "call_submission_deadline": None,
            "_source_origin": f"{name} (Google Alert)",
        })
    if skipped_noise:
        log.info(
            "Google Alert %s: skipped %d noise-domain entr%s (social / aggregator)",
            name, skipped_noise, "y" if skipped_noise == 1 else "ies",
        )
    return out


# ---------------------------------------------------------------------------
# REST JSON — Grants.gov is the only well-known endpoint in sources.yaml
# ---------------------------------------------------------------------------
def _scan_rest_json(name: str, url: str) -> list[dict[str, Any]]:
    if "api.grants.gov" in url:
        return _scan_grants_gov(name, url)
    if "api.tech.ec.europa.eu/search-api" in url:
        return _scan_eu_funding_tenders(name, url)
    if "search.worldbank.org/api/v2/procnotices" in url:
        return _scan_worldbank_procurement(name, url)
    if "api.ted.europa.eu" in url:
        return _scan_ted(name, url)
    if "find-tender.service.gov.uk" in url:
        return _scan_ocds(name, url,
                          notice_base="https://www.find-tender.service.gov.uk/Notice/",
                          geo="United Kingdom")
    if "contractsfinder.service.gov.uk" in url:
        return _scan_ocds(name, url,
                          notice_base="https://www.contractsfinder.service.gov.uk/Notice/",
                          geo="United Kingdom")
    # NOTE: The World Bank projects API (search.worldbank.org/api/v3/projects)
    # returns ONGOING / COMPLETED projects, not open funding opportunities — use
    # the procnotices endpoint above for open procurement instead.
    log.info("REST JSON endpoint not specifically handled: %s — returning []", url)
    return []


def _fetch_grants_gov_details(numeric_id: str) -> dict[str, Any] | None:
    """Call Grants.gov fetchOpportunity to get the full opportunity payload.

    Returns None on any failure. Used to enrich candidates with:
      * synopsis / description text         → brief_description
      * estimated total program funding      → estimated_value
      * response / posting / archive dates  → submission_deadline / date_posted
      * funding activity categories          → program_area
      * applicant types + cost sharing       → notes (eligibility hints)
      * funding instruments                  → funding_window
      * CFDA + expected # of awards          → notes

    HISTORICAL TRAP — the request body MUST use `opportunityId`. The
    previous implementation sent `oppId` and the API silently returned a
    bare error stub `{serverURI, message}` with NO synopsis/data — which
    is why every Grants.gov RFP since launch has had blank
    `estimated_value`, `brief_description`, etc. The data was always
    there; we just weren't asking for it correctly.
    """
    if not numeric_id:
        return None
    try:
        # Coerce to int so the API accepts the body (string ids also fail
        # silently and return the error stub).
        opp_id_int = int(str(numeric_id).strip())
    except (TypeError, ValueError):
        return None
    try:
        r = _http.post(
            "https://api.grants.gov/v1/api/fetchOpportunity",
            json={"opportunityId": opp_id_int},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        j = r.json() or {}
    except Exception as exc:
        log.debug("fetchOpportunity %s failed: %s", numeric_id, exc)
        return None
    # The API returns 200 with {data: {serverURI, message}} when the body
    # shape is wrong — detect that case so callers don't think they got
    # real data. Real responses always have data.synopsis populated.
    data = j.get("data") or {}
    if not isinstance(data, dict) or "synopsis" not in data:
        log.warning("fetchOpportunity %s returned no synopsis (stub response)", numeric_id)
        return None
    return j


def _scan_grants_gov(name: str, url: str) -> list[dict[str, Any]]:
    """Grants.gov search2 API. Posts JSON body, returns oppHits list, then
    enriches each hit with fetchOpportunity for the synopsis + funding info."""
    out: list[dict[str, Any]] = []
    for keyword in ["global health", "infectious disease", "HIV", "tuberculosis", "malaria"]:
        body = {
            "rows": 25,
            "keyword": keyword,
            # 'posted' only — forecasts have no firm close date and pollute
            # the table with deadline=None rows that look like expired RFPs.
            # When a forecast goes live it'll show up here on the next scan.
            "oppStatuses": "posted",
            "sortBy": "openDate|desc",
        }
        try:
            r = _http.post(
                url, json=body,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json() or {}
        except Exception as exc:
            log.warning("Grants.gov keyword=%s failed: %s", keyword, exc)
            continue
        hits = (((data.get("data") or {}).get("oppHits")) or [])
        for h in hits:
            title = _clean(h.get("title") or "")
            # Detail page URL needs the NUMERIC internal `id`, not the
            # human-readable `number` (e.g. "RFA-AI-28-024"). Passing the
            # number makes every link redirect to the generic search page.
            numeric_id = _clean(h.get("id") or "")
            opp_number = _clean(h.get("number") or "")
            link = (
                f"https://www.grants.gov/search-results-detail/{numeric_id}"
                if numeric_id else None
            )
            agency = _clean(h.get("agencyName") or h.get("agencyCode") or "")
            close_iso = h.get("closeDate")
            open_iso = h.get("openDate")
            if not title:
                continue
            out.append({
                "opportunity_title": title,
                "opportunity_link": link,
                "opportunity_id": opp_number,  # surface the human-readable RFA number
                "funding_agency": agency or "US Federal (Grants.gov)",
                "brief_description": None,
                "date_posted": _parse_iso_date(open_iso),
                "call_submission_deadline": _parse_iso_date(close_iso),
                "_source_origin": f"{name} (kw={keyword!r})",
                "_grants_gov_id": numeric_id,
            })
    # Dedup within this scrape by opportunity_link / title
    deduped = _dedup_by_link_or_title(out)

    # Enrich each unique hit with the detail payload. Until the
    # `oppId` → `opportunityId` bug was fixed (2026-06-04), this loop
    # was a no-op — every detail call returned the error stub and we
    # were storing only the listing-call fields. Now that the detail
    # call works, enrichment carries the rest of the rich payload
    # (estimated_value, program_area, etc.) so we want to cover EVERY
    # deduped candidate, not just the first 50. At 5 keywords × 25 rows
    # we typically see 60-80 unique hits; each detail call is ~500ms-1s
    # → +30-80s scan time, but the alternative is shipping the table
    # with most rows empty (the bug we just fixed).
    #
    # Field map per config/donor_field_map.yaml → grants_gov.detail_field_map.
    # This block is the executor; keep it aligned with the dictionary when
    # the donor exposes new fields.
    enrich_cap = int(os.environ.get("GRANTS_GOV_ENRICH_CAP", "100"))
    for cand in deduped[:enrich_cap]:
        details = _fetch_grants_gov_details(cand.get("_grants_gov_id", ""))
        if not details:
            continue
        d = (details.get("data") or {}) if isinstance(details, dict) else {}
        syn = d.get("synopsis") or {}

        # --- opportunity_id (override list value if detail is canonical) ---
        opp_num = _clean(d.get("opportunityNumber") or "")
        if opp_num and not cand.get("opportunity_id"):
            cand["opportunity_id"] = opp_num

        # --- funding_agency: prefer the human-readable agencyDetails.agencyName
        # ("Dept. of the Army -- USAMRAA") over the listing's agencyCode
        # ("DOD-AMRAA") which is opaque to reviewers ---
        agency_detail = (d.get("agencyDetails") or {}).get("agencyName")
        if agency_detail:
            cand["funding_agency"] = _clean(agency_detail)

        # --- brief_description (synopsisDesc, HTML-stripped, capped) ---
        desc = _clean(syn.get("synopsisDesc") or "")
        if desc:
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = re.sub(r"\s+", " ", desc).strip()
            cand["brief_description"] = desc[:1800]

        # --- estimated_value: PRIMARY = total program funding, FALLBACK = per-award ceiling.
        # Both fields arrive as strings like "11165000" or "none"/None. ---
        money_paths = [syn.get("estimatedFunding"), syn.get("awardCeiling")]
        for raw in money_paths:
            money = _coerce_money(raw)
            if money is not None:
                cand["call_award_value"] = money
                cand["currency"] = "USD"
                break

        # --- distinct award-scope fields (public-site reporting) ---
        af = _coerce_money(syn.get("awardFloor"))
        if af is not None:
            cand["call_award_floor"] = af
        ac = _coerce_money(syn.get("awardCeiling"))
        if ac is not None:
            cand["call_award_ceiling"] = ac
        tot = _coerce_money(syn.get("estimatedFunding"))
        if tot is not None:
            cand["total_program_funding"] = tot
        try:
            na = int(str(syn.get("numberOfAwards")).strip())
            if na > 0:
                cand["expected_awards"] = na
        except (TypeError, ValueError):
            pass
        fon = _clean(d.get("opportunityNumber") or "") or cand.get("opportunity_id")
        if fon:
            cand["funding_opportunity_number"] = fon

        # --- date fields (DB column is `date_posted`, not `date_posted`) ---
        dp = _parse_iso_date(syn.get("postingDate") or "")
        if dp:
            cand["date_posted"] = dp
        # Detail-call deadline overrides the list-call value (it's the
        # authoritative `responseDate` rather than the abbreviated closeDate).
        dl = _parse_iso_date(syn.get("responseDate") or "")
        if dl:
            cand["call_submission_deadline"] = dl

        # --- program_area: from fundingActivityCategories[*].description ---
        cats = syn.get("fundingActivityCategories") or []
        if isinstance(cats, list):
            labels = [_clean(c.get("description") or "") for c in cats if isinstance(c, dict)]
            labels = [x for x in labels if x]
            if labels:
                cand["call_domain_areas"] = labels

        # --- funding_window: instrument types joined ---
        instrs = syn.get("fundingInstruments") or []
        if isinstance(instrs, list):
            labels = [_clean(i.get("description") or "") for i in instrs if isinstance(i, dict)]
            labels = [x for x in labels if x]
            if labels:
                cand["funding_window"] = " / ".join(labels)

        # --- notes: eligibility + cost-share + CFDA + expected #awards ---
        notes_parts: list[str] = []
        applicants = syn.get("applicantTypes") or []
        if isinstance(applicants, list):
            a_labels = [_clean(a.get("description") or "") for a in applicants if isinstance(a, dict)]
            a_labels = [x for x in a_labels if x]
            if a_labels:
                notes_parts.append("Eligible applicants: " + "; ".join(a_labels))
                # Structured copy for the applicant-type match gate (avoids
                # re-parsing the notes string downstream).
                cand["_applicant_types"] = a_labels
                # SAFE US-only drop #2: applicant types are PURELY US government /
                # public tiers (state/county/city/tribal govts, school districts,
                # public universities) with no open type — structurally domestic.
                try:
                    from core.auto_scorer import grants_gov_government_only as _gov_only
                    if _gov_only(a_labels):
                        cand["_drop_us_only"] = True
                except Exception:
                    pass
        # "Additional Information on Eligibility" — the DECISIVE geography
        # signal (the "domestic" test; see docs/SCAN_CLASSIFICATION_ALGORITHM.md
        # §6). Previously dropped on the floor, which is why US-domestic-only
        # opportunities couldn't be auto-rejected. Capture it now; the hard
        # geography gate that Declines US-only RFPs for an LMIC deployment is
        # wired in the scoring step.
        elig_text = _clean(syn.get("applicantEligibilityDesc") or "")
        if elig_text:
            elig_text = re.sub(r"<[^>]+>", " ", elig_text)
            elig_text = re.sub(r"\s+", " ", elig_text).strip()
            notes_parts.append("Eligibility detail: " + elig_text)
            # SAFE US-only drop: only when the eligibility text EXPLICITLY
            # restricts to US/domestic applicants (and doesn't also welcome
            # foreign/international ones). Cuts the HRSA/CDC-domestic noise
            # without risking valid USAID/CDC-global calls. (Reuses the same
            # detector everywhere; local import avoids an import cycle.)
            try:
                from core.auto_scorer import grants_gov_domestic_only as _us_only
                if _us_only(elig_text):
                    cand["_drop_us_only"] = True
            except Exception:
                pass
        # grants.gov is the US FEDERAL portal — default each call to US-domestic
        # geography (persisted so SCREENING can reject it for a non-US org) UNLESS
        # its eligibility explicitly welcomes foreign / international applicants.
        # That exception is exactly when global-health calls (CDC-global, Fogarty,
        # USAID) survive — they keep whatever geography the synopsis names, and the
        # geo gate then decides on the real scope. Don't override a richer scope the
        # synopsis already produced (e.g. a named region).
        _foreign_kw = any(k in (elig_text or "").lower() for k in (
            "foreign", "international", "non-u.s", "non-us",
            "outside the united states", "low- and middle-income", "lmic",
            "developing countr", "any country", "worldwide", "globally"))
        # NEGATION-aware: "Foreign entities are not eligible" contains "foreign" but is
        # the OPPOSITE of foreign-welcome — don't let it suppress the US-geography default.
        try:
            from core.auto_scorer import foreign_applicants_excluded as _excl
        except Exception:
            _excl = lambda _t: False
        _foreign_ok = _foreign_kw and not _excl(elig_text)
        if not _foreign_ok and not cand.get("call_geographic_scope"):
            cand["call_geographic_scope"] = ["United States"]
        if cand.get("_applicant_types"):
            cand["eligibility_applicant_types"] = cand["_applicant_types"]
        cs = syn.get("costSharing")
        if cs is not None and str(cs).lower() not in ("none", ""):
            notes_parts.append(f"Cost sharing required: {cs}")
        cfdas = d.get("cfdas") or []
        if isinstance(cfdas, list) and cfdas:
            first = cfdas[0] if isinstance(cfdas[0], dict) else {}
            cfda_num = _clean(first.get("cfdaNumber") or "")
            cfda_title = _clean(first.get("programTitle") or "")
            if cfda_num:
                notes_parts.append(f"CFDA {cfda_num}{' — ' + cfda_title if cfda_title else ''}")
        n_awards = syn.get("numberOfAwards")
        if n_awards and str(n_awards).lower() not in ("none", ""):
            notes_parts.append(f"Expected awards: {n_awards}")
        if notes_parts:
            cand["notes"] = " | ".join(notes_parts)[:1800]

    # Keep US-only opportunities (the `_drop_us_only` / `_applicant_types` flags
    # ride along) instead of silently dropping them at scrape time: the gate
    # (`us_domestic_only_reject`) now rejects them as a logged `geography` reason,
    # so they land in scan_decisions where they can be human-verified and feed the
    # learning loop (capture-everything → P/P/D/R). Drop only the internal id key.
    for cand in deduped:
        cand.pop("_grants_gov_id", None)
    return deduped


def _coerce_money(raw: Any) -> float | None:
    """Coerce a Grants.gov money-as-string into a float, treating the
    string literal 'none' (which the API uses for unspecified fields) as
    null. Returns None when the value is missing or unparseable."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() == "none":
        return None
    s = s.replace(",", "").replace("$", "")
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _scan_worldbank(name: str, url: str) -> list[dict[str, Any]]:
    """World Bank Projects API. Returns active health-sector projects."""
    params = {
        "format": "json",
        "rows": 50,
        "fl": "id,project_name,boardapprovaldate,closingdate,countryname_exact",
        "kw": "health",
        "status_exact": "Active",
    }
    try:
        r = _http.get(
            url, params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
    except Exception as exc:
        log.warning("World Bank query failed: %s", exc)
        return []
    projects = (data.get("projects") or {})
    out: list[dict[str, Any]] = []
    for pid, p in projects.items():
        title = _clean(p.get("project_name") or "")
        if not title:
            continue
        out.append({
            "opportunity_title": title,
            "opportunity_link": f"https://projects.worldbank.org/en/projects-operations/project-detail/{pid}",
            "funding_agency": "World Bank",
            "brief_description": None,
            "date_posted": _parse_iso_date(p.get("boardapprovaldate")),
            "call_submission_deadline": _parse_iso_date(p.get("closingdate")),
            "_source_origin": name,
        })
    return out


# ---------------------------------------------------------------------------
# Open-data / public-sector APIs (free + permissive reuse licenses)
# ---------------------------------------------------------------------------
# All five below are free, key-less, and publish under reuse-permitting licenses
# (EU reuse policy / World Bank CC-BY 4.0 / UK Open Government Licence v3 / EU
# PSI), so candidates are safe to republish — unlike aggregator APIs whose ToS
# forbid redistribution. Each returns the standard candidate dict; the per-org
# eligibility gate (auto_scorer.is_eligible) decides theme/geo relevance, so the
# handlers pull broadly and let the gate filter per tenant.

def _wb_date(s: Any) -> date | None:
    """World Bank emits noticedate as '19-Jun-2026'; submission_date as ISO."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%d-%b-%Y").date()
    except (ValueError, TypeError):
        return _parse_iso_date(str(s)[:10])


# Identifier-prefix → funder name for EU Joint Undertakings worth attributing
# distinctly (the rest stay generic "European Commission").
_EU_JU_FUNDERS = {
    "EDCTP3": "Global Health EDCTP3 Joint Undertaking",
    "IHI": "Innovative Health Initiative",
}


def _eu_budget(bo_str: str) -> tuple[float | None, str | None, float | None,
                                      float | None, int | None]:
    """Parse a SEDIA `budgetOverview` JSON blob →
    (total, currency, per-grant floor, per-grant ceiling, expected #grants).
    Sums every action's per-year budget for the total, and reads each action's
    minContribution / maxContribution / expectedGrants for the per-grant envelope.
    EU budgets are EUR (the blob omits currency)."""
    try:
        b = json.loads(bo_str) if bo_str else {}
    except (ValueError, TypeError):
        return None, None, None, None, None
    tot, cur = 0.0, None
    floor = ceil = None
    awards = 0
    for actions in (b.get("budgetTopicActionMap") or {}).values():
        if not isinstance(actions, list):
            continue
        for a in actions:
            cur = cur or a.get("currency")
            for v in (a.get("budgetYearMap") or {}).values():
                try:
                    tot += float(v)
                except (TypeError, ValueError):
                    pass
            try:
                mn = a.get("minContribution")
                if mn not in (None, "", 0, "0"):
                    floor = float(mn) if floor is None else min(floor, float(mn))
            except (TypeError, ValueError):
                pass
            try:
                mx = a.get("maxContribution")
                if mx not in (None, "", 0, "0"):
                    ceil = float(mx) if ceil is None else max(ceil, float(mx))
            except (TypeError, ValueError):
                pass
            try:
                awards += int(a.get("expectedGrants") or 0)
            except (TypeError, ValueError):
                pass
    return ((tot or None), (cur or ("EUR" if tot else None)),
            floor, ceil, (awards or None))


def _per_award_value(total: float | None, floor: float | None, ceil: float | None,
                     awards: int | None) -> float | None:
    """What ONE winner gets, from the portal's structured budget — None when the portal
    doesn't say.

    In preference order: the stated per-grant ceiling (the "up to" figure, and the same
    HIGHEST-of-a-range rule the regex extractor follows), then the floor, then the
    envelope split across the number of grants the call expects to make.

    A programme envelope is NEVER returned on its own. Splitting EUR 33M across an
    unknown number of grants is not an award size, and a wrong number here is worse than
    no number: the criteria treat a missing value as "we don't know" and exclude it, but
    a present one is measured against the org's funding band as if it were fact."""
    for v in (ceil, floor):
        if v:
            return float(v)
    if total and awards and awards > 0:
        return float(total) / float(awards)
    return None


def _eu_deadlines(val: Any) -> list:
    """Parse a SEDIA `deadlineDate` (a single ISO datetime OR, for a two-stage topic, a
    LIST [stage-1, stage-2]) → a sorted list of date objects. The EFFECTIVE deadline is the
    LAST element (stage-2): the portal flips a two-stage topic to Closed once stage-1 passes,
    but the opportunity is still live until stage-2 — so we key expiry off the latest date."""
    vals = val if isinstance(val, list) else ([val] if val else [])
    out = []
    for v in vals:
        d = _parse_iso_date(str(v)[:10]) if v else None
        if d:
            out.append(d)
    return sorted(out)


def _eu_funder(identifier: str) -> str:
    u = (identifier or "").upper()
    for key, fname in _EU_JU_FUNDERS.items():
        if key in u:
            return fname
    return "European Commission (EU Funding & Tenders)"


def _html_text(s: Any, limit: int = 60_000) -> str:
    """Strip HTML to plain text, bounded. SEDIA `descriptionByte` / `topicConditions`
    arrive as HTML blobs; this yields the readable text for extraction. Best-effort."""
    if not s:
        return ""
    try:
        txt = BeautifulSoup(str(s), "html.parser").get_text(" ", strip=True)
    except Exception:
        txt = re.sub(r"<[^>]+>", " ", str(s))
    return _clean(txt)[:limit]


def _scan_eu_funding_tenders(name: str, url: str, *,
                             text: str = "***") -> list[dict[str, Any]]:
    """EU Funding & Tenders Portal (SEDIA search API). Free, key-less (apiKey
    'SEDIA' is the public token). `text` filters the free-text search ("***" = all;
    "EDCTP3" restricts to the Global Health EDCTP3 JU — the generic query buries
    EDCTP3 under thousands of mainstream Horizon calls). Pulls OPEN + FORTHCOMING
    grants & tenders and maps
    the STRUCTURED metadata — budget (budgetOverview), deadline, identifier, type —
    into our schema. Geography is captured from the call text downstream (NOT
    hardcoded EU: EDCTP3 / Global-Health JUs target sub-Saharan Africa). The query
    body must be a multipart part with explicit application/json, else the API 500s."""
    out: list[dict[str, Any]] = []
    # forthcoming + open. For a TARGETED query (e.g. text="EDCTP3", a small result
    # set) ALSO pull CLOSED (31094503): the portal flips a two-stage topic to Closed
    # once stage-1 passes even though a later stage-2 deadline is still in the future,
    # so the deadline alone reads "open". We ingest those Closed topics and let the gate
    # reject them (logged for the learning loop + flips a row we caught while it was
    # open). The generic "***" sweep stays open-only so closed history can't crowd the
    # bounded result window.
    _statuses = ["31094501", "31094502"]
    if text and text != "***":
        _statuses.append("31094503")                     # closed
    q = {"bool": {"must": [
        {"terms": {"type": ["1", "2"]}},                  # 1=tender, 2=grant
        {"terms": {"status": _statuses}},
    ]}}

    def _first(md: dict, k: str) -> str:
        v = md.get(k)
        if isinstance(v, list):
            return str(v[0]) if v else ""
        return str(v) if v is not None else ""

    for page in (1, 2):
        try:
            r = _http.post(
                url,
                params={"apiKey": "SEDIA", "text": text,
                        "pageSize": "50", "pageNumber": str(page)},
                files={"query": ("query.json", json.dumps(q), "application/json"),
                       "languages": ("languages.json", json.dumps(["en"]),
                                     "application/json")},
                headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
        except Exception as exc:
            log.warning("EU F&T page=%s failed: %s", page, exc)
            break
        if not results:
            break
        for it in results:
            md = it.get("metadata") or {}
            title = _clean(_first(md, "title") or it.get("title") or "")
            if not title:
                continue
            ident = _clean(_first(md, "identifier"))
            amt, cur, _floor, _ceil, _awards = _eu_budget(_first(md, "budgetOverview"))
            # Two-stage deadline handling: keep ALL stage deadlines and use the LAST
            # (stage-2) as the effective submission deadline, so the row doesn't expire when
            # stage-1 passes (the portal marks it Closed but stage-2 is still open).
            _dls = _eu_deadlines(md.get("deadlineDate"))
            _eff_deadline = _dls[-1] if _dls else None
            _two_stage = (_first(md, "deadlineModel").lower() == "two-stage"
                          or len(_dls) >= 2)
            action = _first(md, "typesOfAction").lower()
            is_prize = "prize" in action
            status = _first(md, "status")
            # status 31094501 = forthcoming (future) → announcement, not a live call.
            opp_type = ("announcement" if status == "31094501"
                        else "award" if is_prize else "grant")
            # 31094503 = CLOSED. The portal's explicit closed flag OVERRIDES the deadline
            # (a two-stage topic stays Closed even with a future stage-2 date) — the gate
            # hard-rejects on this.
            _is_closed = status == "31094503"
            # RICH metadata the SEDIA API already returns but we previously ignored: the
            # full topic description + conditions live behind a "Show more" accordion on
            # the JS portal, but the API hands them over as HTML. Folding them into
            # raw_text gives synthesis, the LLM judge, and the regex extractors (duration,
            # geography, program-area) the REAL call text instead of the one-line summary
            # — which is why Focus Areas / Geographic Scope / Duration came up blank.
            desc = _html_text(_first(md, "descriptionByte"))
            cond = _html_text(_first(md, "topicConditions"))
            tags = md.get("tags") if isinstance(md.get("tags"), list) else []
            kws = md.get("keywords") if isinstance(md.get("keywords"), list) else []
            extra = " ".join(str(t) for t in (list(tags) + list(kws)))
            full_text = "\n\n".join(p for p in (desc, cond, extra) if p)[:60_000]
            # EU action family: a Coordination & Support Action (CSA) is capacity /
            # coordination by definition (never an intervention); RIA/IA fund research /
            # innovation. Feeds capacity_only_reject + donor-intel enrichment.
            if "coordination and support" in action:
                action_family = "CSA"
            elif "research and innovation action" in action:
                action_family = "RIA"
            elif "innovation action" in action:
                action_family = "IA"
            else:
                action_family = None
            # Deterministic geography seed from the CONTROLLED tags only (SEDIA tags
            # literally include "Sub-Saharan Africa"/"Africa" for EDCTP). We deliberately
            # do NOT scan the free-text description here: it name-drops regions
            # incidentally ("AU-EU partnership", "global standards", "non-EU countries")
            # that are context, not scope — exactly the trap the context-aware geography
            # rules guard against. The LLM judge (context-aware prompt) refines from there.
            try:
                from core import geographies as _geo
                geo_seed = sorted(_geo.broad_geos_in_text(
                    " ".join(str(t) for t in tags).lower()))
                # "Global Health" (a THEME tag) trips the "global" synonym for the
                # worldwide tier. When a specific region is also present, that generic
                # tier is noise from the theme, not scope — drop it.
                if len(geo_seed) > 1:
                    geo_seed = [g for g in geo_seed
                                if g != "Global / worldwide"] or geo_seed
            except Exception:
                geo_seed = []
            out.append({
                "opportunity_title": title,
                "opportunity_link": it.get("url"),
                "opportunity_id": ident,
                "funding_opportunity_number": ident,
                "funding_agency": _eu_funder(ident),
                "brief_description": _clean(it.get("summary") or "")[:1800] or None,
                # Set _page_text (NOT just raw_text): the regex extractors
                # (extract._blob / build_record), the LLM judge, AND synthesis all read
                # _page_text — raw_text alone would only reach synthesis, leaving
                # duration/geography extraction + the judge still starved on the summary.
                "_page_text": full_text or None,
                "raw_text": full_text or None,
                "call_geographic_scope": geo_seed or None,
                "date_posted": _parse_iso_date(_first(md, "startDate")[:10]),
                # Effective deadline = LAST (stage-2) date for a two-stage topic, else the
                # single date. Survives the portal flipping the topic to Closed after stage-1.
                "call_submission_deadline": _eff_deadline,
                # Window label: surface the two-stage nature (stage-1 concept → stage-2 full).
                "funding_window": "Two-stage" if _two_stage else None,
                # `amt` is the PROGRAMME ENVELOPE — _eu_budget sums every action's
                # per-year budget — and it was going into call_award_value, the field
                # that means "what one winner gets". They are different quantities and
                # the gap is not small: EDCTP3 topic DIGIT-02 funds 8 projects at about
                # EUR 2.25M each, and the envelope stored against it was EUR 33M, the two
                # topics of that call added together. PREFER-6 then measured a EUR 2.25M
                # award against the org's absorptive ceiling and reported the call as
                # eight times too big to take on.
                #
                # The envelope has a column of its own, so it goes there, and the
                # per-award value is only claimed when the portal actually states one.
                "total_program_funding": amt,
                "call_award_value": _per_award_value(amt, _floor, _ceil, _awards),
                "currency": cur,
                "call_award_floor": _floor,
                "call_award_ceiling": _ceil,
                "expected_awards": _awards,
                "solicitation_type": "Prize" if is_prize else None,
                "instrument_type": "Award" if is_prize else (
                    "Grant" if _first(md, "type") == "2" else "Contract"),
                "opportunity_type": opp_type,
                "_action_family": action_family,
                "_tags": tags,
                # Stage deadlines (ISO) so the gate/UI can reason about two-stage survival.
                "_deadline_stages": [d.isoformat() for d in _dls] or None,
                "_closed": _is_closed,
                "_source_origin": f"{name} (status={status})",
            })
    return _dedup_by_link_or_title(out)


def _scan_worldbank_procurement(name: str, url: str) -> list[dict[str, Any]]:
    """World Bank procurement notices (procnotices API). Free; WB data is
    CC-BY 4.0. Skips 'Contract Award' notices (already awarded) — keeps open bids
    / EOIs / GPNs / RFQs. Country → call_geographic_scope so the geo gate can act."""
    out: list[dict[str, Any]] = []
    try:
        r = _http.get(
            url, params={"format": "json", "rows": 100},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        notices = (r.json() or {}).get("procnotices") or []
    except Exception as exc:
        log.warning("World Bank procurement failed: %s", exc)
        return []
    for n in notices:
        ntype = _clean(n.get("notice_type") or "")
        if "award" in ntype.lower():
            continue  # already awarded — not an open opportunity
        proj = _clean(n.get("project_name") or "")
        desc = _clean(n.get("bid_description") or "")
        title = (f"{proj}: {desc}" if proj and desc else (desc or proj))[:300]
        if not title:
            continue
        nid = _clean(n.get("id") or "")
        ctry = _clean(n.get("project_ctry_name") or "")
        out.append({
            "opportunity_title": title,
            "opportunity_link": (
                f"https://projects.worldbank.org/en/projects-operations/"
                f"procurement-detail/{nid}" if nid else
                "https://projects.worldbank.org/en/projects-operations/procurement"),
            "opportunity_id": _clean(n.get("bid_reference_no") or ""),
            "funding_agency": f"World Bank ({ntype})" if ntype else "World Bank",
            "brief_description": desc[:1800] or None,
            "date_posted": _wb_date(n.get("noticedate")),
            "call_submission_deadline": _parse_iso_date(str(n.get("submission_date") or "")[:10]),
            "call_geographic_scope": [ctry] if ctry else None,
            "_source_origin": name,
        })
    return _dedup_by_link_or_title(out)


def _scan_ocds(name: str, url: str, *, notice_base: str, geo: str
               ) -> list[dict[str, Any]]:
    """Generic OCDS release-package reader — UK Find a Tender + Contracts Finder.
    Both publish under the Open Government Licence v3 (declared in the package's
    `license` field). Keeps active/planned tenders; skips awards/cancelled."""
    out: list[dict[str, Any]] = []
    try:
        r = _http.get(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        releases = (r.json() or {}).get("releases") or []
    except Exception as exc:
        log.warning("OCDS %s failed: %s", name, exc)
        return []
    for rel in releases[:80]:
        tags = {str(t).lower() for t in (rel.get("tag") or [])}
        if tags & {"award", "awardupdate", "awardcancellation", "contract"}:
            continue  # awarded / contract stage, not an open opportunity
        t = rel.get("tender") or {}
        if (t.get("status") or "").lower() in (
                "complete", "cancelled", "unsuccessful", "withdrawn"):
            continue
        title = _clean(t.get("title") or "")
        if not title:
            continue
        rid = _clean(rel.get("id") or rel.get("ocid") or "")
        tp = t.get("tenderPeriod") or {}
        out.append({
            "opportunity_title": title,
            "opportunity_link": f"{notice_base}{rid}" if rid else notice_base,
            "opportunity_id": _clean(rel.get("ocid") or ""),
            "funding_agency": _clean((rel.get("buyer") or {}).get("name") or "") or name,
            "brief_description": _clean(t.get("description") or "")[:1800] or None,
            "date_posted": _parse_iso_date(str(rel.get("date") or "")[:10]),
            "call_submission_deadline": _parse_iso_date(str(tp.get("endDate") or "")[:10]),
            "call_geographic_scope": [geo],
            "_source_origin": name,
        })
    return _dedup_by_link_or_title(out)


def _scan_ted(name: str, url: str) -> list[dict[str, Any]]:
    """TED — EU public procurement notices (api.ted.europa.eu/v3). Free; TED data
    is reusable (EU PSI). Scoped to health CPV (85*) + ACTIVE notices. Titles are
    multilingual dicts — prefer English."""
    out: list[dict[str, Any]] = []
    # Recent + newest-first (scope=ACTIVE alone returned stale 2016 notices).
    since = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")
    body = {"query": f"classification-cpv=85* AND publication-date>={since} "
                     "SORT BY publication-date DESC",
            "fields": ["ND", "TI", "PD", "links", "CY", "publication-number",
                       "deadline-receipt-tender-date-lot"],
            "limit": 50}
    try:
        r = _http.post(
            url, json=body,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        notices = (r.json() or {}).get("notices") or []
    except Exception as exc:
        log.warning("TED failed: %s", exc)
        return []
    for n in notices:
        ti = n.get("TI")
        if isinstance(ti, dict):
            title = _clean(ti.get("eng") or next(iter(ti.values()), ""))
        else:
            title = _clean(ti or "")
        if not title:
            continue
        num = _clean(n.get("publication-number") or n.get("ND") or "")
        link = f"https://ted.europa.eu/en/notice/{num}" if num else None
        dl = n.get("deadline-receipt-tender-date-lot")
        if isinstance(dl, list) and dl:
            deadline = _parse_iso_date(str(dl[0])[:10])
        elif isinstance(dl, str):
            deadline = _parse_iso_date(dl[:10])
        else:
            deadline = None
        cy = n.get("CY")
        out.append({
            "opportunity_title": title,
            "opportunity_link": link,
            "opportunity_id": num,
            "funding_agency": "EU public procurement (TED)",
            "brief_description": None,
            "date_posted": _parse_iso_date(str(n.get("PD") or "")[:10]),
            "call_submission_deadline": deadline,
            "call_geographic_scope": cy if isinstance(cy, list) else ([cy] if cy else None),
            "_source_origin": name,
        })
    return _dedup_by_link_or_title(out)


# ── UNGM notice-detail enrichment ────────────────────────────────────────────
# The public notice page is SERVER-rendered, so the Documents / Contacts / UNSPSC
# tabs are all in the HTML (no AJAX). We download + read the primary RFP + General-
# Terms PDFs to give the extractor the REAL call text, and capture contacts + UNSPSC
# codes for donor-intel enrichment. Best-effort; never raises into a scan.
_UNGM_DOC_RE = re.compile(
    r'href="(/Public/Notice/DownloadDocument\?noticeId=\d+&(?:amp;)?documentId=\d+)"'
    r'[^>]*>(.*?)</a>', re.S | re.I)
_UNGM_PRIMARY_HINT = re.compile(
    r'\b(rfp|rfq|rfi|itb|eoi|request\s+for|tender|proposal|solicitation|bid|notice)\b', re.I)
_UNGM_TERMS_HINT = re.compile(r'\b(general\s+terms|terms\s+and\s+conditions|conditions)\b', re.I)
# Enrichment is a per-notice fetch (+ PDF downloads), so bound it to the newest N.
_UNGM_DETAIL_CAP = 40


def _ungm_documents(html_text: str) -> list[dict[str, str]]:
    out, seen = [], set()
    for href, raw in _UNGM_DOC_RE.findall(html_text):
        name = _clean(re.sub(r"<[^>]+>", "", html.unescape(raw)))
        link = "https://www.ungm.org" + html.unescape(href)
        if name and link not in seen:
            seen.add(link)
            out.append({"name": name, "url": link})
    return out[:25]


def _ungm_contacts(soup: BeautifulSoup) -> list[dict[str, str]]:
    out = []
    for item in soup.select("#contactDetails .ungm-list-item"):
        c: dict[str, str] = {}
        for row in item.select(".row"):
            lab = row.select_one(".label")
            val = row.select_one(".value")
            if not (lab and val):
                continue
            label = _clean(lab.get_text(" ", strip=True)).rstrip(":").lower()
            value = _clean(val.get_text(" ", strip=True))
            if not value:
                continue
            if "email" in label:
                c["email"] = value
            elif "first name" in label:
                c["first_name"] = value
            elif "surname" in label or "last name" in label:
                c["surname"] = value
            elif "phone" in label or "telephone" in label:
                c["phone"] = value
            elif "title" in label or "role" in label or "function" in label:
                c["role"] = value
        full = _clean(" ".join(x for x in (c.get("first_name"), c.get("surname")) if x))
        if not full:
            t = item.select_one(".title")
            if t:
                full = _clean(t.get_text(" ", strip=True)).split(" - ")[0]
        if full or c.get("email"):
            out.append({"name": full, "email": c.get("email", ""),
                        "phone": c.get("phone", ""), "role": c.get("role", "")})
    return out[:50]


def _ungm_unspsc(soup: BeautifulSoup) -> list[dict[str, str]]:
    out, seen = [], set()
    for node in soup.select("div.unspscNode > span.nodeName"):
        parts = [_clean(s.get_text(strip=True)) for s in node.select("span.floatLeft")]
        parts = [p for p in parts if p and p != "-"]
        if len(parts) >= 2 and parts[0].isdigit() and parts[0] not in seen:
            seen.add(parts[0])
            out.append({"code": parts[0], "name": parts[1]})
    return out[:40]


def _enrich_ungm_notice(nid: str, cand: dict[str, Any]) -> None:
    """Pull per-notice detail: scan the primary RFP + General-Terms PDFs into the call
    text, and capture documents / contacts / UNSPSC codes. Best-effort; never raises."""
    try:
        r = _http.get(f"https://www.ungm.org/Public/Notice/{nid}",
                      headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        page = r.text
    except Exception as exc:
        log.debug("ungm detail %s failed: %s", nid, exc)
        return
    try:
        soup = BeautifulSoup(page, "html.parser")
        docs = _ungm_documents(page)
        if docs:
            cand["documents"] = docs
        contacts = _ungm_contacts(soup)
        if contacts:
            cand["_contacts"] = contacts          # → donor_contacts (writer wired next)
        unspsc = _ungm_unspsc(soup)
        if unspsc:
            cand["_unspsc"] = unspsc
        # Scan the primary RFP + a General-Terms PDF (bounded) into the call text so the
        # downstream LLM/regex extractor reads the REAL call, not just the listing title.
        pdfs = [d for d in docs if d["name"].lower().endswith(".pdf")]
        primary = [d for d in pdfs if _UNGM_PRIMARY_HINT.search(d["name"])][:1]
        terms = [d for d in pdfs if _UNGM_TERMS_HINT.search(d["name"]) and d not in primary][:1]
        pick = (primary + terms) or pdfs[:1]
        texts = []
        # UNGM's DownloadDocument requires a Referer (the notice page) or it returns 0 bytes.
        _dl_hdrs = {"User-Agent": USER_AGENT,
                    "Referer": f"https://www.ungm.org/Public/Notice/{nid}"}
        for d in pick[:2]:
            try:
                dr = _http.get(d["url"], headers=_dl_hdrs, timeout=HTTP_TIMEOUT)
                if getattr(dr, "ok", False) and len(dr.content) <= ENRICH_PDF_MAX_BYTES:
                    t = _extract_pdf_text(dr.content)
                    if t:
                        texts.append(f"[{d['name']}] {t}")
            except Exception:
                continue
        if texts:
            extra = _clean(" ".join(texts))
            cand["_page_text"] = _clean(((cand.get("_page_text") or "") + " " + extra))[:12000]
            if not cand.get("brief_description"):
                cand["brief_description"] = extra[:1800]
    except Exception as exc:
        log.debug("ungm enrich parse %s failed: %s", nid, exc)


def _scan_ungm(name: str, url: str) -> list[dict[str, Any]]:
    """UNGM (UN Global Marketplace) — the official developer API is gated, but the
    public site's own search endpoint POST /Public/Notice/Search is keyless and
    returns notice rows as HTML (data-noticeid + title/deadline/agency/type/country).
    Reads only the FREE public listing (UNGM Pro features are paid + unused)."""
    out: list[dict[str, Any]] = []
    enriched = 0                                # per-notice detail fetches are bounded
    endpoint = "https://www.ungm.org/Public/Notice/Search"
    hdrs = {"User-Agent": USER_AGENT, "Accept": "text/html",
            "Content-Type": "application/json"}
    for page in range(2):                       # 2 × 50 = up to 100 newest notices
        body = {"PageIndex": page, "PageSize": 50, "Title": "", "Description": "",
                "Reference": "", "PublishedFrom": "", "PublishedTo": "",
                "DeadlineFrom": "", "DeadlineTo": "", "Countries": [],
                "Agencies": [], "NoticeTypes": [], "UNSPSCs": [],
                "SortField": "DatePublished", "SortAscending": False}
        try:
            r = _http.post(endpoint, data=json.dumps(body), headers=hdrs,
                              timeout=HTTP_TIMEOUT)
            r.raise_for_status()
        except Exception as exc:
            log.warning("UNGM page=%s failed: %s", page, exc)
            break
        rows = BeautifulSoup(r.text, "html.parser").select("div.dataRow[data-noticeid]")
        if not rows:
            break
        for row in rows:
            nid = row.get("data-noticeid")
            tcell = row.select_one("div.resultTitle")
            title = _clean(tcell.get_text(" ", strip=True)) if tcell else ""
            title = title.replace("Open in a new window", "").strip()
            if not nid or not title:
                continue
            deadline = None
            dcell = row.select_one("div.resultInfo1.deadline")
            if dcell:
                m = re.search(r"\d{1,2}-[A-Za-z]{3}-\d{4}",
                              dcell.get_text(" ", strip=True))
                if m:
                    deadline = _wb_date(m.group(0))
            agency = ""
            ag = row.select_one("div.resultAgency")
            if ag:
                agency = _clean(ag.get_text(strip=True))
            ref = ""
            for c in row.select("div.resultInfo1"):
                if "deadline" not in (c.get("class") or []):
                    ref = _clean(c.get_text(strip=True))
                    break
            cells = [_clean(c.get_text(" ", strip=True)) for c in row.select("div.tableCell")]
            country = cells[-1] if cells else ""
            cand = {
                "opportunity_title": title,
                "opportunity_link": f"https://www.ungm.org/Public/Notice/{nid}",
                "opportunity_id": ref or None,
                "funding_agency": f"UN — {agency}" if agency else "UN (UNGM)",
                "brief_description": None,
                "call_submission_deadline": deadline,
                "call_geographic_scope": [country] if country else None,
                "_source_origin": name,
            }
            # Enrich the newest N notices with their tab detail (documents → scanned
            # PDF text, contacts, UNSPSC). Bounded so a scan isn't N detail fetches.
            if enriched < _UNGM_DETAIL_CAP:
                _enrich_ungm_notice(nid, cand)
                enriched += 1
            out.append(cand)
    return _dedup_by_link_or_title(out)


# ---------------------------------------------------------------------------
# HTML — generic best-effort anchor extraction
# ---------------------------------------------------------------------------
def _scan_unops(name: str, url: str) -> list[dict[str, Any]]:
    """UNOPS GrantPlus — the SPA is JS/Google-login, but it lists open calls via a
    KEYLESS external JSON API (/api/external/funding-opportunity). Returns records
    with name/referenceNumber/description/geographicAreas/funding/deadline/stage."""
    out: list[dict[str, Any]] = []
    api = ("https://grantplus.unops.org/api/external/funding-opportunity"
           "?pageIndex=0&pageSize=100&ascending=true")
    try:
        r = _http.get(api, headers={"User-Agent": USER_AGENT,
                                       "Accept": "application/json"},
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        recs = (r.json() or {}).get("records") or []
    except Exception as exc:
        log.warning("UNOPS GrantPlus failed: %s", exc)
        return []
    for n in recs:
        if (n.get("stage") or "open").lower() != "open":   # keep only open calls
            continue
        title = _clean(n.get("name") or "")
        if not title:
            continue
        oid = n.get("id")
        geos = [g.get("name") for g in (n.get("geographicAreas") or [])
                if g.get("name")]
        out.append({
            "opportunity_title": title,
            "opportunity_link": (
                f"https://grantplus.unops.org/funding-opportunity/{oid}"
                if oid else "https://grantplus.unops.org/funding-opportunity"),
            "opportunity_id": _clean(n.get("referenceNumber") or ""),
            "funding_agency": "UNOPS (GrantPlus)",
            "brief_description": _clean(n.get("description") or "")[:1800] or None,
            "date_posted": _parse_iso_date(str(n.get("postingDate") or "")[:10]),
            "call_submission_deadline": _parse_iso_date(
                str(n.get("submissionDueDate") or "")[:10]),
            "call_award_value": n.get("fundingAvailable"),
            "currency": (n.get("currency") or {}).get("code"),
            "call_geographic_scope": geos or None,
            "_source_origin": name,
        })
    return _dedup_by_link_or_title(out)


def _scan_chinnova(name: str, url: str) -> list[dict[str, Any]]:
    """CHINNOVA (AAU) grants portal — static HTML, but each call is a card whose
    LINK text is a generic 'Click to View Details' CTA (the title lives in the card
    heading), so the generic anchor extractor misses it. Parse the gc-card-body
    cards: title + 'Deadline: DD Month, YYYY' + grant-details.php?id=N."""
    listing = "https://grants.chinnova.aau.org/pages/all-grants.php"
    try:
        r = _http.get(listing, headers={"User-Agent": USER_AGENT,
                                           "Accept": "text/html"},
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        log.warning("CHINNOVA failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for card in soup.select("div.gc-card-body"):
        a = card.select_one("a[href*='grant-details.php']")
        if not a:
            continue
        head = card.select_one("h1, h2, h3, h4, h5")
        ctext = card.get_text(" ", strip=True)
        title = _clean(head.get_text(" ", strip=True)) if head else ""
        if not title:
            t = re.sub(r"^(EXPIRED|OPEN|CLOSED)\s*", "", ctext, flags=re.I)
            title = _clean(re.split(r"Deadline", t, 1)[0])
        if not title:
            continue
        deadline = None
        m = re.search(r"Deadline:\s*([0-9]{1,2}\s+[A-Za-z]+,?\s+20[0-9]{2})", ctext)
        if m:
            try:
                deadline = datetime.strptime(
                    m.group(1).replace(",", ""), "%d %B %Y").date()
            except ValueError:
                deadline = _parse_iso_date(m.group(1))
        out.append({
            "opportunity_title": title[:300],
            # hrefs are root-relative ("pages/grant-details.php?id=N").
            "opportunity_link": urljoin("https://grants.chinnova.aau.org/",
                                        a["href"]),
            "funding_agency": "CHINNOVA / Association of African Universities",
            "brief_description": None,
            "call_submission_deadline": deadline,
            "_source_origin": name,
        })
    return _dedup_by_link_or_title(out)


_GCC_CA_DATE_RE = re.compile(r"\bby\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4})", re.I)


def _scan_grandchallenges_ca(name: str, url: str) -> list[dict[str, Any]]:
    """Grand Challenges Canada — the /apply-for-funding/ page lists each open call
    as a CARD: a heading ("Nexa Funding Opportunity: …"), a health blurb, an
    "Apply by <date>" line, and a generic "View full details" button. The generic
    anchor crawler keyed off the button text (title "View full details") and the
    page's French meta description, so the call read as off-theme. Parse the card
    instead: heading → title, card body → description, "by <date>" → deadline."""
    try:
        r = _http.get(url, headers={"User-Agent": USER_AGENT,
                                       "Accept": "text/html"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as exc:
        log.warning("Grand Challenges Canada failed: %s", exc)
        return []
    base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        full = urljoin(base, a["href"])
        path = urlsplit(full).path.lower()
        # individual call detail pages; skip the listing + archive/past links.
        # (prefix match so the singular "/funding-opportunity-<slug>" is caught).
        if not re.search(r"/funding-opportunit|/rfp-", path):
            continue
        if any(x in path for x in ("archive", "/past", "apply-for-funding")):
            continue
        # The card's title is the heading immediately BEFORE the button in document
        # order; the card body is the smallest ancestor containing both (climbing
        # to "the first heading" wrongly grabbed a neighbouring card's heading).
        head = a.find_previous(["h2", "h3", "h4", "h5"])
        if not head:
            continue
        title = _clean(head.get_text(" ", strip=True))
        if not title or full in seen:
            continue
        seen.add(full)
        card = a
        while card is not None and head not in list(card.descendants):
            card = card.parent
        ctext = _clean((card or a.parent).get_text(" ", strip=True))
        deadline = None
        m = _GCC_CA_DATE_RE.search(ctext)
        if m:
            try:
                deadline = datetime.strptime(
                    m.group(1).replace(",", "").replace(".", ""), "%B %d %Y").date()
            except ValueError:
                deadline = _parse_iso_date(m.group(1))
        out.append({
            "opportunity_title": title[:300],
            "opportunity_link": full,
            "funding_agency": "Grand Challenges Canada",
            "brief_description": ctext[:1800] or None,
            "call_submission_deadline": deadline,
            "_source_origin": name,
        })
    return _dedup_by_link_or_title(out)


# ---------------------------------------------------------------------------
# Coefficient Giving (ex-Open Philanthropy)
# ---------------------------------------------------------------------------
# Each fund page (e.g. /funds/global-health-wellbeing-opportunities/) carries a
# "Research & Updates" section of cards. Every card is tagged with a post-type
# label in `<p class="text-sans-sm …">…</p>` — Request for Proposals, Blog,
# Cause Investigation, News, … — followed (within the same card) by the title
# heading and the detail link. We keep ONLY the RFP-type cards and reject the
# rest. The label→title→link association is parsed off the raw HTML within a
# bounded window after each label (verified reliable: each label's window holds
# its own title + own detail link first).
_CG_HOST = "coefficientgiving.org"
# Post-type labels that ARE funding solicitations (everything else is editorial).
_CG_RFP_LABEL_RE = re.compile(
    r"request for (?:proposals|applications|expressions?|information)"
    r"|call for (?:proposals|applications|expressions?|concept)"
    r"|funding opportunit|grant opportunit|notice of funding"
    r"|open (?:call|solicitation)|\brfp\b|\brfa\b|\beoi\b",
    re.I,
)


def _cg_page(url: str, headers: dict[str, str]) -> tuple[str, str]:
    """(title, readable-text) of a Coefficient Giving page. ('','') on failure.
    The text feeds downstream deadline/closure/value/eligibility extraction, so a
    sub-page's "This RFP closed on …" notice reaches the gates."""
    try:
        r = _http.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        title = _clean(h1.get_text(" ", strip=True)) if h1 else ""
        for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            t.decompose()
        main = soup.find("main") or soup.find("article") or soup.body or soup
        return title, _clean(main.get_text(" ", strip=True))
    except Exception as exc:  # pragma: no cover — best-effort enrichment
        log.debug("Coefficient Giving page fetch failed (%s): %s", url, exc)
        return "", ""


def _cg_page_text(url: str, headers: dict[str, str]) -> str:
    """Readable text of a Coefficient Giving detail page (back-compat shim)."""
    return _cg_page(url, headers)[1]


# RFP sub-page tabs that live UNDER a fund (e.g. /funds/<slug>/request-for-
# proposals-biosecurity/). The real call + its "closed on …" notice live here,
# not on the fund landing page — so we must drill in.
_CG_SUBPAGE_RE = re.compile(
    r"(request-for-proposals|requests?-for-applications|call-for-proposals|"
    r"call-for-applications|funding-opportunit|grant-opportunit|/rfp|/rfa|/eoi)",
    re.I,
)


def _scan_coefficient_giving(name: str, url: str) -> list[dict[str, Any]]:
    """Coefficient Giving /funds/. Crawl the fund index → each fund page → keep
    only the RFP-type "Research & Updates" cards, rejecting Blog / Cause
    Investigation / News. If `url` is a single fund page, crawl just that one."""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    try:
        r = _http.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except Exception as exc:
        log.warning("Coefficient Giving fetch failed (%s): %s", url, exc)
        return []

    path0 = urlsplit(url).path.lower().rstrip("/")
    # Index page (/funds) → every fund page; a specific fund page → just itself.
    if path0 in ("/funds", ""):
        idx = BeautifulSoup(r.text, "html.parser")
        fund_urls = sorted({
            urljoin(base, a["href"]) for a in idx.find_all("a", href=True)
            if re.match(r"^/funds/[a-z0-9-]+/?$",
                        urlsplit(urljoin(base, a["href"])).path)
        })
    else:
        fund_urls = [url]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for furl in fund_urls:
        try:
            fhtml = r.text if furl == url else _http.get(
                furl, headers=headers, timeout=HTTP_TIMEOUT).text
        except Exception as exc:
            log.warning("Coefficient Giving fund page failed (%s): %s", furl, exc)
            continue
        fsoup = BeautifulSoup(fhtml, "html.parser")
        fh1 = fsoup.find("h1")
        fund_area = _clean(fh1.get_text(" ", strip=True)) if fh1 else None
        fund_path = urlsplit(furl).path.rstrip("/").lower()

        # (A) RFP SUB-PAGE TABS under this fund — the real call (and its
        # "closed on …" notice) lives here, not on the fund landing page. Drill in
        # so the deadline/closure gate sees the actual RFP text.
        for a in fsoup.find_all("a", href=True):
            sub = urljoin(base, a["href"])
            sp = urlsplit(sub).path.rstrip("/").lower()
            if (_CG_HOST in sub and sp.startswith(fund_path + "/")
                    and _CG_SUBPAGE_RE.search(sp) and sub not in seen):
                seen.add(sub)
                s_title, s_text = _cg_page(sub, headers)
                if not s_text:
                    continue
                title = s_title or (f"{fund_area} — Request for Proposals"
                                    if fund_area else "Request for Proposals")
                out.append({
                    "opportunity_title": title[:300],
                    "opportunity_link": sub,
                    "funding_agency": "Coefficient Giving",
                    "brief_description": (s_text[:1800] or None),
                    "_page_text": (s_text[:20000] or None),
                    "_fund_area": fund_area,
                    "_source_origin": name,
                })

        # NOTE: a Coefficient Giving fund PARENT page (/funds/<slug>/) only
        # describes what they fund — it is NOT an open call. A real RFP exists
        # ONLY as a tabbed sub-page (captured in (A)). So a fund with no RFP tab
        # yields no candidate, and the parent page is never emitted. (The old
        # "Research & Updates" card path was dropped 2026-06-26 — it surfaced
        # parent/research pages as false RFPs.)
    return _dedup_by_link_or_title(out)


def _ts_to_date(ts: Any) -> date | None:
    """Coerce a UNIX epoch (seconds) to a date. None on any failure."""
    try:
        return datetime.fromtimestamp(int(ts)).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# Cap on GC detail-page fetches per scan — each is a throttled request; a weekly
# job can afford the whole listing, but bound it so a runaway listing can't stall.
try:
    _GC_DETAIL_MAX = int(os.environ.get("GC_DETAIL_MAX", "60") or 60)
except ValueError:
    _GC_DETAIL_MAX = 60


def _gc_challenge_text(detail_url: str) -> str | None:
    """Full challenge body for a Grand Challenges /challenge/ page.

    The LISTING JSON only carries a truncated summary (opportunity_description[:1800]),
    which omits the "Award Structure and Funding Level" tier table — so amounts published
    ONLY on the detail page (e.g. 'up to US$300,000 … US$800,000') never reached the
    extractor. The detail page embeds the FULL body in __NEXT_DATA__ at
    props.pageProps.challenge.opportunityDescription (clean prose, no nav chrome), so a
    plain GET + JSON read recovers it — no Playwright needed. Returns cleaned text, or
    None on any failure (caller keeps the listing summary)."""
    try:
        r = _http.get(detail_url, headers={"User-Agent": USER_AGENT,
                                           "Accept": "text/html"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return None
        chal = (((json.loads(m.group(1)).get("props") or {}).get("pageProps") or {})
                .get("challenge") or {})
        body = chal.get("opportunityDescription") or ""
        if not body:
            return None
        text = re.sub(r"<[^>]+>", " ", html.unescape(body))
        return _clean(text)[:20000] or None
    except Exception as exc:
        log.debug("GC detail fetch failed %s: %s", detail_url, exc)
        return None


def _scan_grandchallenges(name: str, url: str) -> list[dict[str, Any]]:
    """Grand Challenges family (grandchallenges.org + gcgh.grandchallenges.org).
    The /grant-opportunities listing is a Next.js app that server-side-embeds the
    current calls in <script id="__NEXT_DATA__"> at
    props.pageProps.initialData.listing.data — each item carries the detail URL,
    title, HTML description, funder (initiative_title), launch + closing dates
    (UNIX seconds) and a coming_soon flag. Parsing that JSON directly is more
    reliable than the generic anchor crawler (the cards render client-side) and
    yields the real submission deadline. No Playwright needed."""
    try:
        r = _http.get(url, headers={"User-Agent": USER_AGENT,
                                       "Accept": "text/html"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            log.warning("Grand Challenges %s: no __NEXT_DATA__ block", url)
            return []
        data = ((json.loads(m.group(1)).get("props") or {}).get("pageProps") or {}
                ).get("initialData", {}).get("listing", {}).get("data") or []
    except Exception as exc:
        log.warning("Grand Challenges %s failed: %s", url, exc)
        return []
    base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    # The site JSON sometimes carries ABSOLUTE www./bare marketing-host links for
    # /challenge/ paths (which serve only generic boilerplate) — force the working
    # gcgh. host so the stored link works AND the crawl reads the REAL challenge body.
    from core.source_resolver import canonical_grandchallenges
    out: list[dict[str, Any]] = []
    _detail_fetches = 0
    for it in data:
        if it.get("hidden"):
            continue
        title = _clean(it.get("main_title") or "")
        rel = it.get("url") or ""
        if not title or not rel:
            continue
        link = canonical_grandchallenges(urljoin(base + "/", rel.lstrip("/")))
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.get("opportunity_description") or ""))
        cand = {
            "opportunity_title": title[:300],
            "opportunity_link": link,
            "funding_agency": _clean(it.get("initiative_title") or "")
                              or _funder_from_source_name(name),
            "brief_description": _clean(desc)[:1800] or None,
            "date_posted": _ts_to_date(it.get("date")),
            "call_submission_deadline": _ts_to_date(it.get("date_end")),
            "_source_origin": f"{name}{' (coming soon)' if it.get('coming_soon') else ''}",
        }
        # Pull the FULL challenge body (with the Award Structure / tier amounts) from the
        # detail page so regex + LLM can read the figures the listing summary omits. Skip
        # coming-soon calls (no body yet) and stay within the per-scan fetch cap.
        if (not it.get("coming_soon") and link
                and _detail_fetches < _GC_DETAIL_MAX):
            _detail_fetches += 1
            full = _gc_challenge_text(link)
            if full:
                cand["_page_text"] = full
        out.append(cand)
    return _dedup_by_link_or_title(out)


def _scan_packard(name: str, url: str) -> list[dict[str, Any]]:
    """Packard Foundation — the /grantees/funding-opportunties/ page renders its
    cards client-side, but WordPress exposes the same content via the REST custom
    post type `funding-opportunity`. Keyless GET; clean title/link/date/excerpt."""
    api = "https://www.packard.org/wp-json/wp/v2/funding-opportunity?per_page=30"
    try:
        r = _http.get(api, headers={"User-Agent": USER_AGENT,
                                       "Accept": "application/json"}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        items = r.json() or []
    except Exception as exc:
        log.warning("Packard WP REST failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for it in (items if isinstance(items, list) else []):
        title = _clean(html.unescape((it.get("title") or {}).get("rendered") or ""))
        link = it.get("link") or ""
        if not title or not link:
            continue
        excerpt = re.sub(r"<[^>]+>", " ",
                         html.unescape((it.get("excerpt") or {}).get("rendered") or ""))
        out.append({
            "opportunity_title": title[:300],
            "opportunity_link": link,
            "funding_agency": "Packard Foundation",
            "brief_description": _clean(excerpt)[:1800] or None,
            "date_posted": _parse_iso_date(str(it.get("date") or "")[:10]),
            "call_submission_deadline": None,
            "_source_origin": name,
        })
    return _dedup_by_link_or_title(out)


def _scan_rvo(name: str, url: str) -> list[dict[str, Any]]:
    """Netherlands Enterprise Agency (RVO) — english.rvo.nl subsidy search JSON
    API (keyless GET). Returns searchResults[] with title / url / summary / intro /
    subsidyStatusName. Keep only 'Open for application'; paginate via pager.page.
    Replaces the human /subsidies-programmes page which the generic crawler can't
    read. (The MFA's development subsidies are administered through RVO.)"""
    out: list[dict[str, Any]] = []
    api = "https://english.rvo.nl/api/rvo/v1/search-subsidies"
    page = 0
    while page < 4:
        try:
            r = _http.get(api, params=({"page": page} if page else None),
                             headers={"User-Agent": USER_AGENT,
                                      "Accept": "application/json"},
                             timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            j = r.json() or {}
        except Exception as exc:
            log.warning("RVO page=%s failed: %s", page, exc)
            break
        results = j.get("searchResults") or []
        if not results:
            break
        for it in results:
            if (it.get("subsidyStatusName") or "").strip().lower() != "open for application":
                continue
            title = _clean(it.get("title") or "")
            rel = it.get("url") or ""
            if not title or not rel:
                continue
            summary = re.sub(r"<[^>]+>", " ",
                             html.unescape(it.get("summary") or it.get("intro") or ""))
            out.append({
                "opportunity_title": title[:300],
                "opportunity_link": urljoin("https://english.rvo.nl/", rel.lstrip("/")),
                "funding_agency": "Netherlands Enterprise Agency (RVO)",
                "brief_description": _clean(summary)[:1800] or None,
                "call_submission_deadline": None,
                "_source_origin": name,
            })
        pager = j.get("pager") or {}
        nxt = pager.get("nextPage")
        if nxt is None or nxt <= page:
            break
        page = nxt
    return _dedup_by_link_or_title(out)


# Strong opportunity-path URLs — a link whose PATH clearly points at a specific
# call (e.g. /apply/rfp, /calls-for-proposals/<slug>, /grants/<slug>) is accepted
# even with a SHORT anchor text ("RFP", "Request for Proposals", "Apply"), which
# the generic 25-char title floor would otherwise drop. Index roots (/grants,
# /funding bare) are NOT strong — they need the granty+length heuristic.
_STRONG_OPP_PATH = re.compile(
    r"/(?:rfp|rfps|cfp|cfps|eoi|eois|rfi|loi"
    r"|call[s]?-for-(?:proposal|application|tender|project)"
    r"|request-for-(?:proposal|application|expression)"
    r"|funding-opportunit(?:y|ies)?|grant-opportunit(?:y|ies)?|request-for-proposals"
    r"|funding-opportunity|tender|procurement-notice)(?:/|s/|-|$)", re.I)

# Anchors that are navigation chrome, NOT individual solicitations. These were
# slipping through the _STRONG_OPP_PATH bypass (e.g. the "English" language
# toggle and "2"/"3"/"4" pagination links on Unitaid's /calls-for-proposals/).
_LANG_NAMES = {
    "english", "français", "francais", "español", "espanol", "deutsch",
    "português", "portugues", "italiano", "nederlands", "polski", "العربية",
    "中文", "русский", "日本語", "한국어", "हिन्दी", "kiswahili", "عربي",
}
_NAV_TEXT = {
    "next", "previous", "prev", "first", "last", "more", "read more",
    "load more", "show more", "view all", "see all", "view more", "home",
    "back", "apply", "apply now", "menu", "search", "skip to main content",
    "subscribe", "newsletter", "contact", "contact us", "about", "about us",
    "login", "log in", "sign in", "register", "donate", "donate now",
}
_PAGE_NUM_RE = re.compile(r"^(page\s*)?\d{1,3}$", re.I)
_PAGINATION_QS_RE = re.compile(r"[?&](paged?|page|p|pg)=\d+", re.I)


def _norm_loc(u: str) -> str:
    s = urlsplit(u)
    return (s.netloc.lower() + s.path.rstrip("/").lower())


def _is_junk_anchor(text: str, full: str, listing_url: str) -> bool:
    """True for nav chrome: language toggles, pagination, common nav verbs, and
    links that just point back at the listing page itself (lang/pagination)."""
    t = text.strip().lower()
    if t in _LANG_NAMES or t in _NAV_TEXT:
        return True
    if _PAGE_NUM_RE.match(t):
        return True
    if _PAGINATION_QS_RE.search(full):
        return True
    # self-referential: same netloc+path as the listing (differs only by
    # query/fragment) — that's a language/pagination/anchor link, not a call.
    if _norm_loc(full) == _norm_loc(listing_url):
        return True
    return False


def _extract_candidates_from_html(name: str, url: str, html_text: str, *,
                                  extract_only: bool = False,
                                  fresh_uids: set[str] | None = None) -> list[dict[str, Any]]:
    """Pure anchor-extraction logic — shared by `_scan_html` (requests) and
    `_scan_html_js` (Playwright-rendered). Takes pre-fetched HTML text;
    returns enriched candidate dicts.

    Incremental extraction: when `extract_only and fresh_uids`, any candidate
    whose opportunity_link is already fresh in the global store is dropped here —
    BEFORE `_enrich_candidate` (the deep-read + LLM step) runs — and excluded from
    the returned list. The count is recorded via `_record_skipped` for run()."""
    soup = BeautifulSoup(html_text, "html.parser")
    funder = _funder_from_source_name(name)
    base_host = urlsplit(url).netloc.lower()

    candidates: dict[str, dict[str, Any]] = {}
    for a in soup.find_all("a", href=True):
        text = _clean(a.get_text(" ", strip=True))
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(url, href)
        # A specific opportunity-path link bypasses the generic title-length floor.
        strong = bool(_STRONG_OPP_PATH.search(urlsplit(full).path))
        if not text or len(text) > 220 or (len(text) < 25 and not strong):
            continue
        # Drop navigation chrome (language toggles, pagination, nav verbs,
        # self-links) — applies even to strong-path links so "English"/"2"/"3"
        # pagination on a /calls-for-proposals/ page no longer slip through.
        if _is_junk_anchor(text, full, url):
            continue
        # Stay within the same domain to avoid scraping nav links to other sites.
        if urlsplit(full).netloc.lower() != base_host:
            continue
        # Reject obvious blog / guidance / FAQ pages by URL path …
        if _BLOG_URL_RE.search(urlsplit(full).path):
            continue
        # … reject search-result / filter URLs (Rockefeller's
        # ?post_type=grant&filter_regions[0]=…, ?keyword=, /search/?)
        # which list grants but aren't grant detail pages themselves.
        if _SEARCH_PAGE_URL_RE.search(full):
            continue
        # … reject URLs whose path embeds a past year, e.g.
        # /year/2022/alliance-rfp.pdf — short-circuits the whole
        # download+parse cycle since the deadline will never be future.
        url_yr = _detect_url_year(full)
        if url_yr and url_yr < date.today().year:
            continue
        # … and by title fragment ("what we do and don't fund", etc.)
        if _BLOG_TITLE_RE.search(text):
            continue
        # Heuristic: title OR url must mention something granty (strong
        # opportunity-path links already qualify).
        if not (strong or _GRANTY_RE.search(text) or _GRANTY_RE.search(href)):
            continue
        if full in candidates:
            # Keep the longest title (typically the most descriptive)
            if len(text) > len(candidates[full]["opportunity_title"]):
                candidates[full]["opportunity_title"] = text
            continue
        candidates[full] = {
            "opportunity_title": text,
            "opportunity_link": full,
            "funding_agency": funder,
            "brief_description": None,
            "date_posted": None,
            "call_submission_deadline": None,
            "_source_origin": f"{name} (HTML)",
        }
        if len(candidates) >= 40:  # cap noise
            break

    all_cands = list(candidates.values())

    # Incremental extraction — drop candidates already fresh in the global store
    # BEFORE the expensive deep-read + LLM enrichment below, and exclude them from
    # the returned list (re-extracting a still-fresh opportunity is the waste this
    # removes). Only active in extract_only mode with a fresh-uid set; otherwise
    # every candidate flows through unchanged (full scans / new tenants).
    if extract_only and fresh_uids:
        kept: list[dict[str, Any]] = []
        skipped = 0
        for c in all_cands:
            link = c.get("opportunity_link") or ""
            if link and make_uid(link) in fresh_uids:
                skipped += 1
                continue
            kept.append(c)
        if skipped:
            _record_skipped(skipped)
            log.info("incremental: skipped %d already-fresh candidate(s) for %s",
                     skipped, name)
        all_cands = kept

    # Detail-page enrichment — fetch each candidate's URL and try to fill
    # missing deadline / description / eligibility. This is what allows the
    # eligibility gate downstream to reject US-only or past-deadline RFPs
    # that the listing-page title alone didn't reveal.
    results = all_cands[:ENRICH_MAX_PAGES]
    for i, c in enumerate(results, 1):
        _enrich_candidate(c)
        if i >= ENRICH_MAX_PAGES:
            log.info("enrich cap reached at %d candidates", ENRICH_MAX_PAGES)
            break
    return results + all_cands[ENRICH_MAX_PAGES:]


def expand_listing(url: str, source_name: str = "listing") -> list[dict[str, Any]]:
    """Walk a LISTING / aggregator index page and return its child opportunity
    candidates (same-domain detail links, enriched) — so the actual calls get
    evaluated instead of the index being dropped. Reuses the standard anchor
    extraction; falls back to the Playwright render for JS-built indexes (e.g.
    React aggregators) when available. Best-effort: [] on any failure.

    Each child is tagged `_expanded_from` so provenance is traceable. Aggregator
    children re-enter the pipeline and get resolved to their primary source; donor
    listing children are the real calls and go straight to the gate."""
    if not url:
        return []
    try:
        cands = _scan_html(source_name, url)
    except Exception as exc:
        log.debug("expand_listing requests path failed for %s: %s", url, exc)
        cands = []
    if not cands:
        try:
            from core import deep_read
            if deep_read.available():
                cands = _scan_html_js(source_name, url)
        except Exception as exc:
            log.debug("expand_listing JS path failed for %s: %s", url, exc)
    for c in cands:
        c["_expanded_from"] = url
    return cands


def _playwright_available() -> bool:
    """True when Playwright is importable (so we can render JS without recursing
    back into _scan_html when it isn't)."""
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


# SPA framework roots that, on an UNrendered fetch, wrap an otherwise-empty body —
# a plain requests GET sees the shell, not the JS-injected listing.
_SPA_SHELL_MARKERS = (
    'id="root"', "id='root'", 'id="__next"', "__next_data__", "data-reactroot",
    'ng-app', 'id="app"', "window.__nuxt__", "window.__initial_state__",
)


def _looks_like_spa_shell(html_text: str) -> bool:
    """True when the fetched HTML is an unrendered single-page-app shell: very little
    visible text AND a known SPA root marker. Used to decide whether a plain-HTML scan
    that found nothing should be retried through the JS renderer."""
    if not html_text:
        return False
    try:
        text = BeautifulSoup(html_text, "html.parser").get_text(" ", strip=True)
    except Exception:
        text = html_text
    if len(text) >= 800:                       # enough real text → not an empty shell
        return False
    low = html_text.lower()
    return any(m in low for m in _SPA_SHELL_MARKERS)


def _scan_html(name: str, url: str, *, extract_only: bool = False,
               fresh_uids: set[str] | None = None) -> list[dict[str, Any]]:
    """Generic HTML listing-page scraper using `requests` (no JS).
    Suitable for static / server-rendered donor pages. When the fetch is an
    unrendered SPA shell that yields NO candidates, transparently retry through the
    Playwright renderer (if available) so a JS-only listing isn't silently missed or,
    worse, mis-extracted into junk candidates."""
    try:
        r = _http.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except Exception as exc:
        log.warning("HTML fetch failed for %s: %s", url, exc)
        return []
    cands = _extract_candidates_from_html(name, url, r.text,
                                          extract_only=extract_only,
                                          fresh_uids=fresh_uids)
    if not cands and _looks_like_spa_shell(r.text) and _playwright_available():
        log.info("SPA shell detected for %s (no candidates from static HTML) — "
                 "retrying via Playwright renderer", name)
        return _scan_html_js(name, url, extract_only=extract_only,
                             fresh_uids=fresh_uids)
    return cands


def _scan_html_js(name: str, url: str, *, extract_only: bool = False,
                  fresh_uids: set[str] | None = None) -> list[dict[str, Any]]:
    """JS-rendered HTML scraper using Playwright headless Chromium.
    For SPA donor portals (EC Funding Portal, CZI, Mastercard, etc.)
    where the listing widget only populates after JavaScript runs.

    Falls back to plain `_scan_html` if Playwright isn't installed —
    so the scanner stays usable without the optional dependency.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: WPS433 (lazy)
    except ImportError:
        log.warning(
            "playwright not installed — falling back to plain HTML scan for %s. "
            "Install with: pip install playwright && playwright install chromium",
            name,
        )
        return _scan_html(name, url, extract_only=extract_only,
                          fresh_uids=fresh_uids)

    html_text: str | None = None
    try:
        with sync_playwright() as p:
            _px = (os.environ.get("RFPIS_PROXY") or os.environ.get("HTTPS_PROXY")
                   or os.environ.get("HTTP_PROXY"))
            _launch = {"headless": True}
            if _px:
                _launch["proxy"] = {"server": _px}   # hide/rotate the crawl IP
            browser = p.chromium.launch(**_launch)
            try:
                ctx = browser.new_context(user_agent=USER_AGENT)
                page = ctx.new_page()
                page.set_default_timeout(20000)  # 20s default for goto / waits
                page.goto(url, wait_until="networkidle", timeout=20000)
                # Give late-loading widgets one extra second to settle.
                page.wait_for_timeout(1000)
                # SPA listings (EC Funding Portal, DevelopmentAid, etc.) often
                # lazy-render result cards as the user scrolls. Scroll to
                # bottom once + wait so deferred items materialise. Cheap and
                # safe on static pages (scroll-to-bottom is a no-op).
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                except Exception:
                    pass  # don't let a JS quirk kill the whole scan
                html_text = page.content()
            finally:
                browser.close()
    except Exception as exc:
        log.warning("Playwright render failed for %s: %s", url, exc)
        # Last-ditch fallback so the source isn't dead.
        return _scan_html(name, url, extract_only=extract_only,
                          fresh_uids=fresh_uids)

    if not html_text:
        return []
    return _extract_candidates_from_html(name, url, html_text,
                                         extract_only=extract_only,
                                         fresh_uids=fresh_uids)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _clean(s: Any) -> str:
    if s is None:
        return ""
    # Decode HTML entities (feeds/portals emit "&amp;", "&#8203;", "&#39;" …)
    # then strip the now-decoded zero-width / BOM code points, which otherwise
    # survive as literal "&#8203;" noise in titles (e.g. the HRSA MCH LEAP grant).
    s = html.unescape(str(s))
    s = re.sub(r"[​‌‍⁠﻿]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_struct_time(t) -> date | None:
    if not t:
        return None
    try:
        return date(t.tm_year, t.tm_mon, t.tm_mday)
    except Exception:
        return None


def _parse_iso_date(s: str | None) -> date | None:
    """Parse a wide variety of date string formats donors emit.

    Handles ISO 8601, US slash format, AND Grants.gov's natural-language
    timestamps ("May 08, 2026 12:00:00 AM EDT") which were previously
    silently dropping to None.
    """
    if not s:
        return None
    raw = str(s).strip()
    if not raw or raw.lower() == "none":
        return None
    # Strip trailing timezone abbreviation (EDT, PST, UTC, etc.) so the
    # natural-language formats below parse cleanly.
    cleaned = re.sub(r"\s+[A-Z]{2,4}$", "", raw)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%b %d, %Y %I:%M:%S %p",       # "May 08, 2026 12:00:00 AM"
        "%B %d, %Y %I:%M:%S %p",       # "May 08, 2026 12:00:00 AM" (full month)
        "%b %d, %Y",                   # "May 08, 2026"
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(
                cleaned[:len(fmt) + 5] if "%f" in fmt else cleaned,
                fmt,
            ).date()
        except (ValueError, TypeError):
            continue
    return None


def _funder_from_source_name(name: str) -> str:
    """Strip trailing context like ' — donor catalog' or ' RSS' from name."""
    base = name.split("—")[0].strip()
    return re.sub(r"\s+(RSS|API|Feed)$", "", base, flags=re.IGNORECASE)


def _dedup_by_link_or_title(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """In-scrape dedup — same link / same title in one batch."""
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        link = (r.get("opportunity_link") or "").lower().rstrip("/")
        title = (r.get("opportunity_title") or "").lower().strip()
        if link and link in seen_links:
            continue
        if title and title in seen_titles:
            continue
        if link:
            seen_links.add(link)
        if title:
            seen_titles.add(title)
        out.append(r)
    return out


def _filter_relevant(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the deploying org's health-domain keyword filter to candidate titles +
    descriptions. Permissive: any match keeps the row. Use for noise control
    on aggregator feeds (e.g. FundsForNGOs lists everything)."""
    out: list[dict[str, Any]] = []
    for r in rows:
        hay = (
            (r.get("opportunity_title") or "") + " " +
            (r.get("brief_description") or "")
        ).lower()
        if any(kw in hay for kw in HEALTH_KEYWORDS):
            out.append(r)
    return out
