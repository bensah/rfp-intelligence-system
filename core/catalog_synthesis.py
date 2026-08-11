"""Fill the §4 fields of `extracted_solicitations` that nothing has ever written.

WHY THIS EXISTS
---------------
Nine schema fields are populated on ZERO of 686 catalogue rows. They were never failing
extractions — they had no writer at all. `core/extract.py` says the narrative fields are
"populated by a later shadow-mode pass — left None here", and the pass that was eventually
built (`core.llm_synthesis.synthesize_store`) emits `brief_description` and the
tenant-facing review helpers, and never touches these. `full_description` in particular
occurred in exactly two places in the codebase: the column allow-list, and the read on the
opportunity page.

The consequence is a detail page that looks broken but is merely empty: six of its seven
sections have nothing to show.

WHAT IT WRITES, AND HOW
-----------------------
The input is already paid for: `raw_text` holds the call as published, ~3k characters, on
620 of 686 rows. Two kinds of field come out of it.

REGEX — cheap, deterministic, no model call. These are structural facts sitting in the
markup or in fixed phrasing, so a model would only add cost and a chance of invention:

    attachments        links to files shipped with the call (.pdf/.docx/.xlsx…)
    resource_links     templates, guidance, the full RFP — links classed by their label
    submission_format  the channel ("via the online portal", "by email to …")

LLM — genuine reading and rewriting, which regex cannot do:

    full_description        150-300 words, ORIGINAL prose (§4.3, house style §7)
    what_is_funded          scannable bullets
    what_is_not_funded      scannable bullets
    eligibility_countries   who may APPLY (not where the work happens — they differ)
    eligibility_other       registration, consortium, local-partner conditions
    applicant_fit_profile   the ideal applicant's type and maturity
    project_stages          which stages of work the call funds

RUNS ON THE FREE TIER, deliberately. The model is whatever LLM_JUDGE_MODEL points at —
today `gpt-oss:120b` on Ollama Cloud's free tier. That constrains the design rather than
the ambition:

  * ONE call per row for all seven LLM fields, not seven calls. Cost and rate limit are
    per request, and the fields share the same reading of the same document.
  * a bounded input (`_MAX_INPUT` chars) so a long RFP cannot blow the context window
  * JSON-only output, parsed defensively — a free-tier model wraps JSON in prose more
    often than a paid one, so `_parse_json` digs it out rather than discarding the row
  * every field independently optional. A partial answer is written for the fields it got
    right instead of being thrown away whole, which is what makes a weaker model useful.
  * a hard call ceiling per process (`_MAX_CALLS`), so a backfill cannot run away

NOTHING IS INVENTED. Every prompt says to ground each value in the text and to omit rather
than guess, and `synthesize_row` drops any field the model returned empty. A blank column
is a true statement about the call; a plausible sentence about a call that does not say it
is a lie a reviewer would act on.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

log = logging.getLogger(__name__)

# One reading of one document, bounded. 12k characters covers the long tail of
# `raw_text` (~3k typical) with room for a deep-read page, and stays well inside a
# free-tier context window.
_MAX_INPUT = int(os.environ.get("RFPIS_SYNTH_MAX_INPUT", "12000"))
_MAX_OUTPUT_TOKENS = int(os.environ.get("RFPIS_SYNTH_MAX_TOKENS", "2000"))
_MAX_CALLS = int(os.environ.get("RFPIS_SYNTH_MAX_CALLS", "800"))
_CALLS = 0

# BELOW THIS, THERE IS NOTHING TO READ, so spending a call is pure waste. Measured on a
# 20-row batch, 4 rows produced no field at all and every one of them had a raw_text that was
# boilerplate rather than a call: 20 chars ("fundsforNGOs Premium" — an aggregator paywall
# stub), 74, 96 and 108 chars of UNGM site furniture. That is 20% of a free-tier batch spent
# on rows that could not have answered. The model behaved correctly by returning nothing; the
# fix is not to ask.
#
# 400 characters is about two sentences of substance. The threshold is deliberately generous:
# a short-but-real call still gets its call, because a false skip loses a row permanently
# while a wasted call costs seconds.
_MIN_TEXT = int(os.environ.get("RFPIS_SYNTH_MIN_TEXT", "400"))
_SKIPPED_THIN = 0

# House-style caps (§7). Long enough to be useful, short enough that a model padding
# its answer cannot turn a card into an essay.
_LIMITS = {
    "full_description": 4000,      # ~500 words + headroom; the page clips to its own cap
    "what_is_funded": 1500,
    "what_is_not_funded": 1500,
    "eligibility_other": 1200,
    "applicant_fit_profile": 600,
    "project_stages": 400,
    "submission_format": 400,
}

LLM_FIELDS = ("full_description", "what_is_funded", "what_is_not_funded",
              "eligibility_countries", "eligibility_other", "applicant_fit_profile",
              "project_stages", "submission_format")
# submission_format is in BOTH lists deliberately: the regex answers first because a named
# platform and an email address are exact strings a model must never paraphrase, and the
# model — already reading the document for the other seven — answers when regex cannot.
REGEX_FIELDS = ("attachments", "resource_links", "submission_format")
ALL_FIELDS = LLM_FIELDS + tuple(f for f in REGEX_FIELDS if f not in LLM_FIELDS)


# ---------------------------------------------------------------------------
# REGEX: documents shipped with the call
# ---------------------------------------------------------------------------
_DOC_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|rtf|odt)(\?|$)", re.I)

# The label decides the doc_type, per the §4.8 vocabulary. Ordered — the first hit wins,
# so "budget template" is a budget_template rather than a generic template.
_DOC_TYPE_RULES = (
    ("budget_template", r"budget\s*(template|form|sheet|workbook)|financial\s*template"),
    ("narrative_template", r"(narrative|proposal|application|technical)\s*(template|form)"),
    ("full_rfp", r"full\s*(rfp|rfa|call|tender)|solicitation\s*document|"
                 r"(request for (proposals?|applications?))|call\s*(document|text)|"
                 r"terms of reference|\btor\b|bidding document"),
    ("guidance", r"guid(e|ance|elines?)|instructions?|handbook|manual|how to apply"),
    ("faq", r"\bfaq\b|frequently asked"),
    ("annex", r"annex|appendix|attachment\s*\d|schedule\s*\d"),
)


def _doc_type(label: str, url: str) -> str:
    blob = f"{label} {url}".lower()
    for kind, pattern in _DOC_TYPE_RULES:
        if re.search(pattern, blob, re.I):
            return kind
    return "other"


def _clean_label(text: str, url: str) -> str:
    """A human label for a link. Falls back to the file name, never to a bare URL — a
    reviewer scanning a document list needs to know what each one is."""
    t = re.sub(r"\s+", " ", (text or "")).strip(" ·-–—|»>[]()")
    if 2 < len(t) <= 120:
        return t
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", re.sub(r"\.[a-z0-9]{2,5}$", "", name, flags=re.I)).strip() \
        or "Document"


def extract_documents(html: str | None, base_url: str | None = None
                      ) -> tuple[list[dict], list[dict]]:
    """``(attachments, resource_links)`` from a call page's HTML — schema §4.8.

    The split follows the schema: an ATTACHMENT is a file shipped with the call (a real
    document link), a RESOURCE LINK is a referenced page or template. Both are
    ``[{url, label, doc_type}]``. Deduplicated on URL, order preserved, and capped so one
    badly built page cannot write a hundred rows of navigation into the store.
    """
    if not html:
        return [], []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return [], []
    attachments: list[dict] = []
    resources: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base_url or "", href) if base_url else href
        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        label = _clean_label(a.get_text(" ", strip=True), url)
        kind = _doc_type(label, url)
        if _DOC_EXT_RE.search(url):
            seen.add(url)
            attachments.append({"url": url, "label": label, "doc_type": kind})
        elif kind != "other":
            # A PAGE whose label names it a template / guidance / FAQ is a resource.
            # Anything unlabelled is site navigation and is deliberately dropped.
            seen.add(url)
            resources.append({"url": url, "label": label, "doc_type": kind})
        if len(attachments) >= 25 and len(resources) >= 25:
            break
    return attachments[:25], resources[:25]


# ---------------------------------------------------------------------------
# REGEX: how a proposal is submitted
# ---------------------------------------------------------------------------
# NAMED PLATFORMS FIRST. Measuring the phrase-based rules over the corpus returned only
# 3% — the wording is endlessly varied ("submit bids THROUGH UNGM", "respond to a UNOPS
# tender", "registered on the MultiQuote platform") — but the PLATFORM NAME is both easy to
# match exactly and the thing a reviewer needs, because it tells them where the submission
# actually happens and what account they need.
_PLATFORMS = (
    ("UNGM", r"\bUNGM\b|United Nations Global Marketplace"),
    ("UN eTendering (Quantum)", r"\betender|quantum\.partneragencies|\bPOEMS\b"),
    ("grants.gov Workspace", r"grants\.gov|\bworkspace\b(?=[^.]{0,40}applicat)"),
    ("SAM.gov", r"\bsam\.gov\b"),
    ("EU Funding & Tenders Portal",
     r"funding-tenders\.ec\.europa|Funding (and|&) Tenders Portal"),
    ("Submittable", r"\bsubmittable"),
    ("Fluxx", r"\bfluxx\b"),
    ("SmartSimple", r"smartsimple"),
    ("MultiQuote", r"multiquote"),
    ("eProcurement portal", r"e-?procurement (portal|system)|\bSRM\b portal"),
    ("Find a Tender / Contracts Finder", r"find a tender|contracts finder"),
    ("ProZorro / national e-procurement", r"prozorro"),
    ("NIH eRA Commons / ASSIST", r"era commons|\bASSIST\b(?=[^.]{0,30}applicat)"),
)

# Ordered most-specific first: a call naming a portal AND an email wants the portal, which
# is the channel of record.
_SUBMIT_RULES = (
    (r"(?:submit(?:ted)?|apply|application)[^.]{0,80}?\b(?:through|via|on|using)\b[^.]{0,60}?"
     r"\b(online (?:portal|system|form|platform)|e-?portal|submission portal|"
     r"grant(?:s)? portal|application portal|web portal)\b", "Online portal"),
    (r"\b(?:submit|sen[dt]|email|forward)[^.]{0,60}?\bto\b\s*([\w.+-]+@[\w-]+\.[\w.]{2,})", "Email"),
    (r"\b([\w.+-]+@[\w-]+\.[\w.]{2,})\b[^.]{0,40}\b(?:submission|proposals?|applications?)",
     "Email"),
    (r"\b(concept note)\b[^.]{0,60}\b(?:first|before|prior to)\b", "Two-stage: concept note first"),
    (r"\bexpression of interest\b[^.]{0,60}\b(?:first|before|prior to)\b",
     "Two-stage: expression of interest first"),
    (r"\bhard cop(?:y|ies)\b|\bsealed (?:envelope|bid)\b|\bcourier\b|\bby post\b",
     "Physical/sealed submission"),
    (r"\b(?:submit|upload)[^.]{0,50}\b(online|electronic(?:ally)?)\b", "Online submission"),
)


def extract_submission_format(text: str | None) -> str | None:
    """The submission channel in one phrase, with the email address when there is one.

    Regex rather than a model call: the phrasing is formulaic, an address is exact text a
    model should never paraphrase, and getting it wrong sends a proposal nowhere.
    """
    body = re.sub(r"\s+", " ", str(text or ""))
    if not body:
        return None
    # A named platform is the most precise and most useful answer available.
    for name, pattern in _PLATFORMS:
        if re.search(pattern, body, re.I):
            mail = re.search(r"[\w.+-]+@[\w-]+\.[\w.]{2,}", body)
            return (f"Online portal: {name} (queries: {mail.group(0).rstrip(".,;:")})"
                    if mail else f"Online portal: {name}")[:_LIMITS["submission_format"]]
    for pattern, label in _SUBMIT_RULES:
        m = re.search(pattern, body, re.I)
        if not m:
            continue
        # A trailing sentence period gets caught by the address character class; an
        # address with a stray "." on the end is not one a proposal can be sent to.
        detail = next((g.rstrip(".,;:") for g in (m.groups() or ()) if g and "@" in g), None)
        if detail:
            return f"{label}: {detail}"[:_LIMITS["submission_format"]]
        got = next((g for g in (m.groups() or ()) if g), "")
        if got and got.lower() not in label.lower():
            return f"{label} ({got.strip().lower()})"[:_LIMITS["submission_format"]]
        return label
    return None


# ---------------------------------------------------------------------------
# LLM: the seven reading-and-rewriting fields, in ONE call
# ---------------------------------------------------------------------------
_SYSTEM = (
    "You read one funding call and return STRICT JSON describing it. You never invent: "
    "every value must be supported by the text you are given. If the call does not state "
    "something, OMIT that key entirely rather than guessing or writing a placeholder. The one "
    "exception is applicant_fit_profile, which the schema defines as a characterisation "
    "rather than a quotation: infer that from what the call asks for. "
    "Return ONLY the JSON object, no prose before or after it."
)

_USER_TEMPLATE = """Read this {kind} and return a JSON object with these keys.
Omit any key it does not support.

