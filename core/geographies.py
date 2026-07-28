"""Geographic options — UN M49 regions/subregions, income tiers, and countries.

Powers the donor "Funded geographies" field + the donor filters, so a tenant can
drill from a region (e.g. Sub-Saharan Africa) or tier (LMICs) down to a specific
country. Each donor records the regions/countries where it actually funds, so a
reviewer sees the donor's true coverage instead of a single placeholder country.

`expand(selection)` widens a chosen region/tier into the countries it contains,
so a filter for "Sub-Saharan Africa" also matches a donor tagged with "Kenya".

`text_matches_term(text, term)` (with SYNONYMS) lets the scanner/search match an
RFP body to a high-level "broad geography" via its spelling/acronym variants AND
its member countries — e.g. selecting "Sub-Saharan Africa" matches a call that
says "SSA" or merely names "Kenya".
"""
from __future__ import annotations

import re

# ── UN M49 regions & sub-regions ────────────────────────────────────────────
UN_REGIONS = [
    "Africa", "Northern Africa", "Sub-Saharan Africa",
    "Eastern Africa", "Middle Africa", "Southern Africa", "Western Africa",
    "Americas", "Latin America and the Caribbean", "Caribbean",
    "Central America", "South America", "Northern America",
    "Asia", "Central Asia", "Eastern Asia", "South-eastern Asia",
    "Southern Asia", "Western Asia",
    "Europe", "Eastern Europe", "Northern Europe", "Southern Europe",
    "Western Europe",
    "Oceania", "Australia and New Zealand", "Melanesia", "Micronesia",
    "Polynesia",
]

# ── Income / development tiers (not M49, but how donors scope eligibility) ───
INCOME_TIERS = [
    "Global / worldwide", "Global South",
    "Low- and middle-income countries (LMICs)",
    "Low-income countries", "Lower-middle-income countries",
    "Upper-middle-income countries",
    "Least Developed Countries (LDCs)",
    "Fragile & conflict-affected states",
    "Small Island Developing States (SIDS)",
]

# ── Countries (UN members + common observers/territories) ───────────────────
COUNTRIES = [
    "Afghanistan", "Albania", "Algeria", "Andorra", "Angola",
    "Antigua and Barbuda", "Argentina", "Armenia", "Australia", "Austria",
    "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados", "Belarus",
    "Belgium", "Belize", "Benin", "Bhutan", "Bolivia",
    "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei", "Bulgaria",
    "Burkina Faso", "Burundi", "Cabo Verde", "Cambodia", "Cameroon", "Canada",
    "Central African Republic", "Chad", "Chile", "China", "Colombia",
    "Comoros", "Congo (Brazzaville)", "Congo (DRC)", "Costa Rica",
    "Côte d'Ivoire", "Croatia", "Cuba", "Cyprus", "Czechia", "Denmark",
    "Djibouti", "Dominica", "Dominican Republic", "Ecuador", "Egypt",
    "El Salvador", "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini",
    "Ethiopia", "Fiji", "Finland", "France", "Gabon", "Gambia", "Georgia",
    "Germany", "Ghana", "Greece", "Grenada", "Guatemala", "Guinea",
    "Guinea-Bissau", "Guyana", "Haiti", "Honduras", "Hungary", "Iceland",
    "India", "Indonesia", "Iran", "Iraq", "Ireland", "Israel", "Italy",
    "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya", "Kiribati", "Kosovo",
    "Kuwait", "Kyrgyzstan", "Laos", "Latvia", "Lebanon", "Lesotho", "Liberia",
    "Libya", "Liechtenstein", "Lithuania", "Luxembourg", "Madagascar",
    "Malawi", "Malaysia", "Maldives", "Mali", "Malta", "Marshall Islands",
    "Mauritania", "Mauritius", "Mexico", "Micronesia", "Moldova", "Monaco",
    "Mongolia", "Montenegro", "Morocco", "Mozambique", "Myanmar", "Namibia",
    "Nauru", "Nepal", "Netherlands", "New Zealand", "Nicaragua", "Niger",
    "Nigeria", "North Korea", "North Macedonia", "Norway", "Oman", "Pakistan",
    "Palau", "Palestine", "Panama", "Papua New Guinea", "Paraguay", "Peru",
    "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia",
    "Rwanda", "Saint Kitts and Nevis", "Saint Lucia",
    "Saint Vincent and the Grenadines", "Samoa", "San Marino",
    "Sao Tome and Principe", "Saudi Arabia", "Senegal", "Serbia", "Seychelles",
    "Sierra Leone", "Singapore", "Slovakia", "Slovenia", "Solomon Islands",
    "Somalia", "South Africa", "South Korea", "South Sudan", "Spain",
    "Sri Lanka", "Sudan", "Suriname", "Sweden", "Switzerland", "Syria",
    "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Timor-Leste", "Togo",
    "Tonga", "Trinidad and Tobago", "Tunisia", "Türkiye", "Turkmenistan",
    "Tuvalu", "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom",
    "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Venezuela",
    "Vietnam", "Yemen", "Zambia", "Zimbabwe",
]

