"""`opportunity_type` and `instrument_type` are two axes, not two guesses at one answer.

A reviewer opened a call reading **Instrument: Contract** directly above **Opportunity type:
grant** and reasonably took it for the extraction contradicting itself. It is not. The two
columns answer different questions, and `core/type_detect` has said so since the ontology was
written — but nothing on the page or in the data ever expressed the relationship, so two
independent facts sat side by side looking like a disagreement.

    opportunity_type   WHAT PURSUING THIS IS, before you win it. The coarse pursuit class:
                       a funding call, a procurement, a consultancy, a prize. This is the
                       axis the eligibility gate opts out of — a grant-seeking org does not
                       want procurements.

    instrument_type    THE VEHICLE IF YOU WIN. What the donor↔beneficiary relationship
                       becomes: a grant agreement, a cooperative agreement, a contract, a
                       loan, a fellowship.

The order matters: one is BEFORE the award, the other is AFTER it. So "a grant call awarded
as a Contract" is ordinary — a grant is contracted once awarded, and a funder that words its
agreement as a contract has not changed what the opportunity was. 30 live rows are that
shape, and none of them is an error.

WHAT THIS MODULE ADDS
---------------------
  canon_opportunity   one vocabulary. The column drifted: "Grant/funding call" on 325 rows
                      but bare "grant" on 23, "Announcement" on 11 and "announcement" on 44,
                      "award" on 7. Same fact, five spellings.
  complement          fill a missing axis from the one present, because they IMPLY each
                      other. 148 rows say Procurement with no instrument; a procurement is
                      awarded as a contract. 30 say a funding call with no instrument.
  pairing             one sentence a reviewer reads once ("Grant/funding call, awarded as a
                      Grant") instead of two rows they have to reconcile themselves.
  coherence           and only THEN, flag the pairs that are genuinely odd. Measured over
                      686 rows that is 5 of them — a procurement issuing a grant, a
                      procurement issuing equity. Everything else is a normal combination,
                      an unclassified announcement, or a missing half.

The point is to stop crying wolf. A "conflict" warning on 30 legitimate grant-contract rows
teaches a reviewer to ignore the warning that matters on the 5.
"""
from __future__ import annotations

from typing import Any

from core.type_detect import INSTRUMENT_TYPES, OPPORTUNITY_TYPES

# The column was written by more than one code path over time, so the same fact arrived in
# several spellings. Canonicalising is what makes any pair rule possible at all.
_OPPORTUNITY_SYNONYMS = {
    "grant": "Grant/funding call",
    "grants": "Grant/funding call",
    "grant/funding call": "Grant/funding call",
    "funding call": "Grant/funding call",
    "funding opportunity": "Grant/funding call",
    "call for proposals": "Grant/funding call",
    "procurement": "Procurement",
    "tender": "Procurement",
    "goods/services": "Procurement",
    "consultancy": "Consultancy",
    "consulting": "Consultancy",
    "individual consultant": "Consultancy",
    "training": "Training",
    "loan": "Loan",
    "credit": "Loan",
    "prize/challenge": "Prize/Challenge",
    "prize": "Prize/Challenge",
    "challenge": "Prize/Challenge",
    "award": "Prize/Challenge",
    "announcement": "Announcement",
    "other": "Other",
}

_INSTRUMENT_SYNONYMS = {
    "grant": "Grant",
    "grant agreement": "Grant",
    "cooperative agreement": "Cooperative Agreement",
    "co-operative agreement": "Cooperative Agreement",
    "contract": "Contract",
    "services contract": "Contract",
    "framework contract": "Contract",
    "loan": "Loan",
    "equity/investment": "Equity/Investment",
    "equity": "Equity/Investment",
    "investment": "Equity/Investment",
    "prize/award": "Prize/Award",
    "prize": "Prize/Award",
    "award": "Prize/Award",
    "fellowship": "Fellowship",
    "scholarship": "Scholarship",
    "seed fund": "Seed fund",
    "in-kind/ta": "In-kind/TA",
    "technical assistance": "In-kind/TA",
    "other": "Other",
}

# "Announcement" and "Other" mean "we could not tell", NOT a pursuit class. 55 live rows are
# announcements, so treating them as a classified value would have any pairing rule judging
# a value that was never asserted.
UNCLASSIFIED = ("Announcement", "Other")

# The instrument each pursuit class is normally awarded through. Grant calls carry the
# longest list because a funder's chosen vehicle varies far more than the nature of the
# opportunity does — US federal grant programmes routinely award cooperative agreements, and
# some word the agreement itself as a contract.
NORMAL_PAIRS: dict[str, tuple[str, ...]] = {
    "Grant/funding call": ("Grant", "Cooperative Agreement", "Contract", "Prize/Award",
                           "Fellowship", "Scholarship", "Seed fund", "In-kind/TA", "Other"),
    "Procurement": ("Contract", "Other"),
    "Consultancy": ("Contract", "In-kind/TA", "Other"),
    "Training": ("Grant", "Contract", "In-kind/TA", "Other"),
    "Loan": ("Loan", "Equity/Investment", "Other"),
    "Prize/Challenge": ("Prize/Award", "Grant", "Seed fund", "Other"),
}

