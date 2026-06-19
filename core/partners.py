"""Reusable reference lists for partner / donor-portal pickers.

ONE source for:
  * NONPROFIT_PARTNERS — curated bilateral / multilateral / INGO / philanthropy
    partners (the "Trusted non-profit partners" picker).
  * DONOR_PORTALS — top donor application/registration portals (the "Donor
    registrations (donor portal)" picker seed); merged at runtime with the
    website column of the donor_intel catalog.
  * PARTNER_FOUNDED — acronym/name -> year-of-creation, used to backfill the
    donor_intel `founded` column where it's missing (scripts/update_donor_founded.py).

Kept as structured records so the display string, the option list and the year
map all derive from the same data (no drift).
"""
from __future__ import annotations

import re

# (name, acronym, founded) — founded None where unknown.
PARTNERS: list[tuple[str, str, int | None]] = [
    ("World Health Organization", "WHO", 1948),
    ("United Nations Children's Fund", "UNICEF", 1946),
    ("World Bank Group", "WBG", 1944),
    ("International Development Association", "IDA", 1960),
    ("United Nations Population Fund", "UNFPA", 1969),
    ("Joint United Nations Programme on HIV/AIDS", "UNAIDS", 1996),
    ("United Nations Development Programme", "UNDP", 1965),
    ("World Food Programme", "WFP", 1961),
    ("Food and Agriculture Organization of the United Nations", "FAO", 1945),
    ("Gavi, the Vaccine Alliance", "Gavi", 2000),
    ("The Global Fund to Fight AIDS, Tuberculosis and Malaria", "Global Fund", 2002),
    ("Unitaid", "Unitaid", 2006),
    ("Global Financing Facility", "GFF", 2015),
    ("Coalition for Epidemic Preparedness Innovations", "CEPI", 2017),
    ("Bill & Melinda Gates Foundation", "BMGF", 2000),
    ("Wellcome Trust", "Wellcome", 1936),
    ("The Rockefeller Foundation", "RF", 1913),
    ("Ford Foundation", "FF", 1936),
    ("John D. and Catherine T. MacArthur Foundation", "MacArthur Foundation", 1970),
    ("Children's Investment Fund Foundation", "CIFF", 2002),
    ("ELMA Philanthropies", "ELMA", 2005),
    ("Bloomberg Philanthropies", "BP", None),
    ("Africa Centres for Disease Control and Prevention", "Africa CDC", 2017),
    ("Skoll Foundation", "Skoll", 1999),
    ("Conrad N. Hilton Foundation", "CNHF", 1944),
    ("Mastercard Foundation", "MCF", 2006),
    ("Co-Impact", "Co-Impact", 2017),
    ("Fondation Botnar", "Botnar", 2003),
    ("United States Agency for International Development", "USAID", 1961),
    ("Centers for Disease Control and Prevention", "CDC", 1946),
    ("U.S. President's Emergency Plan for AIDS Relief", "PEPFAR", 2003),
    ("National Institutes of Health", "NIH", 1887),
    ("Foreign, Commonwealth and Development Office", "FCDO", 2020),
    ("Agence Française de Développement", "AFD", 1941),
    ("Deutsche Gesellschaft für Internationale Zusammenarbeit", "GIZ", 2011),
    ("KfW Development Bank", "KfW", 1948),
    ("Japan International Cooperation Agency", "JICA", 1974),
    ("Korea International Cooperation Agency", "KOICA", 1991),
    ("Swiss Agency for Development and Cooperation", "SDC", 1961),
    ("Swedish International Development Cooperation Agency", "Sida", 1995),
    ("Norwegian Agency for Development Cooperation", "Norad", 1968),
    ("Expertise France", "EF", 2015),
    ("Spanish Agency for International Development Cooperation", "AECID", 1988),
    ("Italian Agency for Development Cooperation", "AICS", 2016),
    ("European Commission Directorate-General for International Partnerships", "DG INTPA", 2021),
    ("African Development Bank", "AfDB", 1964),
    ("Islamic Development Bank", "IsDB", 1975),
    ("Inter-American Development Bank", "IDB", 1959),
    ("Asian Development Bank", "ADB", 1966),
    ("PATH", "PATH", 1977),
    ("Management Sciences for Health", "MSH", 1971),
    ("Jhpiego", "Jhpiego", 1973),
    ("John Snow, Inc.", "JSI", 1978),
    ("FHI 360", "FHI 360", 2011),
    ("Abt Global", "Abt", 1965),
    ("RTI International", "RTI", 1958),
    ("Chemonics International", "Chemonics", 1975),
    ("DAI Global", "DAI", 1970),
    ("Tetra Tech", "Tetra Tech", 1966),
    ("Palladium", "Palladium", 1965),
    ("ICF", "ICF", 1969),
    ("Population Services International", "PSI", 1970),
    ("IntraHealth International", "IntraHealth", 1979),
    ("Amref Health Africa", "Amref", 1957),
    ("Project HOPE", "Project HOPE", 1958),
    ("IMA World Health", "IMA", 1960),
    ("Corus International", "Corus", 2020),
    ("World Vision International", "WVI", 1950),
    ("CARE International", "CARE", 1945),
    ("Save the Children", "SC", 1919),
    ("International Rescue Committee", "IRC", 1933),
    ("Mercy Corps", "Mercy Corps", 1979),
    ("International Medical Corps", "IMC", 1984),
    ("Médecins Sans Frontières", "MSF", 1971),
    ("Partners In Health", "PIH", 1987),
    ("Last Mile Health", "LMH", 2007),
    ("VillageReach", "VillageReach", 2000),
    ("Living Goods", "LG", 2007),
    ("mothers2mothers", "m2m", 2001),
    ("BRAC", "BRAC", 1972),
    ("Elizabeth Glaser Pediatric AIDS Foundation", "EGPAF", 1988),
    ("ICAP at Columbia University", "ICAP", 2003),
    ("Population Council", "PC", 1952),
    ("Helen Keller Intl", "HKI", 1915),
    ("Nutrition International", "NI", 1992),
    ("Results for Development", "R4D", 2008),
    ("ThinkWell", "ThinkWell", 2011),
    ("Dimagi", "Dimagi", 2002),
    ("Medic", "Medic", None),
    ("eHealth Africa", "eHA", 2009),
    ("FIND", "FIND", 2003),
    ("Drugs for Neglected Diseases initiative", "DNDi", 2003),
    ("Medicines for Malaria Venture", "MMV", 1999),
    ("TB Alliance", "TB Alliance", 2000),
    ("International AIDS Vaccine Initiative", "IAVI", 1996),
    ("International Vaccine Institute", "IVI", 1997),
    ("Sabin Vaccine Institute", "Sabin", 1993),
    ("Global Antibiotic Research and Development Partnership", "GARDP", 2016),
    ("Global Health Innovative Technology Fund", "GHIT Fund", 2013),
    ("Malaria No More", "MNM", 2006),
    # Philanthropic / pooled funders & collaborators (e.g. DIV Fund backers).
    ("Development Innovation Ventures Fund", "DIV Fund", None),
    ("Coefficient Giving", "Coefficient Giving", 2017),   # formerly Open Philanthropy
    ("GiveWell", "GiveWell", 2007),
    ("Livelihood Impact Fund", "Livelihood Impact Fund", None),
    ("CRI Foundation", "CRI Foundation", 2006),
    ("Global Development Incubator", "Global Development Incubator", 2007),
    ("Anonymous Donors", "Anonymous Donors", None),
    ("UK Department of Health and Social Care", "DHSC", 2018),
]