# Ordered options for the multiselect: regions, tiers, then countries.
GEO_OPTIONS = UN_REGIONS + INCOME_TIERS + COUNTRIES

# ── Region → member-countries (for filter expansion) ────────────────────────
# Only the regions/tiers a Global-South-focused tool actually filters on are
# enumerated; others expand to themselves (still selectable, just no children).
_SSA = [
    "Angola", "Benin", "Botswana", "Burkina Faso", "Burundi", "Cabo Verde",
    "Cameroon", "Central African Republic", "Chad", "Comoros",
    "Congo (Brazzaville)", "Congo (DRC)", "Côte d'Ivoire", "Djibouti",
    "Equatorial Guinea", "Eritrea", "Eswatini", "Ethiopia", "Gabon", "Gambia",
    "Ghana", "Guinea", "Guinea-Bissau", "Kenya", "Lesotho", "Liberia",
    "Madagascar", "Malawi", "Mali", "Mauritania", "Mauritius", "Mozambique",
    "Namibia", "Niger", "Nigeria", "Rwanda", "Sao Tome and Principe",
    "Senegal", "Seychelles", "Sierra Leone", "Somalia", "South Africa",
    "South Sudan", "Sudan", "Tanzania", "Togo", "Uganda", "Zambia", "Zimbabwe",
]
_NORTH_AFRICA = ["Algeria", "Egypt", "Libya", "Morocco", "Tunisia"]
_WEST_AFRICA = [
    "Benin", "Burkina Faso", "Cabo Verde", "Côte d'Ivoire", "Gambia", "Ghana",
    "Guinea", "Guinea-Bissau", "Liberia", "Mali", "Mauritania", "Niger",
    "Nigeria", "Senegal", "Sierra Leone", "Togo",
]
_CENTRAL_AFRICA = [
    "Angola", "Cameroon", "Central African Republic", "Chad",
    "Congo (Brazzaville)", "Congo (DRC)", "Equatorial Guinea", "Gabon",
    "Sao Tome and Principe",
]
_EAST_AFRICA = [
    "Burundi", "Comoros", "Djibouti", "Eritrea", "Ethiopia", "Kenya",
    "Madagascar", "Malawi", "Mauritius", "Mozambique", "Rwanda", "Seychelles",
    "Somalia", "South Sudan", "Tanzania", "Uganda", "Zambia", "Zimbabwe",
]
_SOUTHERN_AFRICA = ["Botswana", "Eswatini", "Lesotho", "Namibia", "South Africa"]

_REGION_MEMBERS: dict[str, list[str]] = {
    "africa": _SSA + _NORTH_AFRICA,
    "sub-saharan africa": _SSA,
    "northern africa": _NORTH_AFRICA,
    "western africa": _WEST_AFRICA,
    "middle africa": _CENTRAL_AFRICA,
    "eastern africa": _EAST_AFRICA,
    "southern africa": _SOUTHERN_AFRICA,
    # Income/development tiers that broadly cover the Global South. Coarse on
    # purpose — better to over-match (show the donor) than to miss it.
    "global south": _SSA + _NORTH_AFRICA + _WEST_AFRICA,
    "low- and middle-income countries (lmics)": _SSA + _NORTH_AFRICA,
    "least developed countries (ldcs)": _SSA,
}


