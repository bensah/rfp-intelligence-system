"""Keyword-based program-area classifier.

Maps RFP text (title + brief description) to one or more canonical
program areas from `config/dropdowns.yaml → program_areas`. Used at
scan time to populate `rfp_submissions.program_area` with rich
multi-area tags, and used at display time on the Report dashboard to
build the program-area distribution chart + keyword-success table.

Design choices
--------------
  * **Multi-tag**: an RFP about HIV vaccines for adolescents legitimately
    spans WCH-Vaccines + IDs-HIV/AIDS + WCH-SRH. The classifier returns
    a LIST, not a single best match — let downstream filters / KPIs
    decide whether to count once per area or pick one.
  * **Last resort "Unspecified Program Area"** (not "Other") when zero
    keywords match. "Other" implies a real bucket; Unspecified is
    explicit that the algorithm couldn't classify.
  * **Substring match, case-insensitive** — fast, predictable, no
    tokenizer ambiguities. The keyword bags below were curated to be
    discriminating: each keyword is specific enough that a substring
    hit reliably indicates the topic.
  * **Keyword bags are visible in code**, not config, so it's easy to
    review at PR time which words classify into what. Future move: lift
    them into `config/program_area_keywords.yaml` once the team wants
    to tune without code edits.
"""
from __future__ import annotations

import re

UNSPECIFIED = "Unspecified Program Area"