# Display options for partner pickers: "Name (ACRONYM)" — but just "Name" when
# there's no distinct acronym (avoids ugly "Coefficient Giving (Coefficient Giving)").
NONPROFIT_PARTNERS: list[str] = [
    name if (not acr or acr.strip().lower() == name.strip().lower()) else f"{name} ({acr})"
    for name, acr, _ in PARTNERS
]

# Unified partner vocabulary — ALL possible partner/funder types (bilaterals,
# multilaterals, INGOs, philanthropies, pooled funds, donors). The single list the
# "Funders & collaborators" (donor intel) and "Trusted partners" (org profile)
# pickers share; both accept typed additions for private firms / academic orgs.
ALL_PARTNERS: list[str] = NONPROFIT_PARTNERS

# Acronym (upper) + lowercased name -> founded year, for the donor_intel backfill.
PARTNER_FOUNDED: dict[str, int] = {}
for _name, _acr, _yr in PARTNERS:
    if _yr:
        PARTNER_FOUNDED[_acr.strip().lower()] = _yr
        PARTNER_FOUNDED[_name.strip().lower()] = _yr

def clean_portal_url(url) -> str:
    """Bare, clean host for a donor portal — drop scheme, leading 'www.', any
    path / query / fragment, and any '(label)'. Lowercased.
    'https://www.wellcome.org/funding/schemes' -> 'wellcome.org'."""
    s = str(url or "").strip().lower()
    s = re.sub(r"\s*\(.*$", "", s)          # drop "(EU Funding & Tenders…)" labels
    s = re.sub(r"^[a-z]+://", "", s)        # drop scheme (https://, http://)
    s = re.sub(r"^www\.", "", s)            # drop leading www.
    s = re.split(r"[/?#]", s, 1)[0]         # host only — no subpages
    return s.strip().strip(".")


# Top donor application / registration portals (seed, clean bare hosts). Merged
# + de-duplicated at runtime with the donor_intel `website` column (also cleaned
# via clean_portal_url); "Other" lets users add more.
DONOR_PORTALS: list[str] = [
    "grants.gov", "sam.gov", "gcgh.grandchallenges.org", "wellcome.org",
    "gatesfoundation.org", "theglobalfund.org", "gavi.org", "unitaid.org",
    "ec.europa.eu", "ungm.org", "workwithusaid.gov", "grants.nih.gov",
    "cdc.gov", "fcdo.gov.uk", "afd.fr", "afdb.org",
]