# ── Non-African region members (so the geo gate can test, policy-driven, whether
# a call's region contains the org's country — e.g. Cameroon ∉ EU/Mediterranean).
_EU = [
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czechia", "Denmark",
    "Estonia", "Finland", "France", "Germany", "Greece", "Hungary", "Ireland",
    "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta", "Netherlands",
    "Poland", "Portugal", "Romania", "Slovakia", "Slovenia", "Spain", "Sweden",
]
_EUROPE = sorted(set(_EU + [
    "Albania", "Andorra", "Belarus", "Bosnia and Herzegovina", "Iceland",
    "Kosovo", "Liechtenstein", "Moldova", "Monaco", "Montenegro",
    "North Macedonia", "Norway", "Russia", "San Marino", "Serbia",
    "Switzerland", "Ukraine", "United Kingdom",
]))
_NORTHERN_AMERICA = ["Canada", "United States"]
_CENTRAL_AMERICA = ["Belize", "Costa Rica", "El Salvador", "Guatemala",
                    "Honduras", "Mexico", "Nicaragua", "Panama"]
_SOUTH_AMERICA = ["Argentina", "Bolivia", "Brazil", "Chile", "Colombia",
                  "Ecuador", "Guyana", "Paraguay", "Peru", "Suriname",
                  "Uruguay", "Venezuela"]
_CARIBBEAN = ["Antigua and Barbuda", "Bahamas", "Barbados", "Cuba", "Dominica",
              "Dominican Republic", "Grenada", "Haiti", "Jamaica",
              "Saint Kitts and Nevis", "Saint Lucia",
              "Saint Vincent and the Grenadines", "Trinidad and Tobago"]
_LATAM = sorted(set(_CENTRAL_AMERICA + _SOUTH_AMERICA + _CARIBBEAN))
_NORTH_AMERICA_ALL = sorted(set(_NORTHERN_AMERICA + _CENTRAL_AMERICA + _CARIBBEAN))
_EASTERN_ASIA = ["China", "Japan", "Mongolia", "North Korea", "South Korea",
                 "Taiwan"]
_SE_ASIA = ["Brunei", "Cambodia", "Indonesia", "Laos", "Malaysia", "Myanmar",
            "Philippines", "Singapore", "Thailand", "Timor-Leste", "Vietnam"]
_SOUTHERN_ASIA = ["Afghanistan", "Bangladesh", "Bhutan", "India", "Iran",
                  "Maldives", "Nepal", "Pakistan", "Sri Lanka"]
_CENTRAL_ASIA = ["Kazakhstan", "Kyrgyzstan", "Tajikistan", "Turkmenistan",
                 "Uzbekistan"]
_WESTERN_ASIA = ["Armenia", "Azerbaijan", "Bahrain", "Cyprus", "Georgia",
                 "Iraq", "Israel", "Jordan", "Kuwait", "Lebanon", "Oman",
                 "Palestine", "Qatar", "Saudi Arabia", "Syria", "Türkiye",
                 "United Arab Emirates", "Yemen"]
_ASIA = sorted(set(_EASTERN_ASIA + _SE_ASIA + _SOUTHERN_ASIA + _CENTRAL_ASIA
                   + _WESTERN_ASIA))
_OCEANIA = ["Australia", "Fiji", "Kiribati", "Marshall Islands", "Micronesia",
            "Nauru", "New Zealand", "Palau", "Papua New Guinea", "Samoa",
            "Solomon Islands", "Tonga", "Tuvalu", "Vanuatu"]
# The Mediterranean basin — Southern Europe + North Africa + the Levant. Notably
# does NOT include Sub-Saharan Africa (Cameroon/Mali), so it excludes that org.
_MEDITERRANEAN = ["Albania", "Algeria", "Bosnia and Herzegovina", "Croatia",
                  "Cyprus", "Egypt", "France", "Greece", "Israel", "Italy",
                  "Lebanon", "Libya", "Malta", "Monaco", "Montenegro", "Morocco",
                  "Palestine", "Slovenia", "Spain", "Syria", "Tunisia", "Türkiye"]