# ============================================================================
# Keyword bags
# ============================================================================
# Each entry: canonical program area → list of substring keywords (case
# insensitive). The substring match treats the keyword as a regex \b word
# boundary so "HIV" doesn't fire on "shiver" but does fire on "HIV-positive".
# Add new keywords as they come up — the matcher is forgiving on word
# boundaries so plurals and hyphenations don't need separate entries.
# ============================================================================
PROGRAM_AREA_KEYWORDS: dict[str, list[str]] = {
    # ---- WCH (Women & Children's Health) -----------------------------------
    "WCH - Vaccines": [
        "vaccine", "vaccination", "immunization", "immunisation",
        "EPI", "Expanded Programme on Immunization",
        "BCG", "DTP", "MMR", "HPV vaccine",
    ],
    "WCH - SRH": [
        "sexual and reproductive health", "reproductive health", "SRH",
        "family planning", "contraception", "contraceptive",
        "FGM", "female genital mutilation",
        "abortion", "post-abortion", "safe abortion",
        "menstrual", "menopause", "fertility", "infertility",
        "STI", "STD", "sexually transmitted",
        "HPV", "syphilis", "gonorrhea", "gonorrhoea",
        "chlamydia", "trichomoniasis",
        "adolescent health", "youth-friendly",
    ],
    "WCH - Nutrition": [
        "nutrition", "malnutrition", "undernutrition",
        "stunting", "wasting", "underweight",
        "obesity", "overweight",
        "food security", "food insecurity",
        "anemia", "anaemia",
        "micronutrient", "vitamin A", "iron deficiency",
        "zinc supplementation", "iodine deficiency",
        "breastfeeding", "complementary feeding",
        "RUTF", "ready-to-use therapeutic food",
        "food fortification", "biofortification",
    ],
    "WCH - MNCH": [
        "maternal", "newborn", "child health", "pediatric", "paediatric",
        "infant mortality", "under-5", "under five",
        "MNCH", "RMNCH", "RMNCAH",
        "maternal mortality", "maternal death",
        "neonatal", "stillbirth", "obstetric",
        "midwife", "midwifery",
        "antenatal", "postnatal", "postpartum", "perinatal",
        "skilled birth attendant", "skilled birth",
        "kangaroo mother care", "essential newborn",
    ],
    # ---- NCDs (Non-Communicable Diseases) ----------------------------------
    "NCDs - Mental Health": [
        "mental health", "depression", "anxiety",
        "psychiatric", "psychiatry", "psychological", "psychology",
        "psychosocial", "suicide", "self-harm",
        "PTSD", "post-traumatic stress",
        "schizophrenia", "bipolar",
        "substance use", "substance abuse", "addiction",
        "well-being", "wellbeing", "well being",
        "MHPSS", "mental health and psychosocial support",
    ],
    "NCDs - Diabetes": [
        "diabetes", "diabetic", "insulin",
        "glucose", "glycemic", "glycaemic",
        "type 1 diabetes", "type 2 diabetes", "T1D", "T2D",
        "hyperglycemia", "hyperglycaemia",
        "metformin", "blood sugar",
    ],
    "NCDs - Cardiovascular Diseases": [
        "cardiovascular", "cardiac", "heart disease",
        "hypertension", "blood pressure",
        "stroke", "atherosclerosis", "coronary",
        "CVD", "cholesterol", "cardiology", "ischemic", "ischaemic",
        "rheumatic heart",
    ],
    "NCDs - Cancer": [
        "cancer", "oncology", "oncological",
        "tumor", "tumour", "carcinoma",
        "leukemia", "leukaemia", "lymphoma",
        "chemotherapy", "radiotherapy",
        "metastasis", "metastatic", "malignancy", "neoplasm",
        "cervical cancer", "breast cancer", "prostate cancer",
        "colorectal cancer", "lung cancer",
        "palliative care",
    ],
    # ---- IDs (Infectious Diseases) -----------------------------------------
    "IDs - Tuberculosis": [
        "tuberculosis",
        "MDR-TB", "XDR-TB", "drug-resistant TB",
        "DOTS", "mycobacterium tuberculosis",
        "latent TB", "TB infection",
        # Note: bare "TB" handled via word-boundary regex to avoid
        # false positives on words containing "tb" as a substring.
    ],
    "IDs - Pandemic Response": [
        "pandemic", "epidemic", "outbreak",
        "COVID", "COVID-19", "SARS-CoV", "long covid",
        "SARS", "MERS",
        "Ebola", "Marburg", "Zika", "Nipah",
        "monkeypox", "mpox",
        "avian flu", "avian influenza", "H5N1", "H1N1",
        "preparedness", "emergency response",
        "biosecurity", "biosafety",
        "IHR", "International Health Regulations",
    ],
    "IDs - Malaria & NTDs": [
        "malaria", "Plasmodium", "antimalarial",
        "mosquito", "bed net", "ITN", "IRS",
        "insecticide-treated",
        "neglected tropical disease", "NTD",
        "schistosomiasis", "lymphatic filariasis",
        "onchocerciasis", "river blindness",
        "trachoma", "leprosy", "Chagas", "leishmaniasis",
        "soil-transmitted helminth", "STH",
        "guinea worm", "dracunculiasis",
        "dengue", "yellow fever", "chikungunya",
    ],
    "IDs - HIV/AIDS": [
        "HIV/AIDS", "HIV-AIDS",
        "antiretroviral", "ART therapy", "ARV",
        "PrEP", "PEP", "PEPFAR",
        "viral load", "viral suppression",
        "HIV-positive", "PLHIV", "people living with HIV",
        "vertical transmission", "mother-to-child transmission",
        "MTCT",
        # Note: bare "HIV" and "AIDS" handled via word-boundary regex.
    ],
    "IDs - Hepatitis": [
        "hepatitis", "HBV", "HCV",
        "hepatitis B", "hepatitis C", "hepatitis A",
        "viral hepatitis", "liver fibrosis", "cirrhosis",
    ],
    "IDs - Antimicrobial Resistance (AMR)": [
        "antimicrobial resistance", "antibiotic resistance", "drug resistance",
        "antimicrobial stewardship", "antibiotic stewardship", "AMR", "AMU",
        "rational antibiotic", "AWaRe", "diagnostic stewardship",
    ],
    # ---- HSS (Health Systems Strengthening) --------------------------------
    "HSS - Health Workforce": [
        "health workforce", "community health worker", "CHW",
        "health worker", "nurse", "nursing",
        "physician training", "medical training",
        "task-shifting", "task shifting",
        "human resources for health", "HRH",
        "midwife training", "clinical training",
        "continuing medical education", "CME",
    ],
    "HSS - Health Financing": [
        "health financing", "universal health coverage", "UHC",
        "health insurance", "national health insurance",
        "social health insurance", "community-based insurance",
        "out-of-pocket", "catastrophic expenditure",
        "results-based financing", "RBF",
        "performance-based financing", "PBF",
        "strategic purchasing", "domestic resource mobilization",
    ],
    # ---- Cross-cutting -----------------------------------------------------
    "Cross-cutting - Market Shaping": [
        "market shaping", "market access",
        "pricing", "price negotiation",
        "procurement", "pooled procurement",
        "supply chain", "commodity security",
        "demand forecasting",
        "local manufacturing", "regional manufacturing",
        "medicines access", "essential medicines",
    ],
    "Cross-cutting - Digital Health (+AI)": [
        "digital health", "eHealth", "mHealth",
        "telemedicine", "telehealth",
        "artificial intelligence", "machine learning",
        "electronic health record", "EHR", "EMR",
        "digital tools", "health technology", "HealthTech",
        "interoperability", "health information system", "HMIS", "DHIS2",
        "digital platform", "mobile application",
    ],
    "Cross-cutting - Diagnostics": [
        "diagnostic", "diagnosis",
        "rapid test", "rapid diagnostic test", "RDT",
        "point-of-care", "POC test",
        "molecular test", "molecular diagnostic",
        "laboratory strengthening", "lab-based",
        "screening program", "in vitro diagnostic", "IVD",
        "biomarker", "test-and-treat",
    ],
    "Cross-cutting - Climate & Health": [
        "climate change", "climate-resilient", "climate resilient",
        "environmental health", "air quality", "air pollution",
        "extreme weather", "heat stress", "heatwave",
        "vector-borne", "vector control",
        "WASH", "water sanitation hygiene",
        "safe water", "sanitation",
        "planetary health", "one health",
    ],
    "Cross-cutting - Assistive Technology": [
        "assistive technology", "assistive device",
        "disability", "persons with disabilities",
        "rehabilitation", "physiotherapy",
        "prosthetic", "prosthesis", "orthotic",
        "wheelchair", "hearing aid", "mobility aid",
        "accessibility", "inclusive design",
    ],
    "Cross-cutting - Research": [
        "research grant", "research program",
        "implementation research", "operational research",
        "health systems research",
        "qualitative research", "quantitative study",
        "randomized controlled trial", "RCT",
        "evidence generation", "evidence-based",
        "scientific publication", "peer-reviewed",
    ],
    # ====================================================================
    # Beyond health — social & development sectors (same canonical-key
    # convention "PREFIX - Sub-area"; PREFIX expanded in CATEGORY_FULL below).
    # ====================================================================
    # ---- EDU (Education & Learning) ----------------------------------------
    "EDU - Early Childhood Development": [
        "early childhood", "ECD", "preschool", "pre-primary", "early learning",
    ],
    "EDU - Basic Education": [
        "primary education", "secondary education", "basic education",
        "schooling", "school enrolment", "school enrollment",
        "girls education", "out-of-school", "education access",
    ],
    "EDU - Higher Education & TVET": [
        "higher education", "tertiary education", "university",
        "vocational", "TVET", "technical and vocational", "skills training",
        "apprenticeship",
    ],
    "EDU - Literacy & Numeracy": [
        "literacy", "numeracy", "foundational learning", "reading outcomes",
        "teaching at the right level", "TaRL", "learning poverty",
    ],
    "EDU - Education Technology": [
        "edtech", "education technology", "e-learning", "digital learning",
        "remote learning", "learning platform",
    ],
    # ---- ECON (Economic Development & Livelihoods) -------------------------
    "ECON - Financial Inclusion": [
        "financial inclusion", "microfinance", "mobile money", "savings group",
        "credit access", "fintech", "digital finance", "banking the unbanked",
    ],
    "ECON - MSME & Entrepreneurship": [
        "MSME", "SME", "small business", "entrepreneurship",
        "enterprise development", "startup", "incubation", "accelerator",
    ],
    "ECON - Jobs & Skills": [
        "employment", "job creation", "workforce development", "livelihoods",
        "decent work", "income generation", "self-employment",
    ],
    "ECON - Social Protection": [
        "social protection", "cash transfer", "safety net", "social safety net",
        "unconditional cash", "social assistance", "graduation approach",
    ],
    "ECON - Trade & Markets": [
        "trade facilitation", "market systems", "value chain", "market access",
        "regional integration", "export promotion",
    ],
    # ---- AGRI (Agriculture & Food Systems) ---------------------------------
    "AGRI - Smallholder Productivity": [
        "smallholder", "crop yield", "agricultural productivity", "farm inputs",
        "improved seeds", "fertilizer", "agricultural extension",
    ],
    "AGRI - Food Security & Resilience": [
        "food security", "food insecurity", "seasonal hunger", "famine",
        "resilient food systems", "food systems",
    ],
    "AGRI - Climate-Smart Agriculture": [
        "climate-smart agriculture", "agroecology", "drought-resistant",
        "rainwater harvesting", "irrigation", "regenerative agriculture",
    ],
    "AGRI - Livestock & Fisheries": [
        "livestock", "poultry", "fisheries", "aquaculture", "animal health",
        "pastoralist", "rangeland",
    ],
    # ---- WASH (Water, Sanitation & Hygiene) --------------------------------
    "WASH - Safe Water": [
        "safe water", "drinking water", "water supply", "water access",
        "water treatment", "chlorination", "borehole", "piped water",
    ],
    "WASH - Sanitation": [
        "sanitation", "latrine", "toilet", "open defecation",
        "faecal sludge", "fecal sludge", "sewerage", "CLTS",
    ],
    "WASH - Hygiene": [
        "hygiene promotion", "handwashing", "menstrual hygiene",
        "WASH in schools", "hygiene behaviour",
    ],
    # ---- ENV (Climate, Energy & Environment) -------------------------------
    "ENV - Climate Adaptation & Resilience": [
        "climate adaptation", "climate resilience", "disaster risk reduction",
        "DRR", "early warning system", "flood resilience", "drought resilience",
    ],
    "ENV - Clean & Renewable Energy": [
        "renewable energy", "solar power", "clean energy", "energy access",
        "off-grid", "mini-grid", "clean cooking", "electrification",
    ],
    "ENV - Biodiversity & Conservation": [
        "biodiversity", "conservation", "deforestation", "reforestation",
        "ecosystem restoration", "wildlife", "marine protection",
    ],
    "ENV - Pollution & Waste": [
        "pollution", "air pollution", "plastic pollution", "waste management",
        "recycling", "lead exposure", "circular economy",
    ],
    # ---- GOV (Governance, Peace & Rights) ----------------------------------
    "GOV - Democracy & Civic Participation": [
        "democracy", "elections", "civic participation", "civil society",
        "citizen engagement", "civic space",
    ],
    "GOV - Anti-corruption & Accountability": [
        "anti-corruption", "transparency", "accountability",
        "public financial management", "PFM", "open government", "audit",
    ],
    "GOV - Human Rights & Justice": [
        "human rights", "rule of law", "access to justice", "legal aid",
        "rights-based", "civic freedoms",
    ],
    "GOV - Peace & Conflict": [
        "peacebuilding", "conflict prevention", "fragility", "stabilization",
        "social cohesion", "violence prevention", "countering violent extremism",
    ],
    # ---- GES (Gender, Equity & Inclusion) ----------------------------------
    "GES - Gender Equality & GBV": [
        "gender equality", "gender-based violence", "GBV", "VAWG",
        "women empowerment", "women's empowerment", "gender mainstreaming",
    ],
    "GES - Disability Inclusion": [
        "disability inclusion", "inclusive education", "inclusive employment",
        "universal design",
    ],
    "GES - Youth Empowerment": [
        "youth empowerment", "young people", "youth development",
        "youth leadership", "adolescent programming",
    ],
    "GES - Migration & Displacement": [
        "refugee", "migrant", "displacement", "internally displaced", "IDP",
        "asylum seeker", "forced migration", "host community",
    ],
    # ---- HUM (Humanitarian & Resilience) -----------------------------------
    "HUM - Emergency Response": [
        "humanitarian", "emergency response", "disaster response",
        "relief operation", "crisis response", "rapid response",
    ],
    "HUM - Food Assistance": [
        "food assistance", "food aid", "in-kind food", "emergency nutrition",
        "general food distribution",
    ],
    "HUM - Shelter & Settlements": [
        "shelter", "settlement", "camp management", "non-food items",
        "transitional shelter",
    ],
}