NAME THE THING BY WHAT IT IS. In any prose you write, refer to it as "the {kind}" — not as
"the call". A reader outside this system does not call a tender "a call", and "the call"
reads as internal shorthand. Use the funder's own register: "the request for proposals",
"the tender", "the call for proposals".

full_description: the PUBLISHER'S OWN account of what they aim to fund, up to 500 words —
  purpose, objectives, scope, focus areas, and what a funded project is expected to achieve.
  Stay CLOSE TO THEIR WORDING: condense and tidy to fit the length, drop navigation and
  boilerplate, keep their terms and their emphasis. Do not rewrite it in your own voice, do
  not editorialise, and do not add anything the text does not say. If the source is longer
  than 500 words, cover the opening scope and objectives and stop cleanly — the reader is
  offered a link to the full text.
what_is_funded: array of short bullet strings — the activities, costs or work this call
  will pay for.
what_is_not_funded: array of short bullet strings — explicit exclusions or ineligible
  costs. Only what the call actually rules out.
eligibility_countries: array of country or region names whose organisations MAY APPLY.
  This is the applicant's own registration/location, which is often NOT the same as where
  the funded work happens — if the call only says where the work happens, omit this key.
applicant_fit_profile: 1-2 sentences on the ideal applicant — organisation type and
  maturity (e.g. an established national NGO with prior donor-funded delivery experience).
  THIS ONE KEY IS A JUDGEMENT, not a quote: infer it from what the call asks for (the scale
  of the award, the eligibility wording, the delivery expected) and answer whenever the call
  gives you enough to characterise the applicant it wants. Say "not stated" nowhere — just
  describe the profile the call implies.