_REGION_MEMBERS.update({
    "europe": _EUROPE,
    "european union": _EU,
    "northern europe": ["Denmark", "Estonia", "Finland", "Iceland", "Ireland",
                        "Latvia", "Lithuania", "Norway", "Sweden",
                        "United Kingdom"],
    "western europe": ["Austria", "Belgium", "France", "Germany",
                       "Liechtenstein", "Luxembourg", "Monaco", "Netherlands",
                       "Switzerland"],
    "southern europe": ["Albania", "Andorra", "Bosnia and Herzegovina",
                        "Croatia", "Greece", "Italy", "Malta", "Montenegro",
                        "North Macedonia", "Portugal", "San Marino", "Serbia",
                        "Slovenia", "Spain"],
    "eastern europe": ["Belarus", "Bulgaria", "Czechia", "Hungary", "Moldova",
                       "Poland", "Romania", "Russia", "Slovakia", "Ukraine"],
    "americas": sorted(set(_NORTHERN_AMERICA + _LATAM)),
    "northern america": _NORTHERN_AMERICA,
    "north america": _NORTH_AMERICA_ALL,
    "latin america and the caribbean": _LATAM,
    "central america": _CENTRAL_AMERICA,
    "south america": _SOUTH_AMERICA,
    "caribbean": _CARIBBEAN,
    "asia": _ASIA,
    "eastern asia": _EASTERN_ASIA,
    "south-eastern asia": _SE_ASIA,
    "southern asia": _SOUTHERN_ASIA,
    "central asia": _CENTRAL_ASIA,
    "western asia": _WESTERN_ASIA,
    "oceania": _OCEANIA,
    "mediterranean": _MEDITERRANEAN,
})

# Region labels the geo gate scans for in a call's text (UN regions + the two
# common non-M49 scopes). Income tiers + the global tier are deliberately
# EXCLUDED — those are inclusive ("LMIC", "global"), handled as keepers upstream.
REGION_TERMS = UN_REGIONS + ["European Union", "Mediterranean"]


def expand(selection) -> set[str]:
    """Expand a list of region/tier/country labels into a lowercased set of all
    implied geographies (the labels themselves + any member countries), so a
    region filter also matches donors tagged with that region's countries."""
    out: set[str] = set()
    for item in (selection or []):
        s = str(item).strip()
        if not s:
            continue
        out.add(s.lower())
        out.update(c.lower() for c in _REGION_MEMBERS.get(s.lower(), []))
    return out


# ── Broad geographies + synonyms (Scan Preferences "Broad-geography terms") ──
# The high-level options offered as broad terms: UN regions + income/dev tiers
# (NOT individual countries — those go in "Eligible Countries"). Each term has
# spelling/acronym variants matched in RFP text, plus its member countries via
# expand(). Selecting a broad term is what relaxes the gate beyond exact
# countries; with NONE selected, only exact eligible-country matches admit.
BROAD_GEOGRAPHIES = UN_REGIONS + INCOME_TIERS

# Conservative variant lists — word-boundary matched, so short acronyms (SSA,
# LMIC, LDC) won't fire inside other words. Terms not listed fall back to just
# their own label + member countries.
SYNONYMS: dict[str, list[str]] = {
    "Africa": ["africa", "african continent", "pan-african", "pan african"],
    "Sub-Saharan Africa": ["sub-saharan africa", "sub saharan africa",
                           "subsaharan africa", "sub-saharan", "ssa"],
    "Northern Africa": ["north africa", "northern africa", "maghreb"],
    "Eastern Africa": ["east africa", "eastern africa", "horn of africa"],
    "Western Africa": ["west africa", "western africa", "sahel"],
    "Middle Africa": ["central africa", "middle africa"],
    "Southern Africa": ["southern africa"],
    "Latin America and the Caribbean": ["latin america", "the caribbean"],
    "South America": ["south america", "south american"],
    "Central America": ["central america"],
    "Southern Asia": ["south asia", "southern asia"],
    "South-eastern Asia": ["southeast asia", "south-east asia",
                           "south-eastern asia", "asean"],
    "Eastern Asia": ["east asia", "eastern asia"],
    "Western Asia": ["west asia", "western asia", "middle east"],
    "Central Asia": ["central asia"],
    "Global / worldwide": ["global", "globally", "worldwide", "world wide",
                           "international", "any country", "all countries",
                           "around the world"],
    "Global South": ["global south", "global-south", "developing world",
                     "majority world", "the south"],
    "Low- and middle-income countries (LMICs)": [
        "lmic", "lmics", "l&mic", "l&mics", "lmi countries",
        "low- and middle-income", "low and middle income", "low and middle-income",
        "low-and-middle-income", "low/middle income", "low-middle-income",
        "lower- and middle-income", "lower and middle income",
        "low- or middle-income", "low or middle income",
        "developing country", "developing countries", "developing nation",
        "developing nations", "developing economies", "developing region",
        "developing regions", "resource-limited", "resource limited",
        "resource-poor", "resource poor", "low-resource", "low resource",
        "resource-constrained", "under-resourced", "underserved countries"],
    "Low-income countries": ["low-income country", "low-income countries",
                             "low income countries", "low income country",
                             "poorest countries", "poorest nations"],
    "Lower-middle-income countries": ["lower-middle-income", "lower middle income",
                                      "lower-middle income"],
    "Upper-middle-income countries": ["upper-middle-income", "upper middle income",
                                      "upper-middle income"],
    "Least Developed Countries (LDCs)": ["least developed countries",
                                         "least developed country", "least-developed",
                                         "ldc", "ldcs", "under-developed",
                                         "underdeveloped", "under-developed countries",
                                         "underdeveloped countries", "third world",
                                         "third-world"],
    "Fragile & conflict-affected states": ["fragile state", "fragile states",
                                           "conflict-affected", "conflict affected"],
    "Small Island Developing States (SIDS)": ["small island developing states",
                                              "sids", "small island states"],
}


