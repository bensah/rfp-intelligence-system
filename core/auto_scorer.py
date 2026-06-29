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

from core import geographies as geo
from core.policies import CRITERION_KEYS
from core.scorer import criterion_score, score_submission

# Region/tier labels from the shared geography vocabulary — used so an admin's
# eligible-geographies selection that is a REGION (not a country) is recognised
# as a containing region in the geo gate, matching how donors tag scope.
_GEO_REGION_TIER = {s.lower() for s in (geo.UN_REGIONS + geo.INCOME_TIERS)}


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


def _geo_text(candidate: dict[str, Any]) -> str:
    """Text used for GEOGRAPHY detection only — title + description + extracted
    scope + theme, but NOT the funder name. A funder like 'African Development
    Bank' must not make an Ethiopia-specific call look Africa-wide, and a funder
    headquartered in 'United States' must not tag a global call as US-only."""
    return _normalize(
        " ".join([
            candidate.get("opportunity_title") or "",
            candidate.get("brief_description") or "",
            " ".join(candidate.get("geographic_scope") or []),
            candidate.get("focus_theme") or "",
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
    # Explicitly pairs foreign/international WITH domestic — both welcome, so
    # never a US-only restriction ("Foreign and domestic organizations …").
    r"(?:foreign|international|non[\-\s]*u\.?s\.?)\s+and\s+domestic"
    r"|domestic\s+and\s+(?:foreign|international|non[\-\s]*u\.?s\.?)"
    r"|foreign\s+(?:organizations?|entities|applicants?|institutions?)\s+(?:are\s+)?eligible"
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


# Explicit US-domestic-only eligibility statements (Grants.gov
# applicantEligibilityDesc). High precision on purpose: we only drop when the
# text plainly restricts to US/domestic applicants — never on a guess — so a
# valid USAID/CDC global-health call is never auto-dropped.
_US_DOMESTIC_ONLY_PATTERN = re.compile(
    r"(?:"
    r"\b(?:all|only)\s+domestic\b"
    r"|\bdomestic\s+(?:public\s+)?(?:and\s+)?(?:private\s+)?(?:non-?profit\s+)?"
    r"(?:for-?profit\s+)?(?:entit|organi[sz]|applicant|institution|"
    r"faith-?based|community-?based)"
    r"|\bdomestic\s+applicants\s+only\b"
    r"|\blimited\s+to\s+(?:[^.]{0,60}?)(?:domestic|united\s+states|u\.?s\.?\b)"
    r"|\bmust\s+be\s+(?:[^.]{0,40}?)(?:located|based|incorporated|organized|"
    r"domiciled)\s+(?:in|within)\s+the\s+(?:united\s+states|u\.?s\.?)"
    r"|\b(?:located|based)\s+(?:in|within)\s+the\s+united\s+states\b"
    r"|\bmust\s+be\s+(?:a\s+|an\s+)?u\.?s\.?\b"
    r"|\bu\.?s\.?\s*[-.]?\s*based\s+(?:organi|entit|applicant|institution|non)"
    r"|\bwithin\s+the\s+united\s+states\b"
    r"|(?:u\.?s\.?|united\s+states)\s+(?:entit|organi|applicant)[a-z]*\s+only"
    # Grants.gov boilerplate that DEFINES the scope as US states + territories —
    # e.g. '"Domestic" means the 50 states, the District of Columbia, …, Guam,
    # …, Palau.' (HRSA HHT-26 case.) Unambiguously US-only; an LMIC/global call
    # never says this.
    r"|[\"“”']?domestic[\"“”']?\s+means\b"
    r"|\b(?:the\s+)?(?:fifty|50)\s+states\b"
    r"|\bdistrict\s+of\s+columbia\b"
    # US-STATUTORY eligibility markers — when a call ties eligibility to a US
    # federal statute or US-only entity class, it's domestic even without the
    # word "domestic" (e.g. the CCBHC / SAMHSA case: "Indian Health Service …
    # Urban Indian Organization … Title V of the Indian Health Care Improvement
    # Act (25 U.S.C. 1601)"). India-safe by construction: a Cameroon/LMIC/global
    # call never cites the U.S. Code or these US-only Native-American bodies.
    r"|\b\d{1,3}\s+u\.?\s*s\.?\s*c\.?\s*\d"          # USC citation, "25 U.S.C. 1601"
    r"|\bunited\s+states\s+code\b"
    r"|\bindian\s+health\s+service\b"               # the US agency (singular)
    r"|\burban\s+indian\s+organization"
    r"|\bindian\s+health\s+care\s+improvement\s+act"
    r"|\bfederally\s+recognized\s+(?:indian\s+)?trib"
    r")",
    re.IGNORECASE,
)


def grants_gov_domestic_only(elig_text: str | None) -> bool:
    """True when a Grants.gov eligibility description EXPLICITLY restricts to
    US/domestic applicants AND carries no foreign/international-eligible
    statement. Used to drop clearly-US-only opps for an LMIC deployment without
    risking valid international calls (which fail this test)."""
    if not elig_text:
        return False
    if _has_inclusive_eligibility(elig_text):
        return False
    return bool(_US_DOMESTIC_ONLY_PATTERN.search(elig_text))


# Grants.gov applicant-type tiers that are structurally US-domestic government /
# public entities. If EVERY listed applicant type is one of these (and none is
# an open type below), the opp is US-government-only — not applicable to an LMIC
# NGO. Conservative: the presence of any open type keeps the opp.
_GOV_TIER_RE = re.compile(
    r"(government|school district|housing authorit|state controlled institution"
    r"|public.*institution.*higher\s+education)", re.I)
_OPEN_APPLICANT_RE = re.compile(
    r"(nonprofit|non-profit|private institution|individual|for.?profit"
    r"|small business|unrestricted|\bother)", re.I)


def grants_gov_government_only(applicant_types: list[str] | None) -> bool:
    """True when EVERY Grants.gov applicant type is a US-domestic government /
    public tier (state/county/city/tribal governments, school districts,
    housing authorities, public universities) and none is an open type
    (nonprofit, individual, for-profit, unrestricted, 'other'). A hard
    US-domestic fact — safe to drop for an LMIC deployment."""
    labels = [str(a).strip() for a in (applicant_types or []) if str(a).strip()]
    if not labels:
        return False
    if any(_OPEN_APPLICANT_RE.search(a) for a in labels):
        return False
    return all(_GOV_TIER_RE.search(a) for a in labels)


# ---------------------------------------------------------------------------
# Applicant-type match — does the call actually admit the deploying org's type?
# A call that publishes an explicit eligible-applicant list with NO open type
# and none of the org's types is structurally out of scope (e.g. a grant only
# for state governments, or for-profit small businesses, when we're an NGO).
# Canonical buckets collapse the Grants.gov enumerated types + free-text into a
# few comparable groups. "open" = unrestricted / open to any / "Others".
# ---------------------------------------------------------------------------
_APPLICANT_BUCKET_RES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("open", re.compile(
        r"unrestricted|open\s+to\s+(?:any|all)|any\s+type\s+of\s+entit"
        # the literal "Others" applicant type — but NOT the "…, other than …"
        # exclusion clause baked into the Grants.gov nonprofit/higher-ed labels.
        r"|\bothers?\b(?!\s+than\b)|see\s+(?:the\s+)?text\s+field"
        r"|no\s+restrictions?", re.I)),
    ("nonprofit", re.compile(
        r"non-?profits?|not-for-?profits?|\bn\.?g\.?o\.?s?\b|\bcbos?\b"
        r"|501\s*\(?c\)?\s*\(?3\)?|charit(?:y|ies|able)|civil\s+society", re.I)),
    ("government", re.compile(
        r"\bgovernments?\b|public\s+(?:agenc|sector|bod|entit)"
        r"|municipal|special\s+district\s+government|housing\s+authorit"
        r"|state\s+controlled", re.I)),
    ("school_district", re.compile(r"school\s+district", re.I)),
    ("higher_ed", re.compile(
        r"institutions?\s+of\s+higher\s+education|universit|college|academic\s+institut", re.I)),
    ("for_profit", re.compile(
        r"for-?profit|small\s+business|private\s+sector|\bcompan(?:y|ies)|\bfirms?\b", re.I)),
    ("individual", re.compile(r"\bindividuals?\b", re.I)),
    ("tribal", re.compile(r"\btribal\b", re.I)),
)

# Eligibility-text phrasing that means "closed to a named/existing set" — even
# when the applicant-type list looks open. High precision on purpose.
_CLOSED_TO_NAMED_RE = re.compile(
    r"\b(?:by\s+invitation\s+only"
    r"|invitation[\-\s]only"
    r"|limited\s+to\s+(?:current|existing|previously|prior)\s+"
    r"(?:grantees|recipients|awardees|partners|members)"
    r"|only\s+(?:current|existing)\s+(?:grantees|recipients|awardees)\s+"
    r"(?:may|are\s+eligible\s+to)\s+apply"
    r"|not\s+open\s+to\s+(?:new|the\s+(?:general\s+)?public)"
    r"|competition\s+is\s+limited\s+to)\b", re.I)


def _applicant_labels(candidate: dict[str, Any]) -> list[str]:
    """Pull the published eligible-applicant labels out of a candidate.

    Prefers a structured `_applicant_types` list (set by the Grants.gov
    scraper); falls back to parsing the "Eligible applicants: a; b; c" segment
    the scraper writes into `notes`."""
    raw = candidate.get("_applicant_types")
    if isinstance(raw, list) and raw:
        return [str(x).strip() for x in raw if str(x).strip()]
    notes = candidate.get("notes") or ""
    m = re.search(r"Eligible applicants:\s*(.+?)(?:\s*\|\s*|$)", notes, re.I)
    if not m:
        return []
    return [p.strip() for p in re.split(r";|·", m.group(1)) if p.strip()]


def _bucket(label: str) -> set[str]:
    return {name for name, rx in _APPLICANT_BUCKET_RES if rx.search(label)}


def applicant_type_mismatch_reject(candidate: dict[str, Any],
                                   policies: dict[str, Any]) -> tuple[bool, str]:
    """(True, reason) when a call's published eligibility EXCLUDES the deploying
    org's applicant type — either a closed-to-named-recipients phrase, or an
    explicit applicant-type list with no open type and no overlap with the org's
    own types. Conservative: silent on calls with no published list / an open
    type / an unclassifiable list (geo + theme gates still apply)."""
    elig_cfg = policies.get("eligibility") or {}
    # DEFAULT OFF (Bernard 2026-06-19): an applicant-type mismatch must NOT hard-
    # reject — it's a QUALIFICATION (MUST-1) signal computed later from org↔donor
    # eligibility, not a scan-time drop. Only rejects if a deployment explicitly
    # opts back in via policies.eligibility.reject_applicant_type_mismatch = true.
    if not elig_cfg.get("reject_applicant_type_mismatch", False):
        return False, ""
    org_buckets = {str(b).strip().lower()
                   for b in (elig_cfg.get("org_applicant_types") or ["nonprofit"])
                   if str(b).strip()}
    # Decisive "closed to a named set" wording in the eligibility text.
    elig_text = " ".join([
        candidate.get("notes") or "",
        candidate.get("brief_description") or "",
    ])
    if _CLOSED_TO_NAMED_RE.search(elig_text):
        return True, ("not open to all — restricted to a named/existing set of "
                      "recipients (invitation-only or current-grantees-only)")
    labels = _applicant_labels(candidate)
    if not labels:
        return False, ""                       # no published list → can't judge
    rfp_buckets: set[str] = set()
    for lbl in labels:
        rfp_buckets |= _bucket(lbl)
    if "open" in rfp_buckets or not rfp_buckets:
        return False, ""                       # unrestricted / unclassifiable
    if org_buckets & rfp_buckets:
        return False, ""                       # the org's type IS admitted
    pretty = "; ".join(labels[:4]) + ("; …" if len(labels) > 4 else "")
    return True, (f"applicant-type mismatch — open only to [{pretty}], not to "
                  f"{'/'.join(sorted(org_buckets)) or 'our'} applicants")


# The pure worldwide tier — applied AFTER the named-foreign check so a vague
# "global" can't rescue a call that names a specific non-eligible country
# ("Global Afghanistan Tenders" → Afghanistan, foreign). All other broad terms
# carry their own member countries and are checked before the foreign verdict.
# Includes bare legacy free-text variants so an old config that stored "global"
# / "worldwide" as separate broad terms still gets the after-foreign treatment.
_GLOBAL_TIER_LOWER = {"global / worldwide", "global", "globally", "worldwide",
                      "world wide", "international"}


# Eligibility tied EXCLUSIVELY to a place ("X-based … only", "open only to X",
# "restricted/limited to X", "must be based in X", "specific to X"). Drives the
# precedence rule: an exclusive restriction to a place outside the org's scope
# rejects even when the text also name-drops an in-region country.
_EXCLUSIVITY_RE = re.compile(
    r"(only (?:open |available )?to|open only to|restricted to|limited to"
    r"|reserved for|exclusively (?:for|to|open)|specific to"
    r"|must be (?:based|located|registered|headquartered|incorporated|established) in"
    r"|applicants? must be (?:from|based in|located in)|eligibility is limited to"
    r"|-based\s+(?:researchers?|institutions?|organi[sz]ations?|applicants?|entities|"
    r"companies|teams)[^.]{0,25}?\bonly)", re.I)
# Demonym / short-form → canonical place, so "Canadian institutions only" or
# "EU resilience" resolve to a place we can expand and test against org scope.
_DEMONYM_TO_PLACE = {
    "indian": "India", "canadian": "Canada", "chinese": "China",
    "american": "United States", "british": "United Kingdom",
    "australian": "Australia", "japanese": "Japan", "german": "Germany",
    "french": "France", "spanish": "Spain", "italian": "Italy",
    "korean": "South Korea", "swiss": "Switzerland", "european": "Europe",
    "eu": "European Union", "us": "United States", "usa": "United States",
    "uk": "United Kingdom",
}
# Places (countries + the non-M49 scopes) recognised next to an exclusivity
# phrase. Longest-first so "United States" wins over "States".
_PLACE_TOKENS = sorted(
    set(geo.COUNTRIES) | set(geo.REGION_TERMS) | set(_DEMONYM_TO_PLACE),
    key=len, reverse=True)
_PLACE_RE = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _PLACE_TOKENS) + r")\b", re.I)