# Short codes that need word-boundary matching to avoid false positives
# (the "TB" / "HIV" / "AIDS" / "AT" suffix-of-word trap).
_BARE_ACRONYMS: dict[str, list[str]] = {
    "IDs - Tuberculosis": ["TB"],
    "IDs - HIV/AIDS": ["HIV", "AIDS"],
    "Cross-cutting - Assistive Technology": ["AT"],
}


# ---------------------------------------------------------------------------
# Two-level taxonomy (display + hierarchy) layered on top of the canonical keys.
# The keys above ("IDs - Tuberculosis") stay the single source of truth for
# classification, donor fit + matching; this layer just lets every FORM show a
# clean hierarchy — pick a high-level Category, then drill into its sub-areas —
# while storing canonical keys (or a Category name for a broad pick).
# ---------------------------------------------------------------------------
CATEGORY_FULL: dict[str, str] = {
    # Health
    "WCH": "Women & Children's Health",
    "NCDs": "Non-Communicable Diseases",
    "IDs": "Infectious Diseases",
    "HSS": "Health System Strengthening",
    "Cross-cutting": "Cross-cutting (Health)",
    # Social & development
    "EDU": "Education & Learning",
    "ECON": "Economic Development & Livelihoods",
    "AGRI": "Agriculture & Food Systems",
    "WASH": "Water, Sanitation & Hygiene (WASH)",
    "ENV": "Climate, Energy & Environment",
    "GOV": "Governance, Peace & Rights",
    "GES": "Gender, Equity & Inclusion",
    "HUM": "Humanitarian & Resilience",
}


