"""A published list of eligible countries outranks a regional keyword.

The SGCI/STISA call names nineteen eligible countries on its own page, under a
`COUNTRIES` heading, and the pipeline stored its scope as
`['Africa', 'Sub-Saharan Africa']` - picked up from the title ("advancing
Africa's ...") and from the PROGRAMS line ("Science Granting Councils Initiative
in Sub-Saharan Africa"). Both of those contain the tenant's countries, so every
geography gate passed a call the tenant could not apply to. The nineteen
countries were never extracted into any field, so nothing downstream could act
on them either: `eligibility_countries` was `[]`.

That is the absence-of-evidence failure in its usual shape. A broad region is
what you get when no one reads the list, and it is strictly more permissive than
the list, so the error always runs one way - toward admitting calls.

WHY THIS IS NOT JUST A LABEL SEARCH. The countries are not introduced by the
words you would guess. There is no "eligible countries" phrase anywhere on the
page: the list sits under a bare `COUNTRIES` metadata heading, and the
Eligibility section only cross-refers to "participating SGCI countries". A label
list written from imagination would have missed it, so these labels were read off
the real page.

WHY A RUN WALK AND NOT A WINDOW. `catalog_synthesis.page_text` collapses all
whitespace, so by the time we see the page the heading is inline:

    ... TOPIC(S) Science and Technology COUNTRIES Botswana Burkina Faso
    Cote d'Ivoire ... Zimbabwe FUNDED BY Science Granting Councils ...

There is no line structure left to anchor on. What IS still there is the shape of
the thing: a label followed by a dense, contiguous run of country names that ends
at the next metadata label. So we walk the run - consuming country names and the
separators between them and stopping at the first token that is neither - which
ends exactly at "FUNDED" without needing a window length or a density threshold
to tune.

Everything here is text-only and raises nothing: a detector error must never take
down a scan.
"""
from __future__ import annotations

import logging
import re

from core import geographies as geo

log = logging.getLogger(__name__)

# Curly apostrophes and dashes vary by source; "Cote d'Ivoire" must match whether
# the page used U+2019 or U+0027, and the vocabulary spells it one way.
_PUNCT_MAP = {"’": "'", "‘": "'", "ʼ": "'",
              "–": "-", "—": "-", " ": " "}


def _fold(text: str) -> str:
    """Strip diacritics, so a page's spelling need not match the vocabulary's.

    Not cosmetic. The vocabulary spells it "Cote d'Ivoire" with a circumflex and
    the page does not always, and because the walk below stops at the first token
    it cannot read, ONE unmatched spelling truncated the whole nineteen-country
    list to two. A truncated list is worse than none: it is still treated as the
    published rule, and a short list biases toward rejecting.
    """
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))


def _normalise(text: str) -> str:
    for bad, good in _PUNCT_MAP.items():
        text = text.replace(bad, good)
    return re.sub(r"\s+", " ", _fold(text))


def _country_vocabulary() -> tuple[list[str], dict[str, str]]:
    """``(spellings longest-first, folded spelling -> canonical name)``.

    Built from core.geographies so the vocabulary has ONE owner - the official
    names, the ISO-inverted forms and the aliases all live there, and a country
    added there is matched here without a second edit. The map is what turns a
    folded match back into the canonical name, since `geo.canonical_geo` is keyed
    on the accented spelling and would hand a folded one straight back.
    """
    terms = set(geo.COUNTRIES)
    terms.update(getattr(geo, "_COUNTRY_ALIASES", {}))
    canon: dict[str, str] = {}
    for t in terms:
        if not t:
            continue
        folded = _normalise(str(t)).strip()
        if not folded:
            continue
        canon.setdefault(folded.lower(), geo.canonical_geo(t) or str(t))
    return sorted(canon, key=len, reverse=True), canon


_VOCAB, _FOLDED_TO_CANON = _country_vocabulary()
_COUNTRY_RE = re.compile(
    r"(?:" + "|".join(re.escape(t) for t in _VOCAB) + r")", re.IGNORECASE)

# What may sit BETWEEN two names in a list and still be the same list. Anything
# else ends the run - which is how "… Zimbabwe FUNDED BY …" terminates at the
# right place without a window length.
_SEPARATOR_RE = re.compile(
    r"^(?:[\s,;:/|&·•\-]+|\band\b|\bor\b|\bthe\b)+", re.IGNORECASE)

# One word-ish token, for stepping over a spelling the vocabulary lacks.
_TOKEN_RE = re.compile(r"[^\s,;:/|&·•]+")
_SKIP_BUDGET = 2                  # consecutive unknown tokens tolerated
_SKIP_TOKEN_MAX = 24              # a longer token is prose, not a country name

# Labels that introduce a list of countries whose organisations MAY APPLY.
# Ordered longest/most-specific first only for readability; all are tried.
_LABELS = (
    r"eligible\s+countr(?:y|ies)",
    r"countr(?:y|ies)\s+of\s+eligibilit(?:y|ies)",
    r"countr(?:y|ies)\s+(?:that\s+are\s+)?eligible",
    r"eligible\s+(?:geographies|locations)",
    r"applicants?\s+must\s+be\s+(?:based|located|registered|established)\s+in",
    r"open\s+to\s+(?:applicants?|institutions?|organi[sz]ations?|researchers?|"
    r"consortia)\s+(?:from|in|based\s+in)",
    r"(?:applicants?|institutions?|organi[sz]ations?|researchers?|consortia)\s+"
    r"from\s+the\s+following\s+countr(?:y|ies)",
    r"following\s+(?:eligible\s+)?countr(?:y|ies)",
    r"participating\s+(?:\w+\s+)?countr(?:y|ies)",
    # The bare metadata heading the real page actually uses. Weakest of the set,
    # so it carries the extra requirements in `_MIN_FOR_BARE_LABEL`.
    r"countr(?:y|ies)",
)
_LABEL_RE = re.compile(r"(?:" + "|".join(_LABELS) + r")", re.IGNORECASE)