# Worldwide / open-to-all eligibility — a genuine global scope. Excludes bare
# "global" on purpose (too many unit/programme names contain it).
_WORLDWIDE_RE = re.compile(
    r"\b(globally|world\s?wide|any country|all countries|across the (?:globe|world)"
    r"|open to all|all eligible countries|regardless of (?:country|location)"
    r"|international applicants?)\b", re.IGNORECASE)
# Development tiers an org's country can BELONG to (so a call open to one of these
# includes the org). Excludes the pure worldwide tier (handled by _WORLDWIDE_RE).
_DEV_TIERS = [t for t in geo.INCOME_TIERS if t != "Global / worldwide"]


def _dev_tier_mentioned(text: str) -> bool:
    """True if the text NAMES a development tier (LMIC / developing / Global South /
    LDC / low-/middle-income …). Matches the tier's label+synonyms ONLY, never its
    member countries — otherwise any African country name (an LMIC member) would
    falsely register the tier (e.g. 'Senegal health grant')."""
    tl = (text or "").lower()
    return any(re.search(r"\b" + re.escape(v), tl)
               for t in _DEV_TIERS for v in geo._region_name_variants(t))


def _org_geo_set(policies: dict[str, Any]) -> set[str]:
    """The org's geographic ANCHOR — the countries a call's scope must include to
    be in-scope. Country-precise: when eligible countries are set we anchor on
    THEM (not a broad-region expansion), so a Cameroon/Mali org accepts calls
    scoped to West/Central Africa, Sub-Saharan Africa or Africa-wide (each
    contains Cameroon or Mali) but rejects East-/Southern-/North-Africa-only calls
    (which don't). Only when no specific country is configured do we fall back to
    the selected region broad-terms. Income tiers (LMIC) never anchor — they're
    inclusive keepers handled separately."""
    countries = policies.get("countries", {}) or {}
    eligible = [c for c in (countries.get("eligible") or []) if c]
    if eligible:
        return geo.expand(eligible)            # countries expand to themselves
    region_broad = [b for b in (countries.get("broad_terms") or [])
                    if b and b not in geo.INCOME_TIERS]
    return geo.expand(region_broad)


def _place_in_org(place: str, org_set: set[str]) -> bool:
    """True if `place` (a country/region/demonym) overlaps the org's countries."""
    canon = _DEMONYM_TO_PLACE.get(place.strip().lower(), place)
    members = geo.expand([canon]) or {canon.strip().lower()}
    return bool(members & org_set)


def geographic_exclusion_reject(candidate: dict[str, Any],
                                policies: dict[str, Any]) -> tuple[bool, str]:
    """Policy-driven geo gate: a call is in-scope only if its defined geography
    INCLUDES the org's country/region (derived from the org's configured
    countries + region broad-terms). Reconfigure the org to France and only
    EU/Europe calls pass; to Morocco and only North-Africa calls pass.

    Order of decisions:
      1. Exclusive restriction to a place outside org scope → reject (this beats a
         passing in-region mention, e.g. GCGH "India-based … only" that also names
         South Africa).
      2. Inclusive wording (international / global / LMIC / a containing region) →
         keep — so a Canada/CIHR call open beyond Canada survives.
      3. Defined scope (regions/countries named) with NO overlap with org → reject.
      4. No geography at all → defer (country_eligible parks it)."""
    org_set = _org_geo_set(policies)
    if not org_set:
        return False, ""                       # org geography unconfigured → defer
    text = _geo_text(candidate)

    # 1. Exclusive-to-a-place restriction.
    for em in _EXCLUSIVITY_RE.finditer(text):
        window = text[max(0, em.start() - 30): em.end() + 50]
        pm = _PLACE_RE.search(window)
        if pm and not _place_in_org(pm.group(1), org_set):
            return True, f"geography: eligibility restricted to {pm.group(1)} (outside org scope)"

    # Detect named countries + specific regions. Country spans are blanked before
    # region detection so "South Africa"/"South Sudan" can't register a continent.
    call_countries = {m.lower() for m in _COUNTRY_PATTERN.findall(text)}
    clean = text
    for c in sorted(call_countries, key=len, reverse=True):
        clean = re.sub(r"\b" + re.escape(c) + r"\b", " ", clean)
    specific_regions = geo.regions_in_text(clean)   # UN regions + EU + Mediterranean

    # 2. A specific REGION is the hardest scope signal (it CONSTRAINS the call —
    # a development-tier word alongside is only a qualifier, e.g. "LMICs in Asia"
    # is Asia-scoped). Keep iff that region (or a named country) includes the org.
    if specific_regions:
        region_set = set()
        for region in specific_regions:
            region_set |= geo.expand([region])
        if (region_set | call_countries) & org_set:
            return False, ""
        sample = ", ".join(sorted(specific_regions)[:3])
        return True, f"geography: scope ({sample}) excludes the org's country/region"

    # 3. No specific region. Keep when the call is open to the org's DEVELOPMENT
    # TIER (LMIC / Global South / developing / LDC / low-/middle-income), worldwide,
    # or to international applicants — the org's country qualifies. A bare foreign
    # country mention (e.g. ResearchNet's funder-country 'Canada' stamp) does NOT
    # override an LMIC-open call.
    if _has_inclusive_eligibility(text) or _WORLDWIDE_RE.search(text) \
            or _dev_tier_mentioned(text):
        return False, ""

    # 4. No region, no tier, no worldwide — specific COUNTRIES constrain the scope.
    if call_countries:
        if call_countries & org_set:
            return False, ""
        sample = ", ".join(sorted(call_countries)[:3])
        return True, f"geography: scope ({sample}) excludes the org's country"

    # 5. No geography at all → defer (country_eligible parks it).
    return False, ""


def _geo_strength(candidate: dict[str, Any], policies: dict[str, Any]) -> str:
    """How well the candidate's geography matches our scope:
       'strong'   — names an eligible COUNTRY, or opens to international applicants
       'regional' — matches an admin-selected BROAD geography (region/tier) via
                    its synonyms or member countries
       'foreign'  — names a specific non-eligible country not covered by any
                    selected broad geography
       'silent'   — no geography mentioned at all

    Eligible Countries are matched EXACTLY (no region expansion). Broad
    geographies are opt-in: with none selected the gate is strict country-only.
    Each selected broad term matches via core.geographies synonyms + member
    countries. The pure worldwide tier is applied last so a vague 'global'
    can't override a named foreign country. Shared by country_eligible() +
    auto_score(). Geography is read WITHOUT the funder name (see _geo_text)."""
    countries = policies.get("countries", {}) or {}
    eligible_raw = [c for c in (countries.get("eligible") or []) if c]
    broad_raw = [b for b in (countries.get("broad_terms") or []) if b]
    # Split: region/tier labels behave as broad terms even if they were stored
    # under "eligible" by a legacy config; everything else is an exact country.
    eligible_countries = {c.lower() for c in eligible_raw
                          if c.lower() not in _GEO_REGION_TIER}
    broad_terms = broad_raw + [c for c in eligible_raw
                               if c.lower() in _GEO_REGION_TIER]
    real_broad = [b for b in broad_terms if b.strip().lower() not in _GLOBAL_TIER_LOWER]
    global_broad = [b for b in broad_terms if b.strip().lower() in _GLOBAL_TIER_LOWER]

    text = _geo_text(candidate)
    if _has_inclusive_eligibility(text):
        return "strong"
    mentioned = {m.lower() for m in _COUNTRY_PATTERN.findall(text)}
    if mentioned & eligible_countries:        # names one of our exact countries
        return "strong"
    if any(geo.text_matches_term(text, b) for b in real_broad):
        return "regional"
    if mentioned:                             # named non-eligible country → drop
        return "foreign"
    if any(geo.text_matches_term(text, b) for b in global_broad):
        return "regional"                     # worldwide tier selected + no excluding country
    return "silent"