def _split_key(key: str) -> tuple[str, str]:
    if " - " in (key or ""):
        pre, sub = key.split(" - ", 1)
        return pre.strip(), sub.strip()
    return "", (key or "").strip()


def subarea_label(key: str) -> str:
    """'Cross-cutting - Assistive Technology' -> 'Assistive Technology'."""
    return _split_key(key)[1]


def category_full(key: str) -> str:
    """'HSS - Health Financing' -> 'Health System Strengthening'."""
    pre, _ = _split_key(key)
    return CATEGORY_FULL.get(pre, pre or key)


# Ordered {full category: [sub-area label, ...]} for the hierarchical picker.
TAXONOMY: dict[str, list[str]] = {}
for _k in PROGRAM_AREA_KEYWORDS:
    TAXONOMY.setdefault(category_full(_k), []).append(subarea_label(_k))
CATEGORIES: list[str] = list(TAXONOMY.keys())

_KEY_BY_PAIR = {(category_full(k), subarea_label(k)): k for k in PROGRAM_AREA_KEYWORDS}
_KEYS_BY_CATEGORY: dict[str, list[str]] = {}
_KEYS_BY_SUBLABEL: dict[str, list[str]] = {}
for _k in PROGRAM_AREA_KEYWORDS:
    _KEYS_BY_CATEGORY.setdefault(category_full(_k), []).append(_k)
    _KEYS_BY_SUBLABEL.setdefault(subarea_label(_k), []).append(_k)