SYNONYMS.update({
    "Europe": ["europe", "european"],
    "European Union": ["european union", "eu", "eu member states",
                       "eu member state", "horizon europe", "the eu", "eu's"],
    "Northern Europe": ["northern europe"],
    "Western Europe": ["western europe"],
    "Southern Europe": ["southern europe"],
    "Eastern Europe": ["eastern europe"],
    "Americas": ["the americas"],
    "Northern America": ["northern america"],
    "North America": ["north america", "north american"],
    "Asia": ["asia", "asian"],
    "Oceania": ["oceania", "pacific islands"],
    "Mediterranean": ["mediterranean", "mediterranean region",
                      "mediterranean basin", "the mediterranean"],
})

# "EU" alone is high-precision as an uppercase token but noisy lowercased, so it
# is matched separately (word-boundary, case-sensitive) by callers, not here.


def _region_name_variants(term: str) -> list[str]:
    """A region's LABEL + SYNONYMS only (NOT its member countries — those are
    detected separately as countries). Longest first for span consumption."""
    out = {term.lower()}
    out.update(s.lower() for s in SYNONYMS.get(term, []))
    return sorted((v for v in out if v), key=len, reverse=True)


def regions_in_text(text_lower: str) -> set[str]:
    """Region labels (from REGION_TERMS) named in the already-lowercased text.
    Matches region NAMES/synonyms only, longest-phrase-first with span
    consumption — so "sub-Saharan Africa" registers as Sub-Saharan Africa and the
    inner "africa" does NOT also register the whole continent. Used by the geo
    gate to learn a call's scope."""
    pairs = sorted(
        ((v, term) for term in REGION_TERMS for v in _region_name_variants(term)),
        key=lambda p: len(p[0]), reverse=True)
    found: set[str] = set()
    work = text_lower
    for v, term in pairs:
        pat = r"\b" + re.escape(v) + r"\b"
        if re.search(pat, work):
            found.add(term)
            work = re.sub(pat, " ", work)   # consume so a parent term can't re-match
    return found


def variants(term: str) -> list[str]:
    """All lowercased text variants for a broad-geography term: its label, its
    declared SYNONYMS, and its member countries (from expand). Deduped."""
    t = str(term or "").strip()
    if not t:
        return []
    out = {t.lower()}
    out.update(s.lower() for s in SYNONYMS.get(t, []))
    out.update(c.lower() for c in _REGION_MEMBERS.get(t.lower(), []))
    return sorted(v for v in out if v)


def text_matches_term(text_lower: str, term: str) -> bool:
    """True if any variant of `term` appears as a whole word/phrase in the
    (already lowercased) text. Word-boundary matched so e.g. "africa" fires on
    "in Africa" but not "African Development Bank", and "ssa" doesn't fire
    inside another word."""
    if not text_lower or not term:
        return False
    return any(re.search(r"\b" + re.escape(v) + r"\b", text_lower)
               for v in variants(term))


# Every broad term we can normalise to: UN regions + income tiers + the extra
# SYNONYMS-only labels (European Union, Mediterranean, North America, …).
_ALL_BROAD_TERMS = list(dict.fromkeys(list(BROAD_GEOGRAPHIES) + list(SYNONYMS.keys())))


