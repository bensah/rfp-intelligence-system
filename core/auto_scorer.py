"""Apply policy-based auto-scoring to a candidate RFP at scan time.

Two pieces:

  * `is_eligible(candidate, policies)` — country + theme gate. If False, the
    candidate is dropped before insertion (we don't pollute the DB with
    out-of-scope RFPs). The Screen view never sees these.

  * `auto_score(candidate, policies)` — for an eligible candidate, returns
    the 9 MUST/PREFER values + feasibility + alignment_score +
    auto_recommendation. Reviewers can still override any of these in the
    Review tab.

Algorithm for a single criterion:
  text = title + description (lowercased)
  pos = count(positive_keywords found in text)
  neg = count(negative_keywords found in text)
  if rigor == 0:           value = "Yes"          (criterion not enforced)
  elif neg > 0:            value = "No"           (strong negative match)
  elif pos >= rigor:       value = "Yes"
  elif pos >= ceil(rigor/2): value = "Partial"
  else:                    value = "No"

So higher rigor demands more positive evidence to score Yes.
"""
from __future__ import annotations

import math
import re
from typing import Any

from core.policies import CRITERION_KEYS
from core.scorer import score_submission


# A reasonable list of LMICs and other countries that often appear in donor
# RFPs. Used by the country eligibility gate to detect "this RFP names a
# specific country that ISN'T one of ours" — and reject. Not exhaustive (no
# G7 / OECD high-income countries by default since they rarely host the
# kinds of RFPs a global-health implementing org pursues; admin can
# extend via policies if needed).
KNOWN_COUNTRIES: tuple[str, ...] = (
    # West & Central Africa
    "Benin", "Burkina Faso", "Cabo Verde", "Cameroon", "Central African Republic",
    "Chad", "Côte d'Ivoire", "Cote d'Ivoire", "Ivory Coast", "Equatorial Guinea",
    "Gabon", "Gambia", "Ghana", "Guinea", "Guinea-Bissau", "Liberia",
    "Mali", "Mauritania", "Niger", "Nigeria", "Republic of Congo",
    "Sao Tome", "Senegal", "Sierra Leone", "Togo",
    "DRC", "Democratic Republic of the Congo", "Congo-Kinshasa",
    # East & Southern Africa
    "Angola", "Botswana", "Burundi", "Comoros", "Djibouti", "Eritrea",
    "Eswatini", "Ethiopia", "Kenya", "Lesotho", "Madagascar", "Malawi",
    "Mauritius", "Mozambique", "Namibia", "Rwanda", "Seychelles",
    "Somalia", "South Africa", "South Sudan", "Sudan", "Tanzania",
    "Uganda", "Zambia", "Zimbabwe",
    # North Africa & Middle East
    "Algeria", "Egypt", "Libya", "Morocco", "Tunisia",
    "Iraq", "Iran", "Jordan", "Lebanon", "Palestine", "Syria", "Yemen",
    # South Asia
    "Afghanistan", "Bangladesh", "Bhutan", "India", "Maldives",
    "Nepal", "Pakistan", "Sri Lanka",
    # Southeast & East Asia
    "Cambodia", "Indonesia", "Laos", "Malaysia", "Myanmar", "Burma",
    "Philippines", "Thailand", "Timor-Leste", "Vietnam",
    "China", "Mongolia", "North Korea", "South Korea",
    # Central Asia & Caucasus
    "Armenia", "Azerbaijan", "Georgia", "Kazakhstan", "Kyrgyzstan",
    "Tajikistan", "Turkmenistan", "Uzbekistan",
    # Pacific
    "Fiji", "Kiribati", "Papua New Guinea", "Samoa", "Solomon Islands",
    "Tonga", "Vanuatu",
    # Latin America & Caribbean
    "Belize", "Bolivia", "Brazil", "Colombia", "Costa Rica", "Cuba",
    "Dominican Republic", "Ecuador", "El Salvador", "Guatemala", "Guyana",
    "Haiti", "Honduras", "Jamaica", "Mexico", "Nicaragua", "Panama",
    "Paraguay", "Peru", "Suriname", "Venezuela", "Trinidad",
    # Europe (post-Soviet / Balkans — sometimes appear)
    "Albania", "Belarus", "Bosnia", "Kosovo", "Moldova", "North Macedonia",
    "Serbia", "Ukraine",
    # High-income countries — included so the gate can REJECT RFPs that
    # restrict eligibility to e.g. "US-based" or "applicants in Germany".
    # Most are NOT in the deploying org's eligible list, so detecting them as mentioned
    # triggers the non-eligible-country path. (Add to policies.countries.
    # eligible if your org operates in any of these.)
    "United States", "USA", "US",
    "United Kingdom", "UK", "Britain",
    "Canada", "Australia", "New Zealand",
    "Germany", "France", "Spain", "Italy", "Portugal",
    "Netherlands", "Belgium", "Switzerland", "Austria",
    "Sweden", "Norway", "Denmark", "Finland", "Iceland",
    "Ireland", "Greece", "Poland", "Czech Republic",
    "Japan", "South Korea", "Singapore", "Hong Kong",
    "Israel", "United Arab Emirates", "Saudi Arabia",
    "Russia", "Russian Federation",
)