# What the other axis is when only one is known. Deliberately conservative: only stated
# where the implication is near-certain, so a guess never hardens into a stored fact.
_IMPLIED_INSTRUMENT = {
    "Procurement": "Contract",
    "Consultancy": "Contract",
    "Loan": "Loan",
    "Grant/funding call": "Grant",
    "Prize/Challenge": "Prize/Award",
}
_IMPLIED_OPPORTUNITY = {
    "Contract": "Procurement",
    "Cooperative Agreement": "Grant/funding call",
    "Grant": "Grant/funding call",
    "Loan": "Loan",
    "Equity/Investment": "Loan",
    "Fellowship": "Grant/funding call",
    "Scholarship": "Grant/funding call",
    "Prize/Award": "Prize/Challenge",
    "Seed fund": "Grant/funding call",
}

# How the instrument reads in a sentence, so the pairing is English rather than two labels
# jammed together.
_INSTRUMENT_PHRASE = {
    "Grant": "a grant",
    "Cooperative Agreement": "a cooperative agreement",
    "Contract": "a contract",
    "Loan": "a loan",
    "Equity/Investment": "an equity investment",
    "Prize/Award": "a prize or award",
    "Fellowship": "a fellowship",
    "Scholarship": "a scholarship",
    "Seed fund": "seed funding",
    "In-kind/TA": "in-kind support or technical assistance",
}


def _canon(value: Any, synonyms: dict[str, str], allowed) -> str | None:
    s = str(value or "").strip()
    if not s:
        return None
    hit = synonyms.get(s.lower())
    if hit:
        return hit
    for a in allowed:                       # already canonical, any casing
        if s.lower() == a.lower():
            return a
    return None


def canon_opportunity(value: Any) -> str | None:
    """The canonical pursuit class, or None when the value says nothing usable."""
    return _canon(value, _OPPORTUNITY_SYNONYMS, OPPORTUNITY_TYPES)


def canon_instrument(value: Any) -> str | None:
    """The canonical award vehicle, or None."""
    return _canon(value, _INSTRUMENT_SYNONYMS, INSTRUMENT_TYPES)


def is_classified(opportunity: str | None) -> bool:
    """False for a value that means "we could not tell" — no pairing rule should judge one."""
    return bool(opportunity) and opportunity not in UNCLASSIFIED


def complement(opportunity: Any, instrument: Any) -> tuple[str | None, str | None, bool, bool]:
    """``(opportunity, instrument, opportunity_inferred, instrument_inferred)``.

    Fills a missing axis from the one present, because they imply each other: 148 live rows
    state Procurement with no instrument, and a procurement is awarded through a contract.
    Never overwrites a stated value, and never infers from an unclassified one.
    """
    opp, inst = canon_opportunity(opportunity), canon_instrument(instrument)
    opp_inferred = inst_inferred = False
    if not inst and is_classified(opp):
        inst = _IMPLIED_INSTRUMENT.get(opp)
        inst_inferred = inst is not None
    if not opp and inst:
        opp = _IMPLIED_OPPORTUNITY.get(inst)
        opp_inferred = opp is not None
    return opp, inst, opp_inferred, inst_inferred


def coherence(opportunity: Any, instrument: Any) -> tuple[str, str]:
    """``(verdict, why)`` — ``consistent`` · ``unclassified`` · ``unknown`` · ``unusual``.

    Only a pair that is genuinely hard to explain is ``unusual``: over 686 live rows that is
    5 (a procurement issuing a grant, a procurement issuing equity). A grant call awarded as
    a contract is ``consistent``, because a grant is contracted after award — warning on
    those 30 would teach a reviewer to ignore the warning that matters on the 5.
    """
    opp, inst = canon_opportunity(opportunity), canon_instrument(instrument)
    if not opp and not inst:
        return "unknown", "neither axis was extracted"
    if not opp or not inst:
        return "unknown", "only one of the two axes was extracted"
    if not is_classified(opp):
        return "unclassified", f"the pursuit class is {opp.lower()}, so there is nothing to check"
    normal = NORMAL_PAIRS.get(opp)
    if normal is None:
        return "unclassified", f"no pairing rule for {opp}"
    if inst in normal:
        return "consistent", f"a {opp.lower()} is normally awarded through {_phrase(inst)}"
    return "unusual", (f"a {opp.lower()} awarded through {_phrase(inst)} is an unusual "
                       f"combination — one of the two is likely misread")


def _phrase(instrument: str | None) -> str:
    if not instrument:
        return "an unstated instrument"
    return _INSTRUMENT_PHRASE.get(instrument, instrument.lower())


def pairing(opportunity: Any, instrument: Any) -> dict[str, Any]:
    """Everything the page needs about the two axes, as ONE reconciled answer.

    ``{"text", "verdict", "note", "opportunity_type", "instrument_type", "inferred"}``
    where ``text`` is the single line to display and ``note`` is set only when there is
    something a reviewer should actually act on.
    """
    opp, inst, opp_inf, inst_inf = complement(opportunity, instrument)
    verdict, why = coherence(opp, inst)
    inferred = [k for k, v in (("opportunity_type", opp_inf), ("instrument_type", inst_inf))
                if v]

    if opp and inst:
        text = f"{opp}, awarded as {_phrase(inst)}"
    elif opp:
        text = opp
    elif inst:
        text = f"Awarded as {_phrase(inst)}"
    else:
        text = ""
    if inferred and text:
        # Say so rather than presenting a derived value as an extracted one.
        text += " (instrument inferred)" if inst_inf and not opp_inf else " (inferred)"

    note = ""
    if verdict == "unusual":
        note = why[0].upper() + why[1:]
    return {"text": text, "verdict": verdict, "note": note, "why": why,
            "opportunity_type": opp, "instrument_type": inst, "inferred": inferred}