# Individual-oriented awards — scholarships, studentships, bursaries, student
# travel/dissertation awards. These fund a NAMED PERSON, not an organization,
# so the deploying org can't apply as an entity. Checked against the TITLE
# (strongest signal) so an org grant that merely mentions "scholarships" as an
# activity isn't nuked. Kept narrow on purpose ("fellowship" alone is NOT here
# — many org/research fellowships are valid).
_INDIVIDUAL_AWARD_RE = re.compile(
    r"\b(scholarships?|studentships?|bursar(?:y|ies)|tuition|"
    r"doctoral student|ph\.?d\.? student|master'?s student|"
    r"student research award|dissertation award|undergraduate award)\b", re.I)


def individual_award_reject(candidate: dict[str, Any]) -> tuple[bool, str]:
    """(True, reason) when the call is an individual scholarship / student
    award rather than an organizational grant — drop it."""
    title = candidate.get("opportunity_title") or ""
    if _INDIVIDUAL_AWARD_RE.search(title):
        return True, "individual scholarship / student award (not an org grant)"
    return False, ""


# Employment postings (we're RFP-focused, not a job board) — detected on the
# TITLE or URL, never the body, so a real call that merely mentions "jobs" (a
# youth-livelihoods RFP) isn't caught. NOTE: "consultancy / consultant /
# recruitment of a firm" is deliberately NOT here — those are valid procurement
# opportunities the team pursues.
_JOB_TITLE_RE = re.compile(
    r"\b(jobs?|vacanc(?:y|ies)|we are hiring|now hiring|join our team|"
    r"job (?:opening|posting|search)|\d+\s+job position)\b", re.I)
_JOB_URL_RE = re.compile(
    r"(?:\.jobs(?:/|$)|/jobs?(?:/|\b|-)|/vacanc|/careers?\b|//jobs\.)", re.I)
# Clearly non-funding page types (NOT blog/news — real calls live there).
_NON_FUNDING_RE = re.compile(
    r"\b(standardized testing|report card|course catalog|academic calendar|"
    r"school district|cookie policy|privacy policy|terms of use|log ?in|"
    r"frequently asked|faqs?)\b", re.I)
# Press-release / news-wire aggregators re-publish announcements — they aren't
# the call's own page (e.g. miragenews' NYC-bathrooms story). High noise → drop.
_NEWSWIRE_RE = re.compile(
    r"(miragenews|prnewswire|businesswire|globenewswire|einnews|openpr|"
    r"prweb|newswire\.|/press-release)", re.I)


def non_funding_reject(candidate: dict[str, Any]) -> tuple[bool, str]:
    """(True, reason) for job/vacancy postings and other clearly non-funding
    pages (course/policy/login). Job detection is guarded by an RFP signal in
    the title so a genuine call about jobs/livelihoods survives."""
    title = candidate.get("opportunity_title") or ""
    link = candidate.get("opportunity_link") or ""
    has_rfp = (any(p in title.lower() for p in _RFP_STRONG_PHRASES)
               or _has_rfp_acronym(title))
    if (_JOB_TITLE_RE.search(title) or _JOB_URL_RE.search(link)) and not has_rfp:
        return True, "job / vacancy posting (not a funding call)"
    if _NEWSWIRE_RE.search(link):
        return True, "press-release / news-wire aggregator (not the call source)"
    # Normalise URL separators (-, _, /) → spaces so "standardized-testing" or
    # "/faqs" in a path matches the same as the words in a title.
    norm = f"{title} {re.sub(r'[-_/]+', ' ', link)}"
    if _NON_FUNDING_RE.search(norm):
        return True, "non-funding page (FAQ / course / policy / login)"
    return False, ""


# Pages that rendered an ERROR (the crawl reached a generic error / 404 / "site
# unavailable" template, not the real call). Title + description only, with
# high-precision phrases so legit RFP prose isn't caught.
_ERROR_PAGE_RE = re.compile(
    r"(the\s+system\s+has\s+encountered\s+an\s+error"
    r"|an?\s+(?:unexpected\s+)?error\s+has\s+occurred"
    r"|error\s+occurred\s+while\s+processing"
    r"|404\s*[-—:]?\s*(?:error|page\s+not\s+found|not\s+found)"
    # "Page not found", and the WordPress/CMS soft-404 body that returns 200 but
    # says the post is gone — e.g. healthresearch.org: "No Results Found · The
    # page you requested could not be found. Try refining your search…".
    r"|page\s+(?:you\s+(?:requested|are\s+looking\s+for)\s+)?"
    r"(?:cannot|can'?t|could\s+not|was\s+not|doesn'?t|does\s+not)\s+(?:be\s+)?"
    r"(?:found|exist)"
    r"|page\s+not\s+found"
    r"|\bno\s+results?\s+found\b"
    r"|try\s+refining\s+your\s+search"
    r"|use\s+the\s+navigation\s+above\s+to\s+locate"
    r"|service\s+(?:temporarily\s+)?unavailable"
    r"|this\s+page\s+(?:isn'?t|is\s+not)\s+working"
    r"|exceeding\s+(?:its\s+)?recaptcha\b[^.]*quota"
    r")", re.I)


def error_page_reject(candidate: dict[str, Any]) -> tuple[bool, str]:
    """(True, reason) when the fetched page is an error / unavailable template
    rather than a real opportunity (e.g. the ResearchNet 'system has encountered
    an error' page from a stale/broken detail URL).

    Honors a `_dead_page` flag set by the liveness check (core.live_check) /
    scrapers when a fetch returned a 404/410 or a soft-404 body — the strongest
    'this link is dead' signal, independent of the title/description text."""
    if candidate.get("_dead_page"):
        return True, candidate.get("_dead_reason") or "dead link (page gone / error)"
    blob = " ".join([
        candidate.get("opportunity_title") or "",
        candidate.get("brief_description") or "",
        candidate.get("notes") or "",
    ])
    if _ERROR_PAGE_RE.search(blob):
        return True, "error / unavailable page (not a real opportunity)"
    return False, ""


# Title-level signals for opportunity TYPES many implementing orgs (e.g. the organisation)
# don't pursue, BOTH configurable via policies['exclusions'] so an org that DOES
# want them just turns the flag off:
#   * training / education programs — capacity-building of named trainees, not a
#     grant to implement a project.
#   * loans / debt instruments — orgs that implement programs want grants/awards,
#     not money they must repay.
_TRAINING_TITLE_RE = re.compile(
    r"\b(?:"
    r"training\s+(?:cent(?:er|re)s?|programs?|programmes?|institutes?|hubs?)"
    r"|(?:clinical|residency|resident|faculty|student|nurse|nursing|physician|"
    r"medical|workforce|fellowship|preceptor|scholar)\s+(?:training|education)\b"
    r"|(?:student|medical|nursing|graduate|undergraduate|health\s+professions?)"
    r"\s+education\s+programs?"
    r"|education\s+and\s+training\s+programs?"
    r"|faculty\s+development\s+programs?"
    r")", re.I)
_LOAN_TITLE_RE = re.compile(
    r"\b(?:loans?|loan\s+repayment|concessional\s+(?:loan|lending|finance))\b", re.I)
# Individual consultant / contractor procurement — the applicant is a person or
# firm hired to deliver a service, not an org receiving a project grant. Default
# ON; an org that pursues consultancies turns it off in Settings.
_CONSULTANCY_TITLE_RE = re.compile(
    r"\b(?:consultanc(?:y|ies)|consultants?|consulting\s+(?:services?|firm)"
    r"|(?:individual|external|technical|independent)\s+consultant"
    r"|contractors?|recruitment\s+of\s+(?:a\s+)?(?:consultant|firm|individual)"
    r"|provision\s+of\s+consult)", re.I)
# Reimbursement programs (e.g. "Ryan White HIV/AIDS Program Part F Dental
# Reimbursement Program") repay incurred costs to a closed set of named existing
# providers/grantees — a domestic scheme, not an open competitive grant. Default
# ON; title-scoped so legit calls that merely offer "travel reimbursement" in
# their body aren't caught.
_REIMBURSEMENT_TITLE_RE = re.compile(r"\breimbursement\b", re.I)


def non_grant_reject(candidate: dict[str, Any],
                     policies: dict[str, Any]) -> tuple[bool, str]:
    """(True, reason) for opportunity TYPES the org has opted out of via
    policies['exclusions'] — training/education programs and loans. Title-based
    (a project grant rarely IS titled 'X Training Center' / 'Loan Program').
    Both default ON; an org that wants them sets the flag false in Settings."""
    excl = policies.get("exclusions") or {}
    title = candidate.get("opportunity_title") or ""
    if excl.get("reject_loans", True) and _LOAN_TITLE_RE.search(title):
        return True, "loan / debt instrument (org seeks grants & awards, not loans)"
    if excl.get("reject_training_only", True) and _TRAINING_TITLE_RE.search(title):
        return True, "training / education program (not a project grant)"
    if excl.get("reject_consultancies", True) and _CONSULTANCY_TITLE_RE.search(title):
        return True, "consultancy / individual-contractor procurement (org seeks grants)"
    if excl.get("reject_reimbursement", True) and _REIMBURSEMENT_TITLE_RE.search(title):
        return True, ("reimbursement program (repays named existing providers for "
                      "incurred costs — a closed scheme, not an open grant)")
    return False, ""


def country_eligible(candidate: dict[str, Any], policies: dict[str, Any]) -> tuple[bool, str]:
    """Geo gate — PARK the ambiguous, REJECT the clearly-out-of-scope.

    Per the geo rule:
      * No geography defined (silent) → ENTER, parked for review (slip in).
      * Defined scope that includes our eligible countries OR a region/tier
        that contains them (sub-Saharan Africa, LMIC, Africa, …) → ENTER.
      * Defined scope that EXCLUDES us — specific non-eligible countries with
        no containing region (e.g. a Ukraine/China-only call, the Canada Fund
        per-country list) → REJECT (drop).
    Ambiguity (regional / silent) lands in Park via the auto_score geo guard;
    a clearly-eligible match can Proceed. Honors permissive_when_silent.
    Returns (eligible, reason) for the scan log.
    """
    countries = policies.get("countries", {}) or {}
    permissive = bool(countries.get("permissive_when_silent", True))
    strength = _geo_strength(candidate, policies)
    if strength == "strong":
        return True, "geo: eligible country / open to international applicants"
    if strength == "regional":
        return True, "geo: regional / LMIC scope (parked for review)"
    if strength == "silent":
        if permissive:
            return True, "geo: none mentioned (permissive)"
        return False, "geo: none mentioned (strict)"
    # strength == "foreign": the legacy detector saw a non-eligible country, but
    # it's blind to broad containing regions stated by name (a call scoped to
    # "Africa, Latin America" reads as foreign here even though Africa contains
    # Cameroon/Mali). Defer the verdict to the authoritative, policy-driven
    # geographic_exclusion_reject — it rejects a scope that genuinely excludes the
    # org and keeps one that includes an in-region area. This reconciles the two
    # gates so e.g. Grand Challenges Canada's Africa/LatAm/Caribbean call is kept.
    rej, reason = geographic_exclusion_reject(candidate, policies)
    if rej:
        return False, reason
    return True, "geo: mixed scope includes an in-region area (parked)"


