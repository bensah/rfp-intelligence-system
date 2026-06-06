"""Curated keyword vocabulary + stemmer for the Report word cloud.

The word cloud on the Report dashboard is built from RFP titles +
descriptions, NOT from the multi-tag `program_area` classifier output.
The classifier returns long labels ("Cross-cutting - Digital Health
(+AI)"); a word cloud needs individual stemmed tokens whose font size
scales to frequency.

Design — tight niche, not exhaustive
------------------------------------
`KEYWORD_STEMS` is a *curated* vocabulary of ~80 stems relevant to
global-health RFPs. Each stem maps to every surface form we expect to
see in titles (Financ → finance / financing / financed / financial /
finances). Words NOT in the vocabulary are dropped — by design — so the
cloud surfaces a focused list of niche keywords we can amplify in
future donor searches, rather than a sprawling tag cloud full of the
top 200 English words.

Display label
-------------
The dict KEY is the display label shown in the cloud (capitalised,
human-readable: "Financing", "Diagnostic", "HIV/AIDS"). The list VALUES
are the lowercased surface forms matched in titles.

Usage
-----
    from core.keyword_cloud import extract_keyword_frequencies
    freq = extract_keyword_frequencies(["HIV vaccine trial in a low- and middle-income country"])
    # → {"HIV": 1, "Vaccine": 1}
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

# ---------------------------------------------------------------------------
# Curated stem map — the entire vocabulary the word cloud will count.
# Keys are display labels; values are the lowercased surface forms.
# ---------------------------------------------------------------------------
# Style:
#   * Plural / verb / adjective forms collapse to the same stem.
#   * Bare acronyms are uppercase keys with lowercase value list (matching
#     is case-insensitive on tokens but acronyms get word-boundary checks).
#   * Multi-word phrases ("public health") are deliberately omitted — the
#     tokenizer works on single words. To track phrases use program-area
#     classifier instead.
KEYWORD_STEMS: dict[str, list[str]] = {
    # ─── Core disease areas ───
    "HIV":            ["hiv"],
    "AIDS":           ["aids"],
    "TB":             ["tb", "tuberculosis"],
    "Malaria":        ["malaria"],
    "Hepatitis":      ["hepatitis"],
    "Cholera":        ["cholera"],
    "Covid":          ["covid", "covid-19", "sars-cov-2"],
    "Ebola":          ["ebola"],
    "Mpox":           ["mpox", "monkeypox"],
    "Cancer":         ["cancer", "cancers", "oncology", "oncological"],
    "Diabetes":       ["diabetes", "diabetic"],
    "Cardiovascular": ["cardiovascular", "cardiac", "cardio"],
    "Mental":         ["mental"],
    "Disease":        ["disease", "diseases"],
    "Outbreak":       ["outbreak", "outbreaks", "epidemic", "pandemic"],
    "NCD":            ["ncd", "ncds"],

    # ─── Cross-cutting themes ───
    "AMR":            ["amr", "antimicrobial"],
    "Vaccine":        ["vaccine", "vaccines", "vaccination", "vaccinated",
                       "vaccinating", "immunization", "immunisation"],
    "Diagnostic":     ["diagnosis", "diagnostic", "diagnostics", "diagnose",
                       "diagnosing", "diagnosed"],
    "Therapy":        ["therapy", "therapeutic", "therapeutics", "therapies"],
    "Treatment":      ["treatment", "treatments", "treat", "treating", "treated"],
    "Prevention":     ["prevention", "preventive", "preventative", "prevent",
                       "preventing"],
    "Screening":      ["screening", "screen", "screened"],
    "Surveillance":   ["surveillance", "surveil", "surveilling", "surveilled"],
    "Research":       ["research", "researcher", "researchers", "researching"],
    "Innovation":     ["innovation", "innovations", "innovative",
                       "innovate", "innovating"],
    "Digital":        ["digital", "digitization", "digitisation",
                       "digitize", "digitise"],
    "AI":             ["ai", "artificial-intelligence",
                       "artificialintelligence"],
    "Data":           ["data", "dataset", "datasets"],
    "Analytics":      ["analytics", "analytic", "analysis", "analyses",
                       "analyse", "analyze", "analysing", "analyzing"],
    "Climate":        ["climate", "climatic"],
    "Nutrition":      ["nutrition", "nutritional", "malnutrition",
                       "undernutrition"],
    "Gender":         ["gender"],
    "Pollution":      ["pollution", "pollutant", "pollutants"],

    # ─── Populations ───
    "Maternal":       ["maternal", "maternity", "mother", "mothers"],
    "Child":          ["child", "children", "childhood", "pediatric",
                       "paediatric", "pediatrics", "paediatrics"],
    "Adolescent":     ["adolescent", "adolescents", "adolescence", "youth"],
    "Women":          ["women", "woman"],

    # ─── SRH ───
    "Reproductive":   ["reproductive", "reproduction"],
    "Sexual":         ["sexual", "sexuality"],
    "Family":         ["family", "families"],
    "Contraception":  ["contraception", "contraceptive", "contraceptives"],
    "STI":            ["sti", "stis"],

    # ─── Health-system building blocks ───
    "Health":         ["health", "healthy", "healthcare"],
    "Financing":      ["finance", "financing", "financed", "finances",
                       "financial"],
    "Workforce":      ["workforce", "personnel", "staffing"],
    "Supply":         ["supply", "supplies", "procurement"],
    "Pharmaceutical": ["pharmaceutical", "pharmaceuticals", "pharma",
                       "medicine", "medicines", "drug", "drugs"],
    "Insurance":      ["insurance", "insure", "insured"],
    "Policy":         ["policy", "policies"],
    "Governance":     ["governance", "govern", "governing"],
    "Capacity":       ["capacity", "capacities", "capability", "capabilities"],
    "Training":       ["training", "train", "trained", "trainee", "trainees"],
    "Education":      ["education", "educate", "educated", "educational"],

    # ─── Action verbs / programmatic themes ───
    "Strengthen":     ["strengthen", "strengthening", "strengthened"],
    "Scale":          ["scale", "scaling", "scaled", "scale-up", "scaleup"],
    "Implementation": ["implementation", "implement", "implementing",
                       "implemented"],
    "Evaluation":     ["evaluation", "evaluate", "evaluating", "evaluated",
                       "monitoring"],
    "Pilot":          ["pilot", "pilots", "piloting", "piloted"],
    "Intervention":   ["intervention", "interventions", "intervene"],

    # ─── Geography modifiers (kept as keywords on purpose) ───
    "Africa":         ["africa", "african"],
    "Rural":          ["rural"],
    "Urban":          ["urban"],
    "LMIC":           ["lmic", "lmics"],

    # ─── Other recurring RFP themes ───
    "Equity":         ["equity", "equitable"],
    "Sustainability": ["sustainability", "sustainable", "sustain", "sustained"],
    "Resilience":     ["resilience", "resilient"],
    "Emergency":      ["emergency", "emergencies"],
    "Humanitarian":   ["humanitarian"],
    "Refugee":        ["refugee", "refugees", "displaced", "displacement"],
    "Conflict":       ["conflict", "conflicts"],
    "Water":          ["water", "wash"],
    "Sanitation":     ["sanitation"],
    "Hygiene":        ["hygiene"],
    "Energy":         ["energy"],
    "Agriculture":    ["agriculture", "agricultural", "farming", "farm"],
    "Food":           ["food", "foods"],
    "Behavior":       ["behavior", "behaviour", "behavioral", "behavioural"],
    "Community":      ["community", "communities", "community-based"],
    "Awareness":      ["awareness", "aware"],
    "Advocacy":       ["advocacy", "advocate", "advocates", "advocating"],
    "Partnership":    ["partnership", "partnerships", "partner", "partners"],
}


# ---------------------------------------------------------------------------
# Inverse map (built once at import) — used for O(1) lookup per token.
# ---------------------------------------------------------------------------
_WORD_TO_STEM: dict[str, str] = {}
for _stem, _words in KEYWORD_STEMS.items():
    for _w in _words:
        _WORD_TO_STEM[_w.lower()] = _stem


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------
# Match runs of letters (+ hyphens / slashes for "covid-19" / "HIV/AIDS").
# Split into individual tokens — multi-word matching is the classifier's
# job, not this module's.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-/]*")


def _tokenize(text: str) -> list[str]:
    """Lowercased word tokens from `text`. Splits hyphenated + slashed
    forms ("covid-19" → ["covid", "19"]; "HIV/AIDS" → ["hiv", "aids"]).
    Numbers-only tokens dropped."""
    raw = _TOKEN_RE.findall(text)
    out: list[str] = []
    for tok in raw:
        # Split on hyphen and slash so "covid-19" → covid, "HIV/AIDS" → hiv, aids
        for piece in re.split(r"[-/]", tok):
            piece = piece.lower().strip()
            if piece and not piece.isdigit():
                out.append(piece)
    return out


def extract_keyword_frequencies(
    texts: Iterable[str],
    *,
    min_count: int = 1,
) -> dict[str, int]:
    """Aggregate keyword frequencies across an iterable of text strings.

    Pipeline per text:
      1. Tokenize (single words, lowercased, hyphens/slashes split).
      2. Look up each token in the curated stem map.
      3. Skip if not in vocabulary (the niche-only filter — by design).
      4. Increment the display-label counter.

    Args:
      texts: iterable of raw text strings (RFP title + description).
      min_count: drop stems appearing fewer than this many times across
        the entire corpus. Default 1 (keep everything).

    Returns:
      {display_label: count} dict, sorted in caller's preferred order.
    """
    counter: Counter[str] = Counter()
    for text in texts:
        if not text:
            continue
        for tok in _tokenize(str(text)):
            stem = _WORD_TO_STEM.get(tok)
            if stem is not None:
                counter[stem] += 1
    if min_count > 1:
        return {k: v for k, v in counter.items() if v >= min_count}
    return dict(counter)


def vocabulary_size() -> int:
    """Number of distinct stems in the curated vocabulary."""
    return len(KEYWORD_STEMS)