eligibility_other: array of other conditions on WHO may apply — registration status, local
  presence, consortium or partnership requirements, prior-experience thresholds,
  audited-accounts requirements.
submission_format: how a proposal is submitted, in one short phrase — the portal by name,
  an email address, or a two-stage process ("concept note first"). Only if the call says so.
project_stages: array from this list only, for the stages of work this call funds:
  ["Research", "Pilot", "Scale-up", "Implementation", "Technical assistance",
   "Capacity building", "Advocacy", "Infrastructure", "Supply/procurement"]

TITLE: {title}
FUNDER: {funder}

CALL TEXT:
{body}
"""

_STAGES = ("Research", "Pilot", "Scale-up", "Implementation", "Technical assistance",
           "Capacity building", "Advocacy", "Infrastructure", "Supply/procurement")


# What to CALL the thing in prose. "The call" is our internal shorthand; a reader outside the
# system does not refer to a tender as a call, and the funder's own register is what makes the
# text read as though a person wrote it about that specific opportunity.
_KIND_PHRASES = {
    "RFP": "request for proposals", "RFA": "request for applications",
    "RFQ": "request for quotation", "RFI": "request for information",
    "CFP": "call for proposals", "CFA": "call for applications",
    "CFCN": "call for concept notes", "EOI": "expression of interest",
    "LOI": "letter of intent", "NOFO": "funding opportunity",
    "TENDER": "tender", "BID": "invitation to bid", "ITB": "invitation to bid",
    "PRIZE": "prize competition", "CHALLENGE": "challenge",
}


def _kind_phrase(row: dict) -> str:
    """"request for proposals" / "tender" / "funding call" — how to name this thing in prose."""
    raw = str((row or {}).get("solicitation_type") or "").strip()
    hit = _KIND_PHRASES.get(raw.replace(" ", "").replace("-", "").upper())
    if hit:
        return hit
    kind = str((row or {}).get("opportunity_type") or "").strip().lower()
    if "procure" in kind or "tender" in kind:
        return "tender"
    if "prize" in kind or "challenge" in kind:
        return "prize competition"
    if "consult" in kind:
        return "consultancy assignment"
    return "funding call"


def is_enabled() -> bool:
    return bool(os.environ.get("LLM_SYNTH_BASE_URL")
                or os.environ.get("LLM_JUDGE_BASE_URL"))


def _model() -> str:
    return (os.environ.get("LLM_SYNTH_MODEL")
            or os.environ.get("LLM_JUDGE_MODEL") or "llama3.1")


def _client():
    from openai import OpenAI
    return OpenAI(
        base_url=(os.environ.get("LLM_SYNTH_BASE_URL")
                  or os.environ.get("LLM_JUDGE_BASE_URL")),
        api_key=(os.environ.get("LLM_SYNTH_API_KEY")
                 or os.environ.get("LLM_JUDGE_API_KEY") or "ollama"),
        timeout=float(os.environ.get("LLM_SYNTH_TIMEOUT")
                      or os.environ.get("LLM_JUDGE_TIMEOUT", "120") or 120),
        max_retries=1,
    )


def _parse_json(raw: str) -> dict | None:
    """The JSON out of a model reply. A free-tier model fences it, prefixes it with
    "Here is the JSON:", or emits a reasoning preamble far more often than a paid one, so
    the object is dug out rather than the row being discarded."""
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.I | re.M).strip()
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(s[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return None


def _lines(v: Any, cap: int) -> str | None:
    """A bullets field as newline-joined text — the shape `opportunity_detail.as_bullets`
    reads. Placeholder answers ("N/A", "Not specified") are dropped, because a model that
    cannot find an exclusion list often says so instead of omitting the key."""
    if v is None:
        return None
    items = v if isinstance(v, (list, tuple)) else [v]
    out: list[str] = []
    for it in items:
        t = re.sub(r"\s+", " ", str(it or "")).strip(" -•*·")
        if not t or t.lower() in ("n/a", "na", "none", "not specified", "not stated",
                                  "unknown", "tbd", "none stated", "not applicable"):
            continue
        out.append(t)
    return "\n".join(out)[:cap] or None


def _text(v: Any, cap: int) -> str | None:
    t = re.sub(r"\s+", " ", str(v or "")).strip()
    if not t or t.lower() in ("n/a", "none", "not specified", "not stated", "unknown"):
        return None
    return t[:cap]


def _countries(v: Any) -> list[str] | None:
    if not isinstance(v, (list, tuple)):
        v = [v] if v else []
    out: list[str] = []
    for it in v:
        t = re.sub(r"\s+", " ", str(it or "")).strip(" .;,")
        if 1 < len(t) <= 60 and t.lower() not in ("n/a", "none", "not specified",
                                                  "unknown", "not stated"):
            if t not in out:
                out.append(t)
    return out[:60] or None


def _stages(v: Any) -> list[str] | None:
    if not isinstance(v, (list, tuple)):
        v = [v] if v else []
    valid = {s.lower(): s for s in _STAGES}
    out: list[str] = []
    for it in v:
        got = valid.get(re.sub(r"\s+", " ", str(it or "")).strip().lower())
        if got and got not in out:
            out.append(got)
    return out or None


def synthesize_row(row: dict, *, html: str | None = None) -> dict:
    """The §4 fields this row is missing, ready to write. ``{}`` when nothing could be got.

    Only fields that are BLANK on the row are produced, so a re-run never overwrites a
    value already there (including one a human corrected). The regex fields are always
    attempted — they are free. The model call happens only if an LLM field is missing and
    there is text to read.
    """
    global _CALLS
    out: dict[str, Any] = {}
    body = str(row.get("raw_text") or "").strip()

    def _missing(field: str) -> bool:
        v = row.get(field)
        if v is None:
            return True
        if isinstance(v, (list, tuple, dict)):
            return len(v) == 0
        return str(v).strip() in ("", "[]", "{}")

    # --- regex fields, no model call ---------------------------------------
    if html and (_missing("attachments") or _missing("resource_links")):
        att, res = extract_documents(html, row.get("opportunity_url"))
        if att and _missing("attachments"):
            out["attachments"] = att
        if res and _missing("resource_links"):
            out["resource_links"] = res
    if _missing("submission_format"):
        fmt = extract_submission_format(body or html)
        if fmt:
            out["submission_format"] = fmt

    # --- the one model call ------------------------------------------------
    wanted = [f for f in LLM_FIELDS if _missing(f) and f not in out]
    if not wanted or not body or not is_enabled():
        return out
    if len(body) < _MIN_TEXT:
        # Boilerplate, a paywall stub or a footer — not a call. The regex fields above still
        # stand, and they are free.
        global _SKIPPED_THIN
        _SKIPPED_THIN += 1
        log.info("catalog_synthesis: %s has only %d chars of text — no call made",
                 row.get("uid"), len(body))
        return out
    if _CALLS >= _MAX_CALLS:
        log.info("catalog_synthesis: call ceiling %d reached", _MAX_CALLS)
        return out
    prompt = _USER_TEMPLATE.format(
        kind=_kind_phrase(row),
        title=(row.get("opportunity_name") or "")[:300],
        funder=(row.get("funder_name") or "")[:200],
        body=body[:_MAX_INPUT])
    try:
        _CALLS += 1
        resp = _client().chat.completions.create(
            model=_model(),
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": prompt}],
            # Low but not zero: the prose must vary per call rather than settling into one
            # template, while the factual keys stay anchored by the "never invent" rule.
            temperature=0.3, max_tokens=_MAX_OUTPUT_TOKENS)
        raw = (resp.choices[0].message.content or "") if resp.choices else ""
    except Exception as exc:
        log.warning("catalog_synthesis failed for %s: %s: %s",
                    row.get("uid"), type(exc).__name__, exc)
        return out
    parsed = _parse_json(raw)
    if not parsed:
        log.info("catalog_synthesis non-JSON for %s: %r", row.get("uid"), raw[:200])
        return out

    # Field by field, so a partial answer is still worth writing — the point of making a
    # free-tier model useful rather than demanding all seven or nothing.
    got = {
        "full_description": _text(parsed.get("full_description"),
                                  _LIMITS["full_description"]),
        "what_is_funded": _lines(parsed.get("what_is_funded"), _LIMITS["what_is_funded"]),
        "what_is_not_funded": _lines(parsed.get("what_is_not_funded"),
                                     _LIMITS["what_is_not_funded"]),
        "eligibility_countries": _countries(parsed.get("eligibility_countries")),
        "eligibility_other": _lines(parsed.get("eligibility_other"),
                                    _LIMITS["eligibility_other"]),
        "applicant_fit_profile": _text(parsed.get("applicant_fit_profile"),
                                       _LIMITS["applicant_fit_profile"]),
        "project_stages": _stages(parsed.get("project_stages")),
        "submission_format": _text(parsed.get("submission_format"),
                                   _LIMITS["submission_format"]),
    }
    for field in wanted:
        if got.get(field) and field not in out:      # a regex answer already won
            out[field] = got[field]
    return out


def calls_made() -> int:
    return _CALLS


def skipped_thin() -> int:
    """Rows that had too little text to be worth a call — reported so a thin batch reads as
    a source problem rather than as the model failing."""
    return _SKIPPED_THIN


def reset_calls() -> None:
    global _CALLS, _SKIPPED_THIN
    _CALLS = 0
    _SKIPPED_THIN = 0