def broad_geos_in_text(text_lower: str) -> set[str]:
    """Like regions_in_text, but over ALL broad geographies — UN regions AND the
    income/development TIERS (LMICs, LDCs, Global South, …). regions_in_text covers
    only REGION_TERMS, so tier SIGNALS were never captured; this closes that gap.
    Longest-phrase-first with span consumption."""
    pairs = sorted(
        ((v, term) for term in _ALL_BROAD_TERMS for v in _region_name_variants(term)),
        key=lambda p: len(p[0]), reverse=True)
    found: set[str] = set()
    work = text_lower
    for v, term in pairs:
        pat = r"\b" + re.escape(v) + r"\b"
        if re.search(pat, work):
            found.add(term)
            work = re.sub(pat, " ", work)
    return found


# Variant(lowercased) → canonical broad term, and country(lowercased) → canonical
# country, built once for fast single-term normalisation.
_BROAD_CANON: dict[str, str] = {}
for _bt in _ALL_BROAD_TERMS:
    _BROAD_CANON.setdefault(_bt.lower(), _bt)
    for _bv in SYNONYMS.get(_bt, []):
        _BROAD_CANON.setdefault(_bv.lower(), _bt)
_COUNTRY_CANON: dict[str, str] = {c.lower(): c for c in COUNTRIES}
# UN / official LONG-FORM aliases → the canonical short name in COUNTRIES. Lets a
# call scoped to a long form (common in UN/UNGM/World-Bank sources) be compared
# correctly by the geo gate: an ELIGIBLE country's long form still matches, and a
# non-eligible one (e.g. "Lao People's Democratic Republic" → "Laos") reads as foreign.
_COUNTRY_ALIASES: dict[str, str] = {
    "lao people's democratic republic": "Laos", "lao pdr": "Laos",
    "united republic of tanzania": "Tanzania",
    "republic of korea": "South Korea",
    "democratic people's republic of korea": "North Korea",
    "viet nam": "Vietnam",
    "syrian arab republic": "Syria",
    "islamic republic of iran": "Iran",
    "bolivia (plurinational state of)": "Bolivia",
    "plurinational state of bolivia": "Bolivia",
    "venezuela (bolivarian republic of)": "Venezuela",
    "bolivarian republic of venezuela": "Venezuela",
    "republic of moldova": "Moldova",
    "republic of the congo": "Congo (Brazzaville)",
    "democratic republic of congo": "Congo (DRC)",
    "democratic republic of the congo": "Congo (DRC)",
    "brunei darussalam": "Brunei",
    "kyrgyz republic": "Kyrgyzstan",
    "state of palestine": "Palestine",
    "republic of türkiye": "Turkey", "türkiye": "Turkey",
    # Common abbreviation / long-form variants so an HQ-country gate matches regardless of
    # how the country was typed (e.g. donor requires 'US', org HQ says 'United States').
    "us": "United States", "u.s.": "United States", "usa": "United States",
    "u.s.a.": "United States", "united states of america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom",
    "united kingdom of great britain and northern ireland": "United Kingdom",
    "great britain": "United Kingdom",
    "uae": "United Arab Emirates",
    "drc": "Congo (DRC)", "dr congo": "Congo (DRC)",
}
for _al, _canon in _COUNTRY_ALIASES.items():
    _COUNTRY_CANON.setdefault(_al, _canon)


def canonical_geo(term: str | None) -> str:
    """Normalise ONE extracted geographic term to the broad-geography vocabulary so
    scope is COMPARABLE across sources: 'SSA'/'sub-saharan' → 'Sub-Saharan Africa',
    'lmic'/'developing countries' → 'Low- and middle-income countries (LMICs)',
    'global south' → 'Global South', plus canonical country names. A term not in the
    library is returned UNCHANGED (free-text scope is allowed)."""
    t = str(term or "").strip()
    if not t:
        return ""
    tl = re.sub(r"\s+", " ", t.lower()).strip()
    if tl in _BROAD_CANON:
        return _BROAD_CANON[tl]
    if tl in _COUNTRY_CANON:
        return _COUNTRY_CANON[tl]
    hits = broad_geos_in_text(tl)            # e.g. "ssa region", "lmic settings"
    if hits:
        return max(hits, key=len)
    return t                                  # free term not in the library — keep as-is
