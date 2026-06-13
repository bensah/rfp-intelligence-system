"""Geographic options — UN M49 regions/subregions, income tiers, and countries.

Powers the donor "Funded geographies" field + the donor filters, so a tenant can
drill from a region (e.g. Sub-Saharan Africa) or tier (LMICs) down to a specific
country. Each donor records the regions/countries where it actually funds, so a
reviewer sees the donor's true coverage instead of a single placeholder country.

`expand(selection)` widens a chosen region/tier into the countries it contains,
so a filter for "Sub-Saharan Africa" also matches a donor tagged with "Kenya".
"""
from __future__ import annotations

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
