"""Classify an opportunity on TWO orthogonal axes (Bernard's ontology, 2026-06-21):

  * Solicitation type — HOW the call is announced / how you apply
        NOFO, RFA, RFP, CFP, CFA, CfCN, EOI, LOI, RFI, RFQ, Tender, Bid, ITB,
        Procurement notice, Unsolicited, Challenge
  * Instrument type — the donor↔beneficiary CONTRACT if awarded
        Grant, Cooperative Agreement, Contract, Loan, Equity/Investment,
        Prize/Award, Fellowship, Scholarship, Seed fund, In-kind/TA

They're independent (a "NOFO for a Cooperative Agreement", a "CFA for a
Fellowship", a "Tender for a Contract"). The public-site nav category is a third,
DERIVED layer (Funding & Tenders / Jobs / Scholarships / Events). Keep these two
vocabularies as the single source of truth — the dropdowns import them.
"""
from __future__ import annotations

from typing import Any

SOLICITATION_TYPES = [
    "NOFO", "RFA", "RFP", "CFP", "CFA", "CfCN", "EOI", "LOI", "RFI", "RFQ",
    "Tender", "Bid", "ITB", "Procurement notice", "Unsolicited", "Challenge",
    "Other",
]
INSTRUMENT_TYPES = [
    "Grant", "Cooperative Agreement", "Contract", "Loan", "Equity/Investment",
    "Prize/Award", "Fellowship", "Scholarship", "Seed fund", "In-kind/TA",
    "Other",
]

# Longest / most-specific phrases first.
_SOLICITATION_RULES: list[tuple[str, str]] = [
    ("notice of funding opportunity", "NOFO"),
    ("funding opportunity announcement", "NOFO"),
    ("request for applications", "RFA"), ("request for application", "RFA"),
    ("request for proposal", "RFP"),
    ("call for proposal", "CFP"),
    ("call for application", "CFA"),
    ("call for concept", "CfCN"), ("concept note", "CfCN"),
    ("call for expression", "EOI"), ("expression of interest", "EOI"),
    ("letter of inten", "LOI"), ("letter of inquiry", "LOI"),
    ("request for information", "RFI"),
    ("request for quotation", "RFQ"), ("request for quote", "RFQ"),
    ("invitation to bid", "Bid"), ("invitation to tender", "Tender"),
    ("call for tender", "Tender"), ("tender notice", "Tender"), ("tender", "Tender"),
    ("procurement notice", "Procurement notice"),
    ("challenge", "Challenge"),
    ("rolling basis", "Unsolicited"), ("unsolicited", "Unsolicited"),
    ("open call", "CFP"), ("solicitation", "RFP"),
    (" nofo", "NOFO"), (" foa", "NOFO"), (" rfa", "RFA"), (" rfp", "RFP"),
    (" cfp", "CFP"), (" cfa", "CFA"), (" cfcn", "CfCN"), (" eoi", "EOI"),
    (" loi", "LOI"), (" rfi", "RFI"), (" rfq", "RFQ"), (" itb", "ITB"),
    ("call", "CFP"),
]
_INSTRUMENT_RULES: list[tuple[str, str]] = [
    ("cooperative agreement", "Cooperative Agreement"),
    ("seed fund", "Seed fund"), ("seed grant", "Seed fund"),
    ("fellowship", "Fellowship"), ("scholarship", "Scholarship"),
    ("equity", "Equity/Investment"), ("investment", "Equity/Investment"),
    ("venture", "Equity/Investment"),
    ("concessional", "Loan"), ("loan", "Loan"),
    ("in-kind", "In-kind/TA"), ("technical assistance", "In-kind/TA"),
    ("grant", "Grant"),                          # before prize/award/contract
    ("prize", "Prize/Award"), ("award", "Prize/Award"),
    ("procurement contract", "Contract"), ("procurement", "Contract"),
    ("tender", "Contract"), ("contract", "Contract"),
    ("fund", "Grant"),
]


def _match(blob: str, rules: list[tuple[str, str]]) -> str | None:
    # Leading space so the " acronym" rules also match a start-of-string acronym
    # (e.g. "NOFO: …", "CFA for …").
    blob = " " + (blob or "").lower()
    for kw, val in rules:
        if kw in blob:
            return val
    return None


def detect_solicitation(candidate: dict[str, Any]) -> str | None:
    """How the call is announced / applied to (NOFO/RFP/CFA/EOI/Tender/…).
    Reads title + description + link — the solicitation type often appears only in
    the body (a generically-titled call whose text says "request for proposals")."""
    blob = (f"{candidate.get('opportunity_title') or ''} "
            f"{candidate.get('brief_description') or ''} "
            f"{candidate.get('opportunity_link') or ''}")
    return _match(blob, _SOLICITATION_RULES)


def detect_instrument(candidate: dict[str, Any]) -> str | None:
    """The contract if awarded (Grant/Cooperative Agreement/Contract/Loan/…).
    The donor's funding-instrument field (grants.gov, carried on funding_window /
    _funding_instrument) is weighed first."""
    fi = (candidate.get("_funding_instrument") or candidate.get("funding_window")
          or candidate.get("funding_type") or "")
    blob = (f"{fi} {candidate.get('opportunity_title') or ''} "
            f"{candidate.get('opportunity_link') or ''}")
    return _match(blob, _INSTRUMENT_RULES)