# Pre-compile the country search regex once. Use word boundaries so e.g.
# "Mali" doesn't match "Somalia" (the longer name should win when present).
# We search for whole-word case-insensitive matches.
_COUNTRY_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in sorted(KNOWN_COUNTRIES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize(s: str | None) -> str:
    return (s or "").lower()


def _full_text(candidate: dict[str, Any]) -> str:
    """Concatenate every field that might describe the opportunity."""
    return _normalize(
        " ".join([
            candidate.get("opportunity_title") or "",
            candidate.get("brief_description") or "",
            " ".join(candidate.get("geographic_scope") or []),
            candidate.get("focus_theme") or "",
            candidate.get("funding_agency") or "",
        ])
    )


def _matches(text: str, keywords: list[str]) -> int:
    """Count substring matches (case-insensitive). Doesn't double-count one
    keyword appearing multiple times — each keyword is 0 or 1."""
    if not keywords:
        return 0
    text = text.lower()
    return sum(1 for kw in keywords if kw and kw.lower() in text)


# ---------------------------------------------------------------------------
# Country & theme eligibility (PRE-insert gate)
# ---------------------------------------------------------------------------
_INCLUSIVE_ELIGIBILITY_PATTERN = re.compile(
    r"(?:"
    r"foreign\s+(?:organizations?|entities|applicants?|institutions?)\s+(?:are\s+)?eligible"
    r"|non[\-\s]*domestic\s+(?:entities|organizations?|applicants?)?\s*(?:are\s+)?eligible"
    r"|non[\-\s]*u\.?s\.?\s+(?:entities|organizations?|applicants?)\s+(?:are\s+)?eligible"
    r"|international\s+(?:applicants?|organizations?|entities)\s+(?:are\s+)?(?:welcomed?|eligible|accepted)"
    r"|open\s+to\s+(?:applicants?\s+from\s+)?(?:any|all)\s+countr"
    r"|worldwide\s+eligibility"
    r"|globally\s+eligible"
    r"|applicants?\s+from\s+(?:any|all)\s+countr"
    r")",
    re.IGNORECASE,
)


def _has_inclusive_eligibility(text: str) -> bool:
    """Detect 'foreign / international / non-US applicants are eligible'
    patterns. If found, the country gate should NOT reject just because
    the text mentions a specific country (e.g. the US) — it's an
    inclusion statement, not a restriction."""
    return bool(_INCLUSIVE_ELIGIBILITY_PATTERN.search(text or ""))


def country_eligible(candidate: dict[str, Any], policies: dict[str, Any]) -> tuple[bool, str]:
    """Return (eligible, reason).

    Decision tree:
      1. If the text contains an INCLUSIVE eligibility statement
         ("foreign organizations are eligible", "open to any country",
         etc.) → eligible regardless of which specific countries appear.
      2. Find every KNOWN_COUNTRIES mention in the candidate text.
      3. If ANY mentioned country is in our eligible list → ELIGIBLE.
      4. If a specific country was mentioned but NONE are eligible
         → REJECT (the Somalia case — RFP targets a country that
         isn't ours).
      5. If no specific country was mentioned, fall back to broad-term
         matching ("LMIC", "Africa", etc.) and permissive_when_silent.
    """
    countries = policies.get("countries", {}) or {}
    eligible = countries.get("eligible") or []
    broad = countries.get("broad_terms") or []
    permissive = bool(countries.get("permissive_when_silent", True))
    text = _full_text(candidate)
    eligible_lower = {c.lower() for c in eligible if c}

    # Step 1: inclusive eligibility statement short-circuits everything.
    # Prevents false rejects like "non-U.S. entities are eligible" being
    # treated as "U.S. only".
    if _has_inclusive_eligibility(text):
        return True, "RFP explicitly opens to foreign / international applicants"

    # Step 2: find every known country name in the text.
    mentioned = {m.lower() for m in _COUNTRY_PATTERN.findall(text)}

    if mentioned:
        overlap = mentioned & eligible_lower
        if overlap:
            return True, f"mentions eligible country: {sorted(overlap)[0]}"
        # A specific non-eligible country is named → reject.
        sample = sorted(mentioned)[0]
        return False, f"mentions non-eligible country ({sample}); eligible: {sorted(eligible_lower)}"

    # Step 3: no specific country named → check broad geographic terms.
    if any(b.lower() in text for b in broad if b):
        return True, "matches broad-geography term"

    # Step 4: no geography at all?
    geo_signal = re.search(
        r"\b(countr|region|continent|world|global|local|africa|asia|europe|america)\b",
        text,
    )
    if not geo_signal:
        if permissive:
            return True, "no geography mentioned (permissive)"
        return False, "no geography mentioned (strict)"

    return False, "geography mentioned but no overlap with eligible countries"


def theme_eligible(candidate: dict[str, Any], policies: dict[str, Any]) -> tuple[bool, str]:
    themes = policies.get("themes", {}) or {}
    required = themes.get("required_any") or []
    excluded = themes.get("excluded_any") or []
    text = _full_text(candidate)

    if excluded and any(e.lower() in text for e in excluded if e):
        return False, f"matches excluded theme"
    if not required:
        return True, "no theme requirements set"
    if any(kw.lower() in text for kw in required if kw):
        return True, "matches required theme keyword"
    return False, "no required theme keyword matched"


# Closure phrases — donors who run "rolling" calls (no deadline) but
# explicitly say the window is currently closed. Pure Earth's
# Opportunity Fund is the canonical case:
#   "Due to strong interest and a high number of applicants, Pure Earth's
#    Opportunity Fund is no longer accepting applications at this time."
# Without a deadline to compare against, we'd otherwise let these through.
# All matches are case-insensitive substrings of the page text (already
# lower-cased in _full_text via auto_score's pipeline).
_CLOSURE_PHRASE_RE = re.compile(
    r"("
    r"no longer accepting applications?"
    r"|not accepting (?:new )?applications?(?:\s+at this time)?"
    r"|currently (?:not|closed to) (?:accepting|applications?)"
    r"|applications? (?:are|have been|is)\s+(?:now\s+)?closed"
    r"|this (?:call|programme?|program|fund|opportunity) is (?:now\s+)?closed"
    r"|funding (?:round|cycle|window) (?:is\s+)?closed"
    r"|we (?:have\s+)?(?:suspended|paused|temporarily closed)"
    r"|applications? (?:will\s+)?reopen"
    r"|check back (?:later|in)"
    r"|on (?:hold|pause)"
    r"|closed for (?:new\s+)?applications?"
    r")",
    re.IGNORECASE,
)


def closed_call_hard_reject(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Scan-time reject if the page body contains an explicit "closed /
    not accepting" phrase. Especially valuable for rolling-deadline
    donors (Pure Earth, ELMA, etc.) where the absence of a deadline
    would otherwise let an inactive call slip through.

    Conservative on phrasing — only matches unambiguous closure language,
    not generic occurrences of "closed" (e.g. "closed-loop systems").
    """
    text = _full_text(candidate)
    m = _CLOSURE_PHRASE_RE.search(text)
    if m:
        # Trim the match to keep the reason line compact in scan_logs.
        return True, f"call explicitly closed: {m.group(0)!r}"
    return False, ""


def feasibility_hard_reject(candidate: dict[str, Any], policies: dict[str, Any]) -> tuple[bool, str]:
    """If the FEASIBILITY criterion's negative keywords match, reject the
    candidate at scan time (don't even insert it). Negative keywords on
    feasibility represent "we cannot do this kind of work" — so they double
    as a hard kill switch, not just a Score=No nudge.

    Other criteria's negative keywords are softer — they only affect
    scoring, not admission.
    """
    rule = ((policies.get("criteria") or {}).get("feasibility") or {})
    negatives = rule.get("negative") or []
    if not negatives:
        return False, ""
    text = _full_text(candidate)
    hits = [kw for kw in negatives if kw and kw.lower() in text]
    if hits:
        return True, f"feasibility negative keyword: {hits[0]!r}"
    return False, ""


_URL_YEAR_RE_AS = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


def _latest_year_in(text: str) -> int | None:
    """Find the latest 20xx year in arbitrary text. Used as a fallback
    past-deadline signal when no explicit deadline phrase parsed (donor
    pages frequently embed the year in the URL or title)."""
    if not text:
        return None
    years = [int(y) for y in _URL_YEAR_RE_AS.findall(text)]
    return max(years) if years else None


# Threshold: if more than this fraction of non-whitespace characters in
# the brief description are outside Latin/Latin-Extended-A/B, treat the
# document as non-English/French and reject. Generous default (0.30)
# because some donor pages include Arabic / Chinese boilerplate footer
# even on English documents.
_NON_LATIN_REJECT_THRESHOLD = 0.30


def _is_non_latin_dominant(text: str) -> bool:
    """Return True if the text is dominantly non-Latin script (Arabic,
    CJK, Cyrillic, Hebrew, Devanagari, etc.). English + French use
    Latin/Latin-Extended only (code points up to U+024F), so anything
    significantly above that threshold isn't a language the deploying-org
    team can process directly."""
    if not text:
        return False
    sample = text[:2500]
    non_space = [c for c in sample if not c.isspace()]
    if len(non_space) < 80:  # too short to judge reliably
        return False
    non_latin = sum(1 for c in non_space if ord(c) > 0x024F)
    return (non_latin / len(non_space)) > _NON_LATIN_REJECT_THRESHOLD


def language_eligible(candidate: dict[str, Any]) -> tuple[bool, str]:
    """English + French (Latin script) only. Reject Arabic / CJK /
    Cyrillic / Hebrew / etc. so the team isn't handed documents they
    can't review directly."""
    blob = " ".join([
        candidate.get("opportunity_title") or "",
        candidate.get("brief_description") or "",
    ])
    if _is_non_latin_dominant(blob):
        return False, "non-Latin script (English/French only)"
    return True, ""


def deadline_in_future(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Reject candidates whose submission deadline has already passed.

    Three sources of truth, in priority order:
      1. Explicit `submission_deadline` set by the enrichment pipeline.
      2. Latest year in the opportunity_link (or title) — fallback when
         the deadline phrase couldn't be parsed (e.g. the body text is
         in Arabic so our regex missed it, or enrichment was skipped).
      3. No signal at all → keep (rolling RFP).
    """
    from datetime import date as _date, datetime as _dt
    today = _date.today()
    deadline = candidate.get("submission_deadline")
    if not deadline:
        # Fallback: look for a year in the URL or title.
        blob = " ".join([
            candidate.get("opportunity_link") or "",
            candidate.get("opportunity_title") or "",
        ])
        yr = _latest_year_in(blob)
        if yr and yr < today.year:
            return False, (
                f"URL/title year {yr} is past (no explicit deadline parsed)"
            )
        return True, ""
    # Below this point we have an actual `deadline` value to inspect.
    # Accept date, datetime, or ISO string.
    if isinstance(deadline, str):
        try:
            deadline = _dt.fromisoformat(deadline.split("T")[0]).date()
        except (ValueError, TypeError):
            return True, ""  # unparseable — fall through
    elif isinstance(deadline, _dt):
        deadline = deadline.date()
    if not isinstance(deadline, _date):
        return True, ""
    if deadline < today:
        return False, f"deadline passed ({deadline.isoformat()})"
    return True, ""


_SEARCH_URL_PATTERN_AS = re.compile(
    r"(?:[?&]filter_\w+=|[?&]search=|[?&]keyword=|[?&]q="
    r"|[?&]submit=|[?&]post_type=|/search/?[?&]"
    r"|/(?:catalog|listing|results?)/?\?)",
    re.IGNORECASE,
)


def is_eligible(candidate: dict[str, Any], policies: dict[str, Any]) -> tuple[bool, str]:
    """Combined gate: search-URL, language, feasibility, deadline, country,
    theme. Logged in scan output for transparency."""
    # Search/filter result URLs are not grant detail pages — they re-list
    # grants on click. Reject before any other check.
    link = candidate.get("opportunity_link") or ""
    if candidate.get("_is_search_page") or _SEARCH_URL_PATTERN_AS.search(link):
        return False, "URL is a search / filter results page, not a grant detail"
    # DevelopmentAid past-tense grant (Awarded / Closed). Set by the
    # bespoke enricher in scraper._enrich_developmentaid — those listings
    # show on the catalog but aren't open opportunities.
    if candidate.get("_past_tense_grant"):
        return False, "past-tense grant (DevelopmentAid status: Awarded / Closed)"
    # Language — if it's Arabic/CJK/etc. nothing downstream can process
    # it usefully and a non-Latin description makes deadline / keyword
    # extraction unreliable. Cheap check, fail fast.
    ok, reason = language_eligible(candidate)
    if not ok:
        return False, f"language: {reason}"
    rejected, reason = feasibility_hard_reject(candidate, policies)
    if rejected:
        return False, f"feasibility: {reason}"
    # Closure phrase — catches rolling-deadline donors who've explicitly
    # paused intake ("no longer accepting applications"). Run before
    # deadline check because rolling calls have no deadline to compare.
    rejected, reason = closed_call_hard_reject(candidate)
    if rejected:
        return False, reason
    ok, reason = deadline_in_future(candidate)
    if not ok:
        return False, f"deadline: {reason}"
    ok, reason = country_eligible(candidate, policies)
    if not ok:
        return False, f"country: {reason}"
    ok, reason = theme_eligible(candidate, policies)
    if not ok:
        return False, f"theme: {reason}"
    return True, "eligible"


# ---------------------------------------------------------------------------
# Per-criterion scoring
# ---------------------------------------------------------------------------
def _criterion_value(text: str, rule: dict[str, Any]) -> str:
    """Return one of 'Yes' / 'Partial' / 'No'."""
    rigor = int(rule.get("rigor", 2))
    positive = rule.get("positive") or []
    negative = rule.get("negative") or []
    if rigor <= 0:
        return "Yes"  # not enforced — full credit
    neg_hits = _matches(text, negative)
    if neg_hits > 0:
        return "No"
    pos_hits = _matches(text, positive)
    if pos_hits >= rigor:
        return "Yes"
    if pos_hits >= max(1, math.ceil(rigor / 2)):
        return "Partial"
    return "No"


_MUST_KEYS = tuple(k for k in CRITERION_KEYS if k.startswith("must_"))
_PREFER_KEYS = tuple(k for k in CRITERION_KEYS if k.startswith("prefer_"))

# Internal scoring vocab ("Yes"/"Partial"/"No") → DB / UI dropdown vocab
# ("True"/"Partial"/"False"). Applied right before auto_score returns so
# every persisted MUST/PREFER cell matches the eligibility_values list
# in config/dropdowns.yaml. Anything else (Feasibility = None, etc.)
# passes through unchanged.
_CRITERION_DB_VOCAB = {"Yes": "True", "No": "False", "Partial": "Partial"}


# Signals that route a candidate to "Proceed as Sub" instead of straight
# "Proceed".
#
# NOTE: The logic below encodes the REFERENCE-DEPLOYMENT business rules
# (CHAI BDT — Cameroon country office of CHAI Inc., a US 501(c)(3)).
# When RFPIS goes multi-tenant, this signal list should move into a
# per-org config table (`organizations.sub_role_signals`) and stop
# living in code. For now it ships with the CHAI BDT rules verbatim.
#
# CHAI BDT structure:
#   * CHAI Inc. is a US-registered 501(c)(3) — the global parent entity.
#   * 35+ semi-autonomous country offices (including CHAI Cameroon) can
#     apply directly OR route through CHAI US.
# So "US-based applicant required" is NOT an exclusion — it's a directive
# to apply via CHAI US, with the country team (e.g. Cameroon) as sub. The
# Cameroon-facing app surfaces that as "Proceed as sub" so the team knows
# they will be downstream of CHAI US on this one.
#
# For research-institution / university requirements: CHAI is an
# implementation-focused NGO, not a research-degree-granting institution,
# so we'd need a research-org partner as Prime with CHAI sub. Same Sub
# routing.
#
# For EU/Canada/etc. residency requirements: CHAI lacks a local 501(c)
# equivalent in most of those geographies, so it would partner with a
# regional NGO as Prime — again CHAI as Sub.
_SUB_ROLE_SIGNALS = (
    # Research / academic — CHAI is not a research institution
    "academic institution",
    "academic institutions",
    "research institution",
    "research institutions",
    "research-intensive",
    "university",
    "universities",
    "graduate-degree-granting",
    "hochschule",
    "ihe",  # Institutions of Higher Education (US fed grants term)
    "phd-granting",
    # US residency — CHAI Cameroon goes sub to CHAI US (which IS US-based)
    "u.s.-based",
    "us-based",
    "based in the united states",
    "domestic applicants only",
    # EU / Canada / other — CHAI would partner with a regional lead
    "based in the eu",
    "based in europe",
    "european institution",
    "canadian institution",
    "based in canada",
)


def _detect_chai_role(text: str) -> str:
    """Default CHAI to **Prime**. Switch to **Sub** when the RFP text
    contains signals that route CHAI Cameroon downstream of another
    applicant — most commonly:

      * Research / university requirement → CHAI sub to a research-org Prime
      * US-residency requirement → CHAI Cameroon sub to CHAI US (which IS
        a US 501(c)(3))
      * Other regional residency (EU, Canada) → CHAI sub to a regional NGO

    Critically: these are NOT exclusions — they're sub-routing signals.
    The recommendation stays Proceed; only the role flips."""
    if not text:
        return "Prime"
    tl = text.lower()
    if any(s in tl for s in _SUB_ROLE_SIGNALS):
        return "Sub"
    return "Prime"


def _decision_from_criteria(values: dict[str, str]) -> str:
    """CHAI-specific decision tree (overrides scorer.auto_recommendation):

      * Any MUST = No (False)           → Decline
      * ≥2 MUSTs = Partial               → Decline
      * Exactly 1 MUST = Partial         → Park (review)
      * All MUSTs = Yes (True):
          * ≥3 of 4 PREFERs = Yes        → Proceed
          * else                         → Park (review)
    """
    musts_no = sum(1 for m in _MUST_KEYS if values.get(m) == "No")
    musts_partial = sum(1 for m in _MUST_KEYS if values.get(m) == "Partial")
    if musts_no >= 1:
        return "Decline"
    if musts_partial >= 2:
        return "Decline"
    if musts_partial == 1:
        return "Park"
    # All MUSTs = Yes at this point.
    prefers_yes = sum(1 for p in _PREFER_KEYS if values.get(p) == "Yes")
    if prefers_yes >= 3:
        return "Proceed"
    return "Park"


def _extract_geographic_scope(text: str, policies: dict[str, Any]) -> list[str]:
    """Detect KNOWN_COUNTRIES mentions in the candidate's text. Returns the
    list of country names verbatim (case-preserved via the matched form).
    Restricted to eligible-list countries + broad regions when present, so
    Decline / out-of-scope rows don't get spurious geography tags."""
    if not text:
        return []
    eligible = {
        c.lower()
        for c in (policies.get("countries") or {}).get("eligible", []) or []
        if c
    }
    found: list[str] = []
    seen: set[str] = set()
    for m in _COUNTRY_PATTERN.findall(text):
        key = m.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(m)
    # Prioritise eligible countries first in the list (so Cameroon shows
    # before Nigeria if both are mentioned).
    found.sort(key=lambda c: (0 if c.lower() in eligible else 1, c))
    return found[:8]  # cap to avoid overflowing the column


def _extract_program_area(text: str, policies: dict[str, Any]) -> list[str]:
    """Match candidate text against the policy themes.required_any list.
    Each matched theme keyword is added (verbatim) to program_area."""
    if not text:
        return []
    required = (policies.get("themes") or {}).get("required_any", []) or []
    matched: list[str] = []
    tl = text.lower()
    for kw in required:
        if kw and kw.lower() in tl and kw not in matched:
            matched.append(kw)
    return matched[:6]  # cap


def _apply_scoring_rules(
    values: dict[str, str | None],
    candidate: dict[str, Any],
    policies: dict[str, Any],
    text: str,
    criteria_rules: dict[str, Any],
) -> dict[str, Any]:
    """Apply the scoring-rules override layer ON TOP of keyword-based values.

    This is where domain knowledge that can't be expressed as keyword bags
    lives: funder identity, amount tiers, criterion defaults. Each rule
    block is enabled-by-default but admin-tunable via Admin → Settings.

    Order of application matters: later rules can override earlier ones for
    the same criterion. Current order is intentional:
        1. USG funder → Compliant/Resourcing = Partial
        2. Funding-quality tiers → PREFER 6 = Yes/Partial/No by amount
        3. Resourcing large-amount → Resourcing = Partial
        4. Criterion defaults → Monitorable=Yes, Partnership=No when no signal
    """
    rules = (policies.get("scoring_rules") or {}) if isinstance(policies, dict) else {}
    if not rules:
        return values
    funder = (candidate.get("funding_agency") or "").strip()
    # Compute USD-converted estimated_value once. Falls back to 0 on bad data.
    try:
        from core import dropdowns as _dd
        raw_amount = candidate.get("estimated_value")
        amount_usd = (
            float(raw_amount) * _dd.usd_rate(candidate.get("currency"))
            if raw_amount not in (None, "", 0) else 0.0
        )
    except (TypeError, ValueError, ImportError):
        amount_usd = 0.0

    # --- 1. USG-funder rule --------------------------------------------------
    usg = rules.get("usg_funders") or {}
    if usg.get("enabled") and funder:
        funder_lc = funder.lower()
        patterns = [p.lower() for p in (usg.get("patterns") or []) if p]
        if any(p in funder_lc for p in patterns):
            for k, v in (usg.get("forced_values") or {}).items():
                values[k] = v

    # --- 2. Funding-quality tiers ------------------------------------------
    fq = rules.get("funding_quality_tiers") or {}
    if fq.get("enabled") and amount_usd > 0:
        tiers = fq.get("tiers") or []
        # Tiers should be ordered HIGH → LOW; first satisfied wins.
        for tier in tiers:
            try:
                threshold = float(tier.get("threshold_usd", 0))
            except (TypeError, ValueError):
                continue
            if amount_usd >= threshold:
                values["prefer_6_funding_quality"] = tier.get("value") or values.get("prefer_6_funding_quality")
                break

    # --- 3. Resourcing large-amount ----------------------------------------
    res = rules.get("resourcing_large_amount") or {}
    if res.get("enabled") and amount_usd > 0:
        try:
            threshold = float(res.get("threshold_usd", 0))
        except (TypeError, ValueError):
            threshold = 0
        if threshold and amount_usd >= threshold:
            # Don't downgrade if a previous rule (e.g. USG) already set
            # Partial — stays Partial. Don't upgrade Yes → Partial either.
            current = values.get("must_5_resourcing")
            if current in (None, "No"):
                values["must_5_resourcing"] = res.get("forced_value") or "Partial"

    # --- 4. Criterion defaults (Monitorable, Partnership) -------------------
    crit_defaults = rules.get("criterion_defaults") or {}
    for crit_key, conf in crit_defaults.items():
        if not isinstance(conf, dict) or not conf.get("enabled"):
            continue
        default_val = conf.get("default_value")
        if not default_val:
            continue
        current = values.get(crit_key)

        # Respect explicit positive text signals if asked
        if conf.get("respect_positive_keywords", True) and current == "Yes":
            continue  # keyword scoring already said Yes — leave it

        # Respect explicit negative text signals if asked (Monitorable case)
        if conf.get("respect_negative_keywords", True):
            rule = criteria_rules.get(crit_key) or {}
            negatives = rule.get("negative") or []
            if any(n.lower() in text.lower() for n in negatives if n):
                continue  # explicit barrier — keep keyword scorer's "No"

        # Otherwise force the default. This is the key behavior:
        # Monitorable defaults Yes (most modern grants assume M&E);
        # Partnership defaults No (reviewer-confirmed signal).
        values[crit_key] = default_val

    return values


def auto_score(
    candidate: dict[str, Any], policies: dict[str, Any],
) -> dict[str, Any]:
    """Return a dict of fields ready to merge into the rfp_submissions row.

    Output keys: feasibility, must_1_govt_alignment, ..., prefer_9_scale,
    alignment_score, auto_recommendation, decision, decline_flags_present,
    geographic_scope (if detected), program_area (if detected).
    """
    text = _full_text(candidate)
    criteria_rules = policies.get("criteria", {}) or {}
    values: dict[str, str | None] = {}
    for key in CRITERION_KEYS:
        rule = criteria_rules.get(key) or {}
        values[key] = _criterion_value(text, rule)

    # Feasibility uses a HIGH/MEDIUM/LOW vocabulary in the UI (see
    # config/dropdowns.yaml → feasibility), not the YES/PARTIAL/NO that
    # _criterion_value emits. Writing "No" here would surface as a stray
    # option on the Edit dropdown. Reserve feasibility for human judgement:
    # auto-scan leaves it NULL, the reviewer picks High/Medium/Low after
    # reading the brief. The scan-time hard reject (feasibility_hard_reject
    # above) still fires using the same keyword config — we just don't
    # persist a score-style value.
    values["feasibility"] = None

    # Apply the scoring-rules override layer. This captures the domain
    # knowledge that pure keyword matching can't express — funder identity,
    # amount-based tiers, "default-true unless explicit barrier" style
    # rules. See policies.DEFAULT_POLICIES["scoring_rules"] for the
    # configurable shape; the function below is the executor.
    values = _apply_scoring_rules(values, candidate, policies, text, criteria_rules)

    # NEW decline_flags rule (per CHAI policy):
    #   Decline flag = NO only when all 5 MUSTs == Yes AND ≥3 of 4 PREFERs == Yes
    #   Decline flag = YES otherwise
    all_musts_yes = all(values.get(m) == "Yes" for m in _MUST_KEYS)
    prefers_yes = sum(1 for p in _PREFER_KEYS if values.get(p) == "Yes")
    decline_flags = not (all_musts_yes and prefers_yes >= 3)

    # Numeric score for display purposes (Review gauge). We still compute
    # it the legacy way — weighted sum of MUST/PREFER values, 0-100 scale.
    scorer_input = {k: values[k] for k in values if k != "feasibility"}
    score, _legacy_rec = score_submission(scorer_input, decline_flags)

    # Recommendation is now driven by the explicit CHAI decision tree:
    # ANY MUST=No → Decline; ≥2 MUSTs=Partial → Decline; 1 Partial → Park;
    # all MUSTs=Yes + ≥3 PREFERs=Yes → Proceed; else → Park.
    rec = _decision_from_criteria(values)

    # SPARSE-TEXT GUARD: if the candidate text is too thin to make a fair
    # judgement (typical for listing-page anchors where we only got a
    # title), promote any Decline → Park. Rationale: when EVERY criterion
    # defaults to "No" purely because there are no positive keywords to
    # match against, we're not really declining — we're confessing
    # ignorance. Park surfaces the row for human review instead of
    # silently hiding it.
    text_chars = len((text or "").strip())
    if rec == "Decline" and text_chars < 200:
        rec = "Park"

    # MISSING-DEADLINE GUARD: many donor landing pages publish the actual
    # closing date in a banner image, an embedded calendar widget, or a
    # cross-site companion page (e.g. Fondation Pierre Fabre's call-for-
    # projects detail page where the application window only appears as
    # text inside a graphic). The regex pipeline cannot read those, so
    # the candidate currently passes is_eligible() with
    # deadline_in_future = permissive(None). That's how expired calls
    # were slipping through as "Proceed".
    #
    # Treat missing deadlines as uncertainty, not "open". Downgrade any
    # Proceed → Park when no deadline could be extracted, so the
    # reviewer manually confirms the call is still open before any
    # outbound effort. False-positive cost: a real, deadline-less call
    # lands in Park instead of Proceed (cheap to promote). False-
    # negative cost (current): expired calls land in Proceed (expensive
    # to clean up + risks the team chasing dead RFPs).
    if rec in ("Proceed", "Proceed as sub") and not candidate.get("submission_deadline"):
        rec = "Park"

    # Default CHAI role = Prime unless RFP text demands a research /
    # region-specific institution (in which case CHAI applies as Sub).
    chai_role = _detect_chai_role(text)
    # Use "Proceed as sub" so the Tracking page can distinguish role-aware
    # rows from straight Proceed ones.
    if rec == "Proceed" and chai_role == "Sub":
        rec = "Proceed as sub"

    # Vocabulary translation at the DB boundary. The scoring logic above
    # uses "Yes"/"No" (semantic — lots of comparisons depend on these
    # spellings). The UI dropdown for criterion columns uses
    # ["True", "Partial", "False"] per config/dropdowns.yaml →
    # eligibility_values. Translating here keeps the two layers in sync
    # without a mass refactor of every comparison. Feasibility is None
    # (own vocabulary High/Medium/Low) and passes through unchanged.
    db_values = {
        k: _CRITERION_DB_VOCAB.get(v, v) for k, v in values.items()
    }
    out: dict[str, Any] = {
        **db_values,
        "alignment_score": score,
        "auto_recommendation": rec,
        "decline_flags_present": decline_flags,
        # Auto-promote the recommendation into `decision` so the Tracking
        # page (filters decision IN Proceed / Proceed as sub) immediately
        # reflects post-scan triage without requiring a human click per
        # row. Reviewers can override anything on the Review tab.
        "decision": rec,
        # Default the applicant role. Park / Decline rows still get a role
        # so the team has context if they choose to review.
        "chai_role": chai_role,
    }
    # Auto-populated companion fields — only set if the candidate hasn't
    # already provided them, so explicit scraper-extracted values win.
    if not candidate.get("geographic_scope"):
        geo = _extract_geographic_scope(text, policies)
        if geo:
            out["geographic_scope"] = geo
    if not candidate.get("program_area"):
        prog = _extract_program_area(text, policies)
        if prog:
            out["program_area"] = prog
    return out