# The bare `countries` heading is far weaker than "eligible countries", so it has
# to earn it: a lone name after the word "countries" is ordinary prose ("…active
# in 12 countries. Kenya has …"), while a run of several is a published list.
_MIN_FOR_BARE_LABEL = 3
_BARE_LABEL_RE = re.compile(r"^countr(?:y|ies)$", re.IGNORECASE)

# Wording that makes a country list a WORK geography - where the money is spent -
# rather than a rule about who may apply. Pooling the two is the mistake
# `auto_scorer.applicant_countries` exists to prevent, so a label preceded by any
# of these is not an eligibility label at all.
_WORK_GEOGRAPHY_RE = re.compile(
    r"\b(?:target(?:ed|ing)?|beneficiar(?:y|ies)|implementation|implementing|"
    r"operating|operational|recipient|priority|intervention|programme|program|"
    r"partner|partnership|focus|working|activit(?:y|ies)|project)\b", re.I)
_LOOKBEHIND = 46          # characters before a label that establish its sense

# A list longer than this is not a rule about applicants - it is a continental or
# global work geography that landed under the wrong heading. The largest genuine
# eligibility list in the store is 36 (a foundation's intervention countries,
# where applicants must indeed be based), so this sits above the real data and
# below a mis-filed one.
MAX_PLAUSIBLE = 60


def _countries_at(text: str, pos: int) -> tuple[list[str], int]:
    """Walk the contiguous run of country names starting at `pos`.

    Returns the canonical names found and where the run ended. Consumes names and
    the separators between them, and stops at the first token that is neither.
    """
    found: list[str] = []
    i = pos
    skips = 0
    while i < len(text):
        sep = _SEPARATOR_RE.match(text[i:])
        if sep and sep.end():
            i += sep.end()
            continue
        m = _COUNTRY_RE.match(text, i)
        if m:
            found.append(_FOLDED_TO_CANON.get(m.group(0).lower(), m.group(0)))
            i = m.end()
            skips = 0                      # a hit refreshes the tolerance
            continue
        # Not a name we know. A published list may still carry a spelling this
        # vocabulary lacks ("CAR", "Congo-Brazaville" both appear in the store),
        # and stopping dead there truncates the list. So step over a SHORT token
        # and continue only if a country follows it; otherwise the run is over,
        # which is what ends it at "FUNDED BY ...".
        if skips >= _SKIP_BUDGET:
            break
        tok = _TOKEN_RE.match(text, i)
        if not tok or len(tok.group(0)) > _SKIP_TOKEN_MAX:
            break
        nxt = tok.end()
        sep2 = _SEPARATOR_RE.match(text[nxt:])
        if sep2:
            nxt += sep2.end()
        if not _COUNTRY_RE.match(text, nxt):
            break
        skips += 1
        i = nxt
    return found, i


def extract(text: str | None) -> tuple[list[str], str]:
    """``(canonical eligible countries, the label that anchored them)``.

    ``([], "")`` when the page publishes no such list - which must be read as "we
    do not know", never as "no restriction". Never raises.
    """
    try:
        blob = _normalise(text or "")
        if not blob:
            return [], ""
        best: list[str] = []
        best_label = ""
        for m in _LABEL_RE.finditer(blob):
            label = m.group(0)
            before = blob[max(0, m.start() - _LOOKBEHIND):m.start()]
            if _WORK_GEOGRAPHY_RE.search(before):
                continue                      # a work geography, not an applicant rule
            names, _ = _countries_at(blob, m.end())
            uniq: list[str] = []
            for n in names:
                if n not in uniq:
                    uniq.append(n)
            if _BARE_LABEL_RE.match(label.strip()) and len(uniq) < _MIN_FOR_BARE_LABEL:
                continue
            if not uniq or len(uniq) > MAX_PLAUSIBLE:
                continue
            # The longest published run is the list; a later passing mention of one
            # or two countries must not displace it.
            if len(uniq) > len(best):
                best, best_label = uniq, label.strip()
        return best, best_label
    except Exception as exc:                  # never break a scan over a detector
        log.debug("eligible_countries.extract failed: %s", exc)
        return [], ""


def drop_broad_when_listed(scope, listed) -> list[str]:
    """`scope` with broad regions removed, once specific countries are published.

    This is the part that makes the list BITE. Leaving 'Sub-Saharan Africa'
    alongside the nineteen names changes nothing: the geography gate reads the
    scope and a region that contains the tenant's country still passes. A named
    list is narrower than any region that contains it, so when a call publishes
    one, the region is no longer the call's eligibility - it is background.

    Income tiers and 'Global / worldwide' go too, for the same reason and with the
    same direction of error.
    """
    if not listed:
        return [str(s) for s in (scope or [])]
    broad = {str(b).strip().lower() for b in getattr(geo, "BROAD_GEOGRAPHIES", [])}
    broad |= {"global / worldwide", "global south",
              "low- and middle-income countries (lmics)"}
    kept = [str(s) for s in (scope or []) if str(s).strip().lower() not in broad]
    for c in listed:
        if c not in kept:
            kept.append(c)
    return kept
