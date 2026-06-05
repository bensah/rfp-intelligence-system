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
}


# Short codes that need word-boundary matching to avoid false positives
# (the "TB" / "HIV" / "AIDS" / "AT" suffix-of-word trap).
_BARE_ACRONYMS: dict[str, list[str]] = {
    "IDs - Tuberculosis": ["TB"],
    "IDs - HIV/AIDS": ["HIV", "AIDS"],
    "Cross-cutting - Assistive Technology": ["AT"],
}


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