def key_for(full_category: str, subarea: str) -> str | None:
    """Canonical key for a (category, sub-area) pair, or None."""
    return _KEY_BY_PAIR.get((full_category, subarea))


def expand(selections) -> set[str]:
    """Expand a mix of Category names / canonical keys / bare sub-labels into the
    set of canonical sub-area keys (a Category → all its keys). Use for matching
    org ↔ RFP ↔ donor program areas regardless of how each was captured."""
    out: set[str] = set()
    for sel in (selections or []):
        s = str(sel).strip()
        if s in PROGRAM_AREA_KEYWORDS:
            out.add(s)
        elif s in _KEYS_BY_CATEGORY:
            out.update(_KEYS_BY_CATEGORY[s])
        elif s in _KEYS_BY_SUBLABEL:
            out.update(_KEYS_BY_SUBLABEL[s])
    return out


def _matches(text: str, keyword: str) -> bool:
    """Case-insensitive **word-boundary** match for ANY keyword.

    Word-boundary is the default because plain substring matching
    caused real false positives — e.g. the keyword "mental health"
    firing inside "environmental health" (Pure Earth pollution RFPs
    got tagged as Mental Health). The regex `\\b...\\b` treats hyphens
    and slashes as boundaries, so multi-word keywords like
    "HIV/AIDS" or "drug-resistant TB" still match cleanly inside
    larger phrases.
    """
    return bool(re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE))


def classify_program_areas(text: str | None) -> list[str]:
    """Return every program area whose keywords appear in `text`.

    Empty / None / no-match → ["Unspecified Program Area"].

    Result preserves declaration order of PROGRAM_AREA_KEYWORDS so
    chart facet ordering stays stable across runs.
    """
    if not text:
        return [UNSPECIFIED]
    matched: list[str] = []
    for area, kws in PROGRAM_AREA_KEYWORDS.items():
        all_kws = list(kws) + _BARE_ACRONYMS.get(area, [])
        if any(_matches(text, kw) for kw in all_kws):
            matched.append(area)
    return matched or [UNSPECIFIED]


def matched_keywords(text: str | None) -> list[tuple[str, str]]:
    """Return every (program_area, keyword) pair that fired on `text`.

    Used by the Report dashboard to power the word cloud + the keyword-
    success table (top keywords by Proceed/Submitted/Approved rate).

    Result is deduplicated per (area, keyword) so a keyword that appears
    twice in the text only counts once per RFP. Aggregating these
    across many RFPs is what makes a keyword "popular".
    """
    if not text:
        return []
    seen: set[tuple[str, str]] = set()
    for area, kws in PROGRAM_AREA_KEYWORDS.items():
        all_kws = list(kws) + _BARE_ACRONYMS.get(area, [])
        for kw in all_kws:
            if _matches(text, kw):
                seen.add((area, kw))
    return sorted(seen)