def _theme_hit(kw: str, text: str) -> bool:
    """Word-aware keyword match (NOT a bare substring — that let 'TB' match inside
    'I-TB'). Short tokens & acronyms (<=5 chars or ALL-CAPS, e.g. TB, STI, flu,
    UHC) require a WHOLE-word boundary so they don't fire inside a longer word
    ('still', 'fluid', 'itb'). Longer lowercase terms match as a PREFIX so stems
    still work ('immuni'→'immunization', 'diagnostic'→'diagnostics')."""
    kw = (kw or "").strip()
    if not kw:
        return False
    end = r"\b" if (len(kw) <= 5 or kw.isupper()) else ""
    return bool(re.search(r"\b" + re.escape(kw) + end, text, re.IGNORECASE))


def theme_eligible(candidate: dict[str, Any], policies: dict[str, Any],
                   *, llm_theme: bool = False) -> tuple[bool, str]:
    themes = policies.get("themes", {}) or {}
    required = themes.get("required_any") or []
    excluded = themes.get("excluded_any") or []
    text = _full_text(candidate)
    title = candidate.get("opportunity_title") or ""
    req_hits = [kw for kw in required if kw and _theme_hit(kw, text)]
    req_hit = bool(req_hits)
    exc_in_title = any(_theme_hit(e, title) for e in excluded if e)
    exc_in_body = any(_theme_hit(e, text) for e in excluded if e)

    def _regex_verdict() -> tuple[bool, str]:
        # An excluded term is a HARD reject only when the call is actually ABOUT
        # it (term in the TITLE) OR when the call is off-theme anyway (no required
        # match). An INCIDENTAL body mention inside an on-theme call is NOT a
        # reject — e.g. a global-health RFP that name-drops "clinical trial and
        # regulatory infrastructure" isn't a Phase-II clinical-trial call.
        if excluded and (exc_in_title or (exc_in_body and not req_hit)):
            return False, "matches excluded theme"
        if not required:
            return True, "no theme requirements set"
        if req_hit:
            return True, "matches required theme keyword"
        return False, "no required theme keyword matched"

    # LLM adjudication on the AMBIGUOUS cases only — where substring matching is
    # unreliable: a conflict (excluded AND required both hit), or a THIN required
    # match (≤2 distinct keywords) that may be incidental (e.g. an animal-ag RFP
    # matching "influenza"/"pandemic"). The judge reads the whole page and rules
    # on-theme vs incidental. Cheap: the verdict is content-hash cached, so the
    # enrichment call in core.extract reuses it. Gated to the extraction gate
    # (llm_theme=True) so tenant screening stays regex-fast. A clear regex verdict
    # (no required hit at all, or a strong ≥3-keyword match) skips the LLM.
    ambiguous = req_hit and ((exc_in_title or exc_in_body) or len(req_hits) <= 2)
    if llm_theme and ambiguous:
        try:
            from core import llm_judge
            if llm_judge.is_enabled():
                body = (candidate.get("_page_text")
                        or candidate.get("brief_description") or "")
                j = llm_judge.judge(
                    {"opportunity_title": title,
                     "opportunity_link": candidate.get("opportunity_link"),
                     "brief_description": body}, policies)
                if j and j.get("theme_relevant") is True:
                    return True, "theme (LLM): on-theme"
                if j and j.get("theme_relevant") is False:
                    return False, "theme (LLM): off-theme (incidental keyword)"
        except Exception:
            pass
    return _regex_verdict()


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
    # Title/snippet shorthand: "(2026, now closed)", "(closed)", "— now closed".
    r"|\bnow\s+closed\b"
    r"|\(\s*closed\s*\)"
    # Status badge "Closed call" / "Call closed" (research.swiss etc.).
    r"|\bclosed call\b"
    r"|\bcall closed\b"
    r"|\bstatus:\s*closed\b"
    # "This RFP closed on May 11, 2026" + the thank-you-to-applicants wrap-up
    # (Coefficient Giving biosecurity case). Date-anchored → high precision.
    r"|\b(?:rfp|rfa|rfq|call|round|competition|programme?|program|fund|window)\s+"
    r"(?:is\s+|was\s+|has\s+(?:been\s+)?|now\s+)?closed\b"
    r"|\bclosed\s+on\s+[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}"
    r"|thank\s+(?:everyone|you|all|those)[^.]{0,60}?"
    r"(?:who\s+)?(?:submitted|applied|expressed\s+interest)"
    # Past-tense "this round is over" language (Global Affairs Canada case:
    # "the assessment of the proposals … has concluded and applicants have
    # been informed of their results").
    r"|(?:assessment|review|evaluation|adjudication) of (?:the )?"
    r"(?:proposals?|applications?|submissions?)[^.]{0,60}?has (?:concluded|ended|been completed)"
    r"|applicants? have been (?:informed|notified) of (?:their|the)\s+results?"
    r"|this (?:call|competition|round|process)\b[^.]{0,30}?(?:has|is) (?:now )?(?:concluded|ended)"
    r"|(?:call|competition) (?:for proposals? )?(?:has|is) (?:now )?(?:concluded|closed|ended)"
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


# US-domestic-only signal — the decisive geography reject for a non-US
# deployment. See docs/SCAN_CLASSIFICATION_ALGORITHM.md §6 (the "domestic"
# test from HRSA-26-083: "All domestic public or private … entities").
# The pattern itself (`_US_DOMESTIC_ONLY_PATTERN`) is defined once, up near
# `grants_gov_domestic_only`, and is reused here so the scraper-side drop and
# this gate-side reject stay in lock-step.
def us_domestic_only_reject(candidate: dict[str, Any], policies: dict[str, Any]) -> tuple[bool, str]:
    """Reject US-domestic-only opportunities for a non-US deployment.

    US-federal NOFOs frequently restrict eligibility to "domestic" (US-based)
    entities — decisive for a non-US deployment (a country office can't apply as
    prime). The signal lives in the eligibility text, which the grants.gov
    scraper now captures into `notes` (applicantEligibilityDesc); `_full_text`
    excludes notes, so we read it explicitly here.

    Skipped when the deploying org IS a US entity (`org_is_us_entity` setting)
    or when the RFP carries an explicit inclusive-foreign statement.
    """
    try:
        from core import settings as _settings
        us_entity = str(
            _settings.get_setting("org_is_us_entity", "false")
        ).strip().lower() in ("true", "yes", "1")
    except Exception:
        us_entity = False
    if us_entity:
        return False, ""
    text = _full_text(candidate) + " " + (candidate.get("notes") or "")
    if _has_inclusive_eligibility(text):
        return False, ""
    # Structured signals from the grants.gov enricher (set on the candidate):
    #   * _drop_us_only — eligibility text or applicant types were US-domestic.
    #   * _applicant_types — re-check gov-only here so US-government-only opps that
    #     arrive via OTHER paths (web search, listing-children) are caught too.
    if candidate.get("_drop_us_only") \
            or grants_gov_government_only(candidate.get("_applicant_types")):
        return True, "US-domestic-only eligibility — out of scope for a non-US deployment"
    if _US_DOMESTIC_ONLY_PATTERN.search(text):
        return True, "US-domestic-only eligibility — out of scope for a non-US deployment"
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


# A non-rolling call with NO parseable deadline whose page was POSTED more than
# this many days ago is almost certainly closed — real application windows run
# weeks to a few months, not years. Rolling / open-ended calls are exempt (they
# say so in the text). Conservative end of the user's 3-6 month guidance.
_STALE_POSTING_DAYS = 183  # ~6 months
_ROLLING_RE = re.compile(
    r"(rolling\s+basis|on\s+a\s+rolling|no\s+(?:fixed\s+|set\s+)?deadline"
    r"|deadline\s*:?\s*(?:none|n/?a|ongoing|rolling|continuous)"
    r"|accepted\s+(?:on\s+an?\s+)?(?:ongoing|continuous|year[- ]?round|rolling)"
    r"|year[- ]round|continuous(?:ly)?\s+(?:open|accept|intake)"
    r"|always\s+open|at\s+any\s+time|ongoing\s+(?:basis|intake)"
    r"|applications?\s+(?:are\s+)?accepted\s+(?:at\s+any\s+time|on\s+a\s+rolling|continuous))",
    re.IGNORECASE)


def _is_rolling_call(candidate: dict[str, Any]) -> bool:
    """True if the text states the call is rolling / open-ended (no fixed
    deadline). Such calls are EXEMPT from the stale-posting rule."""
    blob = " ".join([
        candidate.get("opportunity_title") or "",
        candidate.get("brief_description") or "",
        candidate.get("notes") or "",
    ])
    return bool(_ROLLING_RE.search(blob))


def deadline_in_future(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Reject candidates whose submission deadline has already passed.

    Sources of truth, in priority order:
      1. Explicit `submission_deadline` set by the enrichment pipeline.
      2. Stale-posting rule — no deadline, NOT rolling, and the page was posted
         more than _STALE_POSTING_DAYS ago → expired (FPF 'Seven Winners' 2017).
      3. Latest year in link/title/body — fallback when nothing else parsed.
      4. No signal at all → keep (rolling / undated RFP).
    """
    from datetime import date as _date, datetime as _dt
    today = _date.today()
    deadline = candidate.get("submission_deadline")
    if not deadline:
        # Stale-posting rule: a non-rolling call with no deadline whose page was
        # POSTED long ago is almost certainly closed (e.g. the FPF 'Seven
        # Winners' page — published 2017, no deadline text, no 'closed' clue).
        posted = candidate.get("date_posted")
        if isinstance(posted, str):
            try:
                posted = _dt.fromisoformat(posted.split("T")[0]).date()
            except (ValueError, TypeError):
                posted = None
        elif isinstance(posted, _dt):
            posted = posted.date()
        if (isinstance(posted, _date) and posted < today
                and (today - posted).days > _STALE_POSTING_DAYS
                and not _is_rolling_call(candidate)):
            return False, (
                f"posted {posted.isoformat()} ({(today - posted).days}d ago), "
                "no deadline + not a rolling call — treating as expired")
        # Fallback: look for a year in the URL or title.
        # Scan URL + title AND the body text / notes. Donors like Fondation
        # Pierre Fabre put the application window only in prose ("accepting
        # submissions through 30 January 2018") with no year in the URL or
        # title — so a URL/title-only scan missed them and they leaked through
        # as Park. If the LATEST year mentioned ANYWHERE is in the past (i.e.
        # the page cites no current/future year), treat the call as expired.
        blob = " ".join([
            candidate.get("opportunity_link") or "",
            candidate.get("opportunity_title") or "",
            candidate.get("brief_description") or "",
            candidate.get("notes") or "",
        ])
        yr = _latest_year_in(blob)
        if yr and yr < today.year:
            return False, (
                f"latest year on page is {yr} (past) and no explicit deadline "
                "parsed — treating as expired"
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

# Listing / index pages that ENUMERATE calls (e.g. AFD
# /en/calls-for-projects/list?status[ongoing]=... ). These must never be posted
# as a record — they are crawl seeds; the scanner extracts the individual call
# pages from them. A real call is a specific slug (/calls-for-projects/<title>),
# not /list, /all, a status-filtered query, or a paginated/faceted index.
_LISTING_URL_RE = re.compile(
    r"(?:"
    r"/list(?:/|\?|$)"                     # .../calls-for-projects/list ; /list?…
    r"|/all(?:/|\?|$)"                     # .../grants/all
    r"|/archive[sd]?(?:/|\?|$)"            # .../archive , /archived
    r"|/explore(?:/|\?|$)"                 # opendata /explore
    r"|[?&](?:status|statut)(?:%5b|\[)"    # ?status[ongoing]=…  (filtered list)
    r"|[?&]page=\d"                        # paginated index
    r"|[?&]disjunctive\."                  # faceted catalog listing
    r")",
    re.IGNORECASE,
)

# A TITLE that is ONLY a generic calls-section heading ("Calls for projects",
# "Funding opportunities", "Open calls") is a listing index, not a single call.
# Anchored ^...$ so a specific call ("Call for Proposals: <subject>", "...2026
# call for project proposals") is NOT matched (it has a subject beyond the
# heading).
_LISTING_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"(?:calls?|requests?|notices?|invitations?)\s+for\s+"
    r"(?:projects?|proposals?|applications?|tenders?|grants?"
    r"|expressions?\s+of\s+interest|concept\s+notes?)"
    # Optional leading modifier (current/latest/open/…) + funding-opportunities,
    # so 'Current funding opportunities' / 'Latest grants' are caught.
    r"|(?:current|latest|open|available|new|active|upcoming|recent|all|explore)?\s*"
    r"(?:funding|grant|grants|financing)\s+opportunit(?:y|ies)"
    r"|open\s+calls?|current\s+(?:calls?|opportunit(?:y|ies))"
    r"|all\s+(?:calls?|grants?|opportunit(?:y|ies))"
    # Verb-led index headings: "Find a funding opportunity", "Search grants",
    # "Browse opportunities", "View / explore funding opportunities".
    r"|(?:find|search|browse|explore|view|see|discover)\s+"
    r"(?:a|an|all|our|the|for|your)?\s*(?:funding\s+|grant\s+|open\s+)?"
    r"(?:opportunit(?:y|ies)|calls?|grants?|projects?|tenders?|fund(?:ing|s)?)"
    r")\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# RFP-signal gate — is this actually a funding CALL, or a process / info /
# error page that merely lives on a donor's site? This is the #1 false-positive
# source: hub pages like "grant life cycle", "Annual Funding Decisions and
# Disbursements", "TAIEX", "complementary financing" that carry no real call.
#
# A candidate PASSES this gate iff:
#   • it has STRONG call wording (an RFP/EOI/NOFO-class phrase or acronym), OR
#   • it has WEAK/generic call wording AND concrete request details
#     (a submission deadline or an award amount), OR
#   • it comes from a funding-specific source (NOT an open-web/Google search)
#     AND carries concrete request details.
# It is REJECTED for: error / unavailable pages; process/info/reporting pages
# that lack strong call wording; or anything with no call signal at all.
# The opportunity URL is folded in (hyphens/slashes -> spaces) so a path like
# /grant-life-cycle/ or /…-complementary-financing trips the same patterns.
# ---------------------------------------------------------------------------
_RFP_STRONG_PHRASES = (
    "request for proposal", "request for proposals",
    "request for application", "request for applications",
    "request for information",
    "request for expression of interest", "request for expressions of interest",
    "call for expression of interest", "call for expressions of interest",
    "call for proposal", "call for proposals", "calls for proposals",
    "call for application", "call for applications", "calls for applications",
    "call for concept note", "call for concept notes",
    "request for concept note", "request for concept notes",
    "call for project", "call for projects", "calls for projects",
    "call for nominations", "call for entries", "call for submissions",
    "notice of funding opportunity", "funding opportunity announcement",
    "notice of funding availability", "annual program statement",
    "broad agency announcement", "invitation to tender",
    "request for tender", "tender notice", "invitation to bid",
    "call for tender", "call for tenders", "request for quotation",
    "grand challenge",
    # Stand-alone full wordings (not only the "request/call for …" prefixed forms).
    "expression of interest", "expressions of interest",
    "letter of intent", "letters of intent", "tender notice", "tenders",
    # Dev-bank procurement notices (AfDB / World Bank style).
    "specific procurement notice", "general procurement notice",
    "procurement notice", "invitation for bids", "prequalification",
)
# Whole UPPERCASE acronyms only (RFPs/NOFOs plural handled by the regex).
_RFP_ACRONYMS = frozenset({
    "RFP", "RFA", "RFI", "RFQ", "EOI", "REOI", "CEOI", "LOI",
    "CFP", "CFA", "NOFO", "NOFA", "FOA", "APS", "BAA", "ITT", "ITB",
    "SPN", "GPN", "IFB",          # specific/general procurement notice, inv. for bids
})
_ACRONYM_TOKEN_RE = re.compile(r"\b([A-Z]{2,6})s?\b")
# Generic / ambiguous wording — a call MIGHT be here; accept only WITH details.
_RFP_WEAK_PHRASES = (
    "funding opportunity", "funding opportunities",
    "grant opportunity", "grant opportunities",
    "open call", "open for applications", "now accepting applications",
    "accepting applications", "applications are open",
    "apply now", "submit a proposal", "submit your proposal",
    "submit a concept note", "application deadline", "deadline to apply",
    "closing date",
    # Awards / prizes are valid opportunity types (Zayed Sustainability Prize,
    # etc.). Weak tier → accepted only WITH details (a deadline or an amount),
    # so closed "contract award" pages still fall to the deadline/past gates.
    "award", "awards", "prize", "prizes",
    "opens applications", "applications now open", "submissions open",
    "now accepting submissions", "accepting submissions",
    "now open for submissions", "open for submissions",
)
# Process / informational / navigational PAGE-TYPE signals — NOT calls. These
# are matched against the TITLE + URL only (the decisive page-type signal); a
# real call would carry RFP/CFP/EOI wording in its title. Matched even when the
# page BODY mentions FOAs/NOFOs (e.g. a CDC "grant life-cycle overview" that
# merely describes the funding process — the title gives it away).
_NON_RFP_PATTERNS = (
    "grant life cycle", "grant lifecycle", "life cycle", "lifecycle",
    "applying for funding", "grant cycle", "grant process", "grants process",
    "annual funding decision", "decisions and disbursement", "disbursement",
    "principal recipient", "recipient reporting", "recipients", "recipient",
    " reporting", "grant implementation", "implementation toolkit", "toolkit",
    "guideline", "guidance", "how to apply", "how we work", "welcome packet",
    "prepare to apply", "eligibility information", "application package",
    "resources for", "frequently asked", "results framework", "track record",
    "complementary financing", "source of financing", "blended finance",
    "technical assistance", "information exchange",
    "past project", "completed project", "previously funded",
    "overview of", "process overview", "our grantees", "grantee report",
    "awarded grant", "annual report", "fact sheet", "factsheet",
    "case study", "case studies", "agenda", "innovation agenda",
    "strategic shift", "strategic plan", "our strategy",
    "glossary", "terms and conditions", "privacy", "personal data",
    "data protection", "press release", "newsletter", "blog",
    "webinar", "who we are", "about us", "contact us", "careers",
    "document library", "publication", "policy brief", "knowledge hub",
    "matching fund", "investment case", "financial management",
    "annual meeting",
)
_ERROR_PAGE_PATTERNS = (
    "page not found", "404 not found", "error 404", "404 error",
    "page doesn't exist", "page does not exist",
    "page can't be found", "page cannot be found",
    "page you are looking for", "page you requested",
    "no longer available", "access denied", "403 forbidden",
    "service unavailable", "temporarily unavailable",
    "something went wrong", "this page is unavailable",
    "bad gateway", "internal server error",
)


def _has_rfp_acronym(raw_text: str) -> bool:
    """True if a whole UPPERCASE RFP-class acronym (RFP, EOI, NOFO, …) appears.
    Case-sensitive on purpose — these are shouted in the wild; a lower-case
    'foa'/'aps' inside an ordinary word must not trigger."""
    return any(m.group(1) in _RFP_ACRONYMS
               for m in _ACRONYM_TOKEN_RE.finditer(raw_text or ""))


def _has_request_details(candidate: dict[str, Any]) -> bool:
    """Concrete signs of an actual call: a submission deadline or an award
    amount. (Description alone doesn't count — hub pages have descriptions.)"""
    if candidate.get("submission_deadline"):
        return True
    ev = candidate.get("estimated_value")
    try:
        return ev not in (None, "", 0, "0") and float(ev) > 0
    except (TypeError, ValueError):
        return False


def _is_open_web_source(candidate: dict[str, Any]) -> bool:
    """True for open-web search results (Google Alerts) — these need explicit
    call wording, not just a deadline, because the web is full of donor pages
    that aren't calls. Funding-specific feeds (grants.gov, NIH, ReliefWeb) and
    curated donor listing pages are trusted to be funding-oriented."""
    return "google alert" in (candidate.get("_source_origin") or "").lower()


def rfp_signal_gate(candidate: dict[str, Any]) -> tuple[bool, str]:
    """Composite verification that this is a real funding CALL. See header.

    Order matters: error -> page-type (title/URL) -> title call-wording ->
    body call-wording + details -> generic wording + details -> trusted
    funding source + details -> reject. The page TYPE (from title + URL) is
    decisive: a process/overview page that merely mentions 'FOA'/'NOFO' in its
    body is still rejected."""
    title = candidate.get("opportunity_title") or ""
    desc = candidate.get("brief_description") or ""
    notes = candidate.get("notes") or ""
    link = candidate.get("opportunity_link") or ""
    link_words = re.sub(r"[-_/]+", " ", link.lower())
    title_url = f"{title.lower()} {link_words}"               # page-type signal
    body = "\n".join([title.lower(), desc.lower(), notes.lower()])

    # 1. Error / unavailable page.
    if candidate.get("_error_page") or any(
        p in body or p in link_words for p in _ERROR_PAGE_PATTERNS
    ):
        return False, "error / unavailable page (not a live RFP)"

    # Does the TITLE/URL itself carry explicit call wording?
    title_strong = (any(p in title_url for p in _RFP_STRONG_PHRASES)
                    or _has_rfp_acronym(title))

    # 2. Page-type screen: a process / informational / navigational page (from
    #    its title or URL) with NO call wording in the title is rejected — even
    #    if the body text mentions FOAs/NOFOs while describing the process.
    if any(p in title_url for p in _NON_RFP_PATTERNS) and not title_strong:
        return False, "process / informational page (title/URL is not a call)"

    # 3. Explicit call wording in the title/URL → it's a call.
    if title_strong:
        return True, ""

    details = _has_request_details(candidate)
    open_web = _is_open_web_source(candidate)

    # 4. Call wording in the body + concrete request details (deadline/amount).
    body_strong = (any(p in body for p in _RFP_STRONG_PHRASES)
                   or _has_rfp_acronym(f"{title} {desc}"))
    if body_strong and details:
        return True, ""
    # 5. Generic/weak call wording + concrete details.
    if any(p in body for p in _RFP_WEAK_PHRASES) and details:
        return True, ""
    # 6. Trusted funding-specific source (not open-web) + concrete details.
    if details and not open_web:
        return True, ""
    return False, "no valid RFP signal (no call wording, deadline, or award amount)"


# News / press / blog / story pages ABOUT an opportunity are not the call itself
# (e.g. zayedsustainabilityprize.com/en/news). Reject as not-an-rfp.
# Boundary allows "-" so a hyphenated section slug like "/news-resources-search/"
# (MMV's news hub) and "/interview-..." (interview write-ups) are caught, not just
# "/news/". These are pages ABOUT a call/grantee, never the call's own page.
_NEWS_URL_RE = re.compile(
    r"/(news|press|press-release|media|newsroom|blog|stories|story|article"
    r"|interview|interviews)(?:/|-|$|\?|\.)",
    re.IGNORECASE)
_NEWS_TITLE_RE = re.compile(
    r"^\s*(news|press release|blog|in the news|newsroom|interview)\b[\s\-:|]",
    re.IGNORECASE)
# Archive / past-call URL paths — a specific PAST call's detail page (not a listing).
# e.g. GACD /funding/past-calls-for-applications/<slug>. Always closed.
_PAST_CALL_URL_RE = re.compile(
    r"/(past[-_]call|closed[-_]call|calls?[-_]archive|past[-_]opportunit"
    r"|past[-_]funding|past[-_]grant)", re.IGNORECASE)
# Resource-mobilisation / "invest IN us" donor pages — the org is being asked to
# fund the donor, the OPPOSITE of a grant call. e.g. gavi.org/investing-gavi/...
# "Gavi's Investment Opportunity 2026-2030". Not a solicitation.
_DONOR_INVEST_URL_RE = re.compile(
    r"/(investing[-_]\w+|resource[-_]mobili[sz]ation|our[-_]donors"
    r"|donor[-_]profile|funding[-_]overview|investment[-_]case)", re.IGNORECASE)
_DONOR_INVEST_TITLE_RE = re.compile(
    r"\b(investment opportunity|investment case|resource mobili[sz]ation"
    r"|donor profile|funding overview|invest in (?:gavi|us|our))\b", re.IGNORECASE)
# Internal / off-mission award types — course (teaching) grants, core/shared
# research-facility & instrumentation support, "academy award" staff/faculty
# programmes (e.g. Stanford "Cardinal Course Grants", "C-ShaRP Academy Award").
# These are not health-PROGRAMME solicitations an external org can pursue; they
# slip the theme gate because the host portal's boilerplate mentions health.
_OFF_MISSION_AWARD_RE = re.compile(
    r"\b(course grants?|core facilit|shared facilit|instrumentation grant"
    r"|academy award|curriculum grant|teaching grant)\b", re.IGNORECASE)
# Announcement of a FUTURE / upcoming call — topics or plans for a later cycle,
# not an open call now (e.g. GACD "Future GACD funding call topics 2025 to 2027").
# These are valuable for a future public "Announcements" section but must NOT enter
# as live RFPs. Matched on the TITLE to stay conservative (a live call may mention
# future dates in its body). opportunity_type = "announcement" downstream.
_FUTURE_ANNOUNCE_RE = re.compile(
    r"\b(future\b[^.]{0,40}\bcall topics?"
    r"|call topics?\b[^.]{0,20}\b20\d\d"
    r"|upcoming\s+(?:call|funding|grant|competition|opportunit)"
    r"|forthcoming\s+(?:call|funding|grant|competition)"
    r"|future\s+funding\s+(?:call|round|opportunit))",
    re.IGNORECASE)


# Coefficient Giving fund-overview parent page: /funds/<slug>/ with no further
# sub-path. The real call always lives one level deeper (a "Request for Proposals"
# tab), so the bare fund page is never an RFP.
_CG_FUND_PARENT_RE = re.compile(
    r"coefficientgiving\.org/funds/[a-z0-9-]+/?(?:[?#].*)?$", re.I)


def is_eligible(candidate: dict[str, Any], policies: dict[str, Any],
                *, geo_org_gates: bool = True,
                llm_adjudicate: bool = False,
                llm_theme: bool = False) -> tuple[bool, str]:
    """Combined gate: search-URL, language, feasibility, deadline, country,
    theme. Logged in scan output for transparency.

    `geo_org_gates=False` skips the GEOGRAPHY + ORG-relative gates (US-domestic,
    geographic-exclusion, applicant-type, country, feasibility) so the same gate
    can serve the EXTRACTION stage (DATA_SCHEMA_ETL.md §3): geography moves to the
    per-tenant scorer (tier 2), and the global store keeps every geography. The
    keep-set (not-an-rfp, opportunity-type, language, off-theme, deadline-past)
    is unchanged. Default True preserves the existing Screened-pipeline behaviour."""
    # Search/filter result URLs are not grant detail pages — they re-list
    # grants on click. Reject before any other check.
    link = candidate.get("opportunity_link") or ""
    # User-managed blacklist (Admin → Blacklist / 🚫 on Records). Hardest reject.
    try:
        from core import blacklist as _bl
        _hit = _bl.is_blacklisted(link)
    except Exception:
        _hit = None
    if _hit:
        return False, f"blacklisted source ({_hit})"
    if candidate.get("_is_search_page") or _SEARCH_URL_PATTERN_AS.search(link):
        return False, "URL is a search / filter results page, not a grant detail"
    # Listing / index of calls (never a single opportunity) — crawl seed only.
    if _LISTING_URL_RE.search(link):
        return False, "URL lists / indexes calls, not a single call"
    # Coefficient Giving fund PARENT page (/funds/<slug>/) only describes what they
    # fund — a real RFP lives ONLY in a tabbed sub-page beneath it. Reject the
    # parent so it never lands as a false call, regardless of how it was found.
    if _CG_FUND_PARENT_RE.search(link):
        return False, ("not-an-rfp: Coefficient Giving fund overview page "
                       "(RFPs live in a sub-page tab, not the fund landing page)")
    _t = (candidate.get("opportunity_title") or "").strip()
    if _LISTING_TITLE_RE.match(_t):
        return False, "title is a generic calls-listing heading, not a single call"
    # News / press / blog page about an opportunity — not the call itself.
    if _NEWS_URL_RE.search(link) or _NEWS_TITLE_RE.match(_t):
        return False, "not-an-rfp: news / press / blog page, not a call"
    # Archive / past-call detail page (URL path) — always a closed call.
    if _PAST_CALL_URL_RE.search(link):
        return False, "not-an-rfp: past / closed call (archive path)"
    # Section "see-more" navigation links — a heading like "Current calls »" or a
    # /redirect-pages/ URL points at a listing, not a single call (AHPSR case).
    if _t.rstrip().endswith(("»", "›", "→", "≫")) or "/redirect-pages/" in link.lower():
        return False, "not-an-rfp: section navigation / 'see more' link, not a call"
    # Resource-mobilisation / "invest in the donor" page — not a grant call.
    if _DONOR_INVEST_URL_RE.search(link) or _DONOR_INVEST_TITLE_RE.search(_t):
        return False, "not-an-rfp: donor investment / resource-mobilisation page, not a call"
    # Internal course / facility / academy award — not a programme solicitation.
    if _OFF_MISSION_AWARD_RE.search(_t):
        return False, "not-an-rfp: internal course / facility / academy award (off-mission)"
    # Announcement of a FUTURE / upcoming call (topics for a later cycle) — not open
    # now. Correct reason for GACD "Future … call topics 2025-2027" (was mislabelled
    # geography). Candidate for the public "Announcements" section later.
    if _FUTURE_ANNOUNCE_RE.search(_t):
        return False, "not-an-rfp: announcement of a future / upcoming call (not open yet)"
    # Non-primary sources never get stored: competitor AGGREGATORS (DevelopmentAid,
    # GrantBite, …) — a crawl SEED only; the pipeline resolves theme-relevant hits
    # to the donor's OWN page first, so anything still on an aggregator host here
    # didn't resolve → drop it — plus BLOG platforms (blogspot/wordpress/…, which
    # let grants-gov.blogspot.com slip in) and bare LISTING/search pages. Human-
    # confirmed registry hosts are authoritative. Fail-open (never blocks a scan).
    # A candidate from a CONFIGURED PRIMARY source (curated catalogue) is trusted —
    # we crawl + extract it directly and never apply the non-primary reject. The
    # non-primary reject only fires for aggregator-class or web-discovered hits
    # that didn't resolve to a primary.
    if candidate.get("_source_class") != "primary":
        try:
            from core import aggregators as _aggr
            _np, _why = _aggr.is_non_primary(link, candidate.get("opportunity_title"))
            if _np:
                return False, _why      # reason prefix = aggregator|blog|listing
        except Exception:
            pass
    # DevelopmentAid past-tense grant (Awarded / Closed). Set by the
    # bespoke enricher in scraper._enrich_developmentaid — those listings
    # show on the catalog but aren't open opportunities.
    if candidate.get("_past_tense_grant"):
        return False, "past-tense grant (DevelopmentAid status: Awarded / Closed)"
    # Not-an-RFP gate — error pages + process/informational pages that live on
    # donor sites but aren't funding calls (the #1 false-positive source).
    ok, reason = rfp_signal_gate(candidate)
    if not ok:
        return False, f"not-an-rfp: {reason}"
    # Individual scholarships / student awards aren't org grants → drop.
    rejected, reason = individual_award_reject(candidate)
    if rejected:
        return False, f"type: {reason}"
    # Job/vacancy postings + clearly non-funding pages → drop (RFP-focused).
    rejected, reason = non_funding_reject(candidate)
    if rejected:
        return False, f"type: {reason}"
    # Error / unavailable pages (system error, 404, service down) → not a call.
    rejected, reason = error_page_reject(candidate)
    if rejected:
        return False, f"not-an-rfp: {reason}"
    # Opt-out types (training/education programs, loans) — policy-configurable.
    rejected, reason = non_grant_reject(candidate, policies)
    if rejected:
        return False, f"type: {reason}"
    # Language — if it's Arabic/CJK/etc. nothing downstream can process
    # it usefully and a non-Latin description makes deadline / keyword
    # extraction unreliable. Cheap check, fail fast.
    ok, reason = language_eligible(candidate)
    if not ok:
        return False, f"language: {reason}"
    # Org-facing only (skipped during extraction so the public store keeps them):
    # recognition PRIZES aren't proposal calls, and FORTHCOMING/announcement entries
    # aren't open yet — neither belongs in a tenant's screened feed.
    if geo_org_gates:
        if (candidate.get("solicitation_type") or "").strip().lower() == "prize":
            return False, "type: recognition prize (not a proposal call)"
        if (candidate.get("opportunity_type") or "").strip().lower() == "announcement":
            return False, "not-an-rfp: forthcoming / announcement (not open yet)"
    rejected, reason = feasibility_hard_reject(candidate, policies)
    if geo_org_gates and rejected:
        return False, f"feasibility: {reason}"
    # Closure phrase — catches rolling-deadline donors who've explicitly
    # paused intake ("no longer accepting applications"). Run before
    # deadline check because rolling calls have no deadline to compare.
    rejected, reason = closed_call_hard_reject(candidate)
    if rejected:
        return False, reason
    rejected, reason = us_domestic_only_reject(candidate, policies)
    if geo_org_gates and rejected:
        return False, f"geography: {reason}"
    # Region/country-exclusive scope that leaves our org out (EU / Mediterranean /
    # India-only / Canada-only …). Runs before the lenient regional/silent gate so
    # an exclusive call isn't parked just because it also name-drops an in-region
    # country (e.g. GCGH "India-based … only" that also mentions South Africa).
    rejected, reason = geographic_exclusion_reject(candidate, policies)
    if geo_org_gates and rejected:
        return False, reason
    # Applicant-type match — does the call admit the deploying org's type at all?
    rejected, reason = applicant_type_mismatch_reject(candidate, policies)
    if geo_org_gates and rejected:
        return False, f"eligibility: {reason}"
    ok, reason = country_eligible(candidate, policies)
    if geo_org_gates and not ok:
        return False, f"country: {reason}"
    ok, reason = theme_eligible(candidate, policies, llm_theme=llm_theme)
    if not ok:
        return False, f"theme: {reason}"
    # Deadline LAST: only attribute a "deadline" reject when the call is
    # otherwise eligible (right geography / applicant type / country / theme) but
    # expired. This stops a spurious "deadline" reason from masking the REAL
    # disqualifier — an off-theme / off-geo call (or one whose deadline
    # mis-parsed to a past date) is now rejected for the actual reason, and
    # "deadline" only appears when the deadline is genuinely the blocker.
    ok, reason = deadline_in_future(candidate)
    if not ok:
        return False, f"deadline: {reason}"
    # LLM fallback (matching) — regex-first: only when the candidate PASSED every
    # regex gate but its geography is SILENT (no scope captured), so the regex geo
    # gate couldn't judge it. Ask the LLM whether the org's country qualifies; reject
    # only on an explicit False. Bounded: most rows have a scope (skip), and
    # llm_judge has a per-process call cap. Org-facing only (geo_org_gates).
    if geo_org_gates and llm_adjudicate:
        _geo = candidate.get("geographic_scope")
        _has_geo = (bool(_geo) if isinstance(_geo, (list, tuple))
                    else bool(str(_geo or "").strip()))
        if not _has_geo:
            try:
                from core import llm_judge
                if llm_judge.is_enabled():
                    _j = llm_judge.judge({
                        "opportunity_title": candidate.get("opportunity_title"),
                        "opportunity_link": candidate.get("opportunity_link"),
                        "brief_description": (candidate.get("_page_text")
                                              or candidate.get("brief_description")),
                    }, policies)
                    if _j:
                        # Don't discard the structured verdict — stash it on the
                        # candidate so the pipeline can persist deadline / amount /
                        # type / geography it recovered (it already paid for the call).
                        candidate["_llm_judgment"] = _j
                        if _j.get("country_eligible") is False:
                            return False, "geography (LLM): org country not eligible"
            except Exception:
                pass
    return True, "eligible"


def is_index_page(candidate: dict[str, Any]) -> bool:
    """True if the candidate is a LISTING / index / search page (a crawl seed),
    not a single opportunity — so the pipeline can walk it for child calls rather
    than just reject it. Mirrors the listing checks in `is_eligible`."""
    link = candidate.get("opportunity_link") or ""
    title = (candidate.get("opportunity_title") or "").strip()
    return bool(_SEARCH_URL_PATTERN_AS.search(link)
                or _LISTING_URL_RE.search(link)
                or (title and _LISTING_TITLE_RE.match(title)))


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


# MUST / PREFER membership — explicit since the bid/no-bid rename dropped the
# must_/prefer_ prefixes (the keys now speak for themselves). Order mirrors
# CRITERION_KEYS.
_MUST_KEYS = ("qualification", "strategic_fit", "capacity",
              "geographic_fit", "cofinancing")
_PREFER_KEYS = ("funding_quality", "funder_relationship",
                "competitiveness", "bid_effort")


def recommend_from_composite(crit_vals: dict[str, Any], composite: float,
                             *, fatal: bool = False) -> str:
    """THE canonical Auto-decision rule (single source — used by auto_score and by
    any path that re-derives a criterion, e.g. the MUST-5 LLM feedback loop).

    2026-06-26: the gate is now a NON-DYNAMIC fatal-factor check, not 'any MUST<2'.
    `fatal` comes from `criteria_derive.fatal_decline` — True only when the org
    EXPLICITLY fails a structural eligibility gate it can't fix before the deadline
    (legal identity, no geographic reach, a donor stage/budget/track-record floor,
    or an inaccessible funding route). Dynamic gaps (no SAM/MOU yet) no longer
    hard-decline — they lower the composite and usually Park for review; MUST
    weight (.65) still sinks genuinely weak bids below the band.
      fatal → Decline · else ≥90 Proceed · 70–89 Park · <70 Decline."""
    if fatal:
        return "Decline"
    return "Proceed" if composite >= 90 else "Park" if composite >= 70 else "Decline"

# Internal scoring vocab ("Yes"/"Partial"/"No") → DB / UI dropdown vocab
# ("True"/"Partial"/"False"). Applied right before auto_score returns so
# every persisted MUST/PREFER cell matches the eligibility_values list
# in config/dropdowns.yaml. Anything else (Feasibility = None, etc.)
# passes through unchanged.
_CRITERION_DB_VOCAB = {"Yes": "True", "No": "False", "Partial": "Partial"}


# Signals that route a candidate to "Proceed as Sub" instead of straight
# "Proceed".
#
# NOTE: The logic below encodes reference-deployment business rules
# patterned on a typical implementing-NGO with a US-parent + country-
# office structure. When RFPIS goes multi-tenant, this signal list
# should move into a per-org config table
# (`organizations.sub_role_signals`) and stop living in code. For now
# it ships hard-coded; deploying orgs can override by editing this list.
#
# Typical implementing-NGO structure assumed here:
#   * Parent org registered as a US 501(c)(3) — eligible for US-only RFPs.
#   * Country offices apply directly OR route through the US parent.
# So "US-based applicant required" is NOT an exclusion — it's a directive
# to apply via the US entity, with the country team as sub. The app
# surfaces that as "Proceed as sub" so the team knows they'll be
# downstream of HQ on this one.
#
# For research-institution / university requirements: an implementing
# NGO is typically not a research-degree-granting institution, so it
# would need a research-org partner as Prime with NGO sub. Same Sub
# routing.
#
# For EU/Canada/etc. residency requirements: the deploying org may lack
# a local 501(c) equivalent in most of those geographies, so it would
# partner with a regional NGO as Prime — again as Sub.
_SUB_ROLE_SIGNALS = (
    # Research / academic — typical implementing NGO is not a research institution
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
    # US residency — country office goes sub to US-parent (which IS US-based)
    "u.s.-based",
    "us-based",
    "based in the united states",
    "domestic applicants only",
    # EU / Canada / other — partner with a regional lead
    "based in the eu",
    "based in europe",
    "european institution",
    "canadian institution",
    "based in canada",
)


def _detect_applicant_role(text: str) -> str:
    """Default the applicant role to **Prime**. Switch to **Sub** when
    the RFP text contains signals that route the deploying org
    downstream of another applicant — most commonly:

      * Research / university requirement → sub to a research-org Prime
      * US-residency requirement → country-office sub to US-parent (which
        IS a US 501(c)(3))
      * Other regional residency (EU, Canada) → sub to a regional NGO

    Critically: these are NOT exclusions — they're sub-routing signals.
    The recommendation stays Proceed; only the role flips."""
    if not text:
        return "Prime"
    tl = text.lower()
    if any(s in tl for s in _SUB_ROLE_SIGNALS):
        return "Sub"
    return "Prime"


def _decision_from_criteria(values: dict[str, str]) -> str:
    """Deploying-org decision tree (overrides scorer.auto_recommendation):

      * Any MUST = No (False)           → Decline
      * ≥2 MUSTs = Partial               → Decline
      * Exactly 1 MUST = Partial         → Park (review)
      * All MUSTs = Yes (True):
          * ≥3 of 4 PREFERs = Yes        → Proceed
          * else                         → Park (review)
    """
    # Normalise through criterion_score (2/1/0/None) so this works for the
    # internal Yes/Partial/No values AND stored DB labels — legacy
    # True/Partial/False and the new MS-Form rich labels alike. A "Not sure" /
    # unscored MUST (None) doesn't trigger Decline (conservative).
    sc = {k: criterion_score(values.get(k)) for k in (_MUST_KEYS + _PREFER_KEYS)}
    musts_no = sum(1 for m in _MUST_KEYS if sc.get(m) == 0)
    musts_partial = sum(1 for m in _MUST_KEYS if sc.get(m) == 1)
    if musts_no >= 1:
        return "Decline"
    if musts_partial >= 2:
        return "Decline"
    if musts_partial == 1:
        return "Park"
    # No blocking MUSTs at this point.
    prefers_yes = sum(1 for p in _PREFER_KEYS if sc.get(p) == 2)
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
    # Prioritise exact eligible countries + the member countries of any selected
    # broad geography (same shared vocabulary as the donor scope + the geo gate).
    _c = policies.get("countries") or {}
    eligible = geo.expand((_c.get("eligible") or []) + (_c.get("broad_terms") or []))
    found: list[str] = []
    seen: set[str] = set()
    for m in _COUNTRY_PATTERN.findall(text):
        key = m.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(m)
    # Prioritise eligible countries first in the list (so an eligible country shows
    # before Nigeria if both are mentioned).
    found.sort(key=lambda c: (0 if c.lower() in eligible else 1, c))
    return found[:8]  # cap to avoid overflowing the column


def _extract_program_area(text: str, policies: dict[str, Any]) -> list[str]:
    """Classify candidate text into one or more canonical program areas.

    Delegates to `core.program_area_classifier.classify_program_areas`
    which uses a comprehensive keyword bag per area (HIV, TB, malaria,
    cancer, mental health, diabetes, nutrition, digital health, etc.)
    and falls back to "Unspecified Program Area" when nothing matches —
    NOT "Other" (which would imply a real bucket; Unspecified makes the
    classifier's failure explicit).

    The `policies` arg is kept for backward compatibility but no longer
    consulted — classifier rules live in code, not policy config, to
    stay reviewable at PR time. Move to per-deployment overrides later
    if a tenant wants to tune without code edits.
    """
    if not text:
        return [_UNSPECIFIED_PROGRAM_AREA]
    from core.program_area_classifier import classify_program_areas
    return classify_program_areas(text)


_UNSPECIFIED_PROGRAM_AREA = "Unspecified Program Area"


def _extract_program_area_LEGACY(text: str, policies: dict[str, Any]) -> list[str]:
    """Legacy keyword-extractor — kept as a reference for backward
    comparison. NOT called anywhere. Delete on the next cleanup pass."""
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
    if fq.get("enabled"):
        if amount_usd > 0:
            tiers = fq.get("tiers") or []
            # Tiers should be ordered HIGH → LOW; first satisfied wins.
            for tier in tiers:
                try:
                    threshold = float(tier.get("threshold_usd", 0))
                except (TypeError, ValueError):
                    continue
                if amount_usd >= threshold:
                    values["funding_quality"] = tier.get("value") or values.get("funding_quality")
                    break
        else:
            # No funding amount published → we can't judge funding quality.
            # Flag Partial (review) rather than scoring the lowest "No" tier.
            values["funding_quality"] = "Partial"

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
            current = values.get("cofinancing")
            if current in (None, "No"):
                values["cofinancing"] = res.get("forced_value") or "Partial"

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

    # --- 5. Donor intelligence — authoritative real-world evidence ----------
    # Match the funder to the donor_intel matrix and let verified donor
    # metadata override MUST 4 / PREFER 8 / (fallback when the RFP is silent
    # on amount) PREFER 6. Wrapped so a lookup failure never breaks scoring.
    try:
        from core import donor_intel
        donor_intel.apply_to_values(values, candidate, policies)
    except Exception:
        pass

    return values


def _apply_criteria_keywords(values: dict[str, Any], text: str,
                             policies: dict[str, Any]) -> dict[str, Any]:
    """Crawl keyword ASSIST (supplements the objective derivation).

    Per criterion, against the RFP text:
      * a NEGATIVE term found → force the criterion to "No" (red flag; for a
        MUST this screens the RFP out as Decline),
      * else a POSITIVE term found AND the criterion is still undetermined
        (None) → set it "Yes".
    Criteria with no configured terms are left exactly as the derivation set
    them. Terms live in policies['criteria'][key]{positive,negative}."""
    if not text:
        return values
    tl = text.lower()
    for key, rule in (policies.get("criteria") or {}).items():
        if key not in values:
            continue
        if any(n and n.lower() in tl for n in (rule.get("negative") or [])):
            values[key] = "No"
        elif values.get(key) is None and any(
                p and p.lower() in tl for p in (rule.get("positive") or [])):
            values[key] = "Yes"
    return values


# Below this description length (chars) a candidate with no other substantive
# field counts as a "blank cheque" — nothing to judge.
_BLANK_DESC_MIN = 140


def _is_blank_cheque(candidate: dict[str, Any]) -> bool:
    """True when the candidate carries essentially NO substantive data to judge —
    no deadline, no real award value, no geography, no SPECIFIC program area, and
    only a threadbare description (e.g. a paywalled / premium-listing anchor that
    yielded just a title). Such a row must not earn a positive score off the
    fabricated optimistic defaults (qualification "Yes, fully" when nothing is
    documented, etc.) — there's nothing to bid on, so it is declined."""
    if candidate.get("submission_deadline"):
        return False
    val = candidate.get("estimated_value")
    try:
        if val is not None and float(val) > 0:   # float('nan') > 0 is False — good
            return False
    except (TypeError, ValueError):
        pass

    def _listed(x) -> bool:
        if not x:
            return False
        if isinstance(x, (list, tuple)):
            return any(str(i).strip() for i in x)
        return bool(str(x).strip())

    if _listed(candidate.get("geographic_scope")):
        return False
    pa = candidate.get("program_area")
    pa_list = pa if isinstance(pa, (list, tuple)) else ([pa] if pa else [])
    from core.program_area_classifier import PROGRAM_AREA_KEYWORDS as _PAK
    if any(str(p) in _PAK for p in pa_list):
        return False
    if len((candidate.get("brief_description") or "").strip()) >= _BLANK_DESC_MIN:
        return False
    return True


def auto_score(
    candidate: dict[str, Any], policies: dict[str, Any],
) -> dict[str, Any]:
    """Return a dict of fields ready to merge into the rfp_submissions row.

    Output keys: feasibility, qualification, ..., bid_effort,
    alignment_score, auto_recommendation, decision, decline_flags_present,
    geographic_scope (if detected), program_area (if detected).
    """
    text = _full_text(candidate)
    # Criteria are now OBJECTIVELY DERIVED from org × RFP (× donor) facts — the
    # per-criterion keyword bags + scoring_rules override layer were retired
    # (2026-06-17). Base every criterion to null ("Not sure" = missing); the
    # derivation below fills what it can determine and leaves the rest null
    # (honest — never a fabricated 0/No). Feasibility is human-only (High/Medium/
    # Low on Review); its out-of-capability terms now live in Themes → Excluded.
    values: dict[str, str | None] = {key: None for key in CRITERION_KEYS}
    _donor_row = None   # matched donor_intel row (set below; used by Path-B decision)

    # OBJECTIVE derivation of the 9 criteria from org × RFP facts — the
    # auto-scan's pick, factoring each criterion's FULL definition (e.g.
    # strategic_fit = priorities AND experience; bid_effort = deadline × BD team).
    # Overrides the keyword guess where derivable; None leaves the keyword/default
    # value. Human review can still change any of these.
    try:
        from core import criteria_derive
        from core import org_profile as _orgp
        from core import settings as _settings
        _donor_row = None
        try:
            from db.supabase_client import get_client
            _fa = (candidate.get("funding_agency") or "").strip()
            if _fa:
                _dq = (get_client().table("donor_intel").select("*")
                       .ilike("donor", _fa).limit(1).execute().data or [])
                _donor_row = _dq[0] if _dq else None
        except Exception:
            _donor_row = None
        _derived = criteria_derive.derive_criteria(
            candidate, _orgp.get_profile(), _donor_row, _settings.get_org(), policies)
        for _k, _lbl in _derived.items():
            if _lbl is not None:
                values[_k] = _lbl
    except Exception:
        pass

    # Crawl keyword ASSIST — admin-configurable per-criterion terms supplement
    # the derivation against the RFP text (see policies['criteria']).
    values = _apply_criteria_keywords(values, text, policies)

    # decline_flags rule (per the reference deployment's policy):
    #   Decline flag = NO only when all 5 MUSTs == Yes AND ≥3 of 4 PREFERs == Yes
    #   Decline flag = YES otherwise. Normalised via criterion_score so the
    #   bid-effort rich label (and any True/Partial/False) counts correctly.
    all_musts_yes = all(criterion_score(values.get(m)) == 2 for m in _MUST_KEYS)
    prefers_yes = sum(1 for p in _PREFER_KEYS if criterion_score(values.get(p)) == 2)
    decline_flags = not (all_musts_yes and prefers_yes >= 3)

    # Numeric score for display purposes (Review gauge). We still compute
    # it the legacy way — weighted sum of MUST/PREFER values, 0-100 scale.
    scorer_input = {k: values[k] for k in values if k != "feasibility"}
    score, _legacy_rec = score_submission(scorer_input, decline_flags)

    # SCORE = composite (0.80 criteria + 0.20 donor-org extras), shown on the
    # Bid-Strength gauge. We keep the composite for the SCORE, but the DECISION
    # comes from the explicit rule below (not the composite's own thresholds).
    rec = _decision_from_criteria(values)   # fallback if matching is unavailable
    try:
        from core import matching as _matching
        from core import org_profile as _orgp
        from core import settings as _settings
        _m = _matching.composite_match(
            {**candidate, **values}, _orgp.get_profile(), _donor_row, _settings.get_org())
        score = _m["composite"]
    except Exception:
        pass

    # DECISION RULE (2026-06-26, supersedes the blanket MUST-gate). Auto-Decline
    # ONLY on a NON-DYNAMIC fatal factor (criteria_derive.fatal_decline) — a
    # structural ineligibility the org can't fix before the deadline (legal
    # identity, no geographic reach, a donor stage/budget/track-record floor, or
    # an inaccessible funding route). Otherwise band the composite: ≥90 Proceed ·
    # 70–89 Park · <70 Decline. Dynamic gaps (no SAM/MOU yet) lower the score and
    # usually Park for review; the high MUST weight (.65) still sinks weak bids.
    try:
        from core import criteria_derive as _cd
        from core import org_profile as _op
        from core import settings as _st
        _is_fatal, _ = _cd.fatal_decline(
            _op.get_profile(), candidate, _donor_row, _st.get_org())
    except Exception:
        _is_fatal = False
    rec = recommend_from_composite(values, score, fatal=_is_fatal)

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

    # NOTE (2026-06-25): the old "Proceed→Park" downgrades for missing-deadline
    # and weak/region-wide geography were REMOVED. They made the decision
    # unpredictable (an all-MUSTs-2, 90+ score call landed in Park because its
    # geography was "LMICs"). Geography is now fully judged by MUST-4
    # (geographic_fit, which credits inclusive tiers like LMICs as own-presence),
    # and the decision is the clean MUST-gate + score-band rule above. Expired
    # calls are already dropped upstream by the extraction/eligibility deadline
    # gate, so they don't reach scoring as Proceed.

    # BLANK-CHEQUE GUARD (final say): a candidate with no substantive data to judge
    # (no deadline, value, geography, specific program area, or real description)
    # must not score off fabricated positive defaults. Clear those fake positives
    # back to "Not sure" and DECLINE — there's nothing to bid on. This overrides
    # the sparse-text→Park promotion above (that one parks <200-char rows for
    # review; a true blank cheque is declined, not parked).
    if _is_blank_cheque(candidate):
        for _k in (*_MUST_KEYS, *_PREFER_KEYS):
            values[_k] = None
        score = 0.0
        rec = "Decline"
        decline_flags = True

    # Default applicant role = Prime unless RFP text demands a research /
    # region-specific institution (in which case the deploying org applies
    # as Sub). The Sub distinction now lives ENTIRELY in `applicant_role` —
    # the decision stays "Proceed" (the "Proceed as sub" decision value was
    # dropped 2026-06-06; Role: Prime/Sub/Technical carries the sub signal).
    applicant_role = _detect_applicant_role(text)

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
        # `decision` is the HUMAN's call and stays NULL (= "Pending") until a
        # reviewer decides — it must never be pre-filled from the model, or the
        # learning signal is polluted (we'd be training on our own guess). The
        # system's suggestion lives in `auto_recommendation` ("Auto-decision");
        # screened/tracking views coalesce decision→auto_recommendation only for
        # display bucketing, never for the stored value.
        # Default the applicant role. Park / Decline rows still get a role
        # so the team has context if they choose to review.
        "applicant_role": applicant_role,
    }
    # Auto-populated companion fields — only set if the candidate hasn't
    # already provided them, so explicit scraper-extracted values win.
    if not candidate.get("geographic_scope"):
        geo = _extract_geographic_scope(text, policies)
        if geo:
            out["geographic_scope"] = geo
    # program_area: classify from the description. REPLACE a generic crawled
    # value (e.g. "Health" — not a taxonomy key) with specific areas so
    # strategic_fit can match the org; leave an already-taxonomy-keyed value
    # (human- or previously-classified) alone. Also stamp focus_theme with the
    # high-level categories when we (re)classify.
    from core.program_area_classifier import (
        PROGRAM_AREA_KEYWORDS as _PAK, UNSPECIFIED as _UNSPEC,
        category_full as _catfull, subarea_label as _sublab,
    )
    _cur_pa = candidate.get("program_area")
    _cur_list = _cur_pa if isinstance(_cur_pa, (list, tuple)) else ([_cur_pa] if _cur_pa else [])
    # match on either the canonical key OR the bare sub-label (stored form)
    if not any(str(v) in _PAK or str(v) in {_sublab(k) for k in _PAK} for v in _cur_list):
        prog = [a for a in (_extract_program_area(text, policies) or []) if a != _UNSPEC]
        if prog:
            # STORE the bare sub-label ("Mental Health"), never the prefixed key
            # ("NCDs - Mental Health") — the category prefix is dropped everywhere
            # per policy. focus_theme keeps the category (computed from the key).
            out["focus_theme"] = "; ".join(sorted({_catfull(a) for a in prog}))
            out["program_area"] = [_sublab(a) for a in prog]
    return out
