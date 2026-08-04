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
# WHAT PURSUING THIS ACTUALLY IS — the coarse class the eligibility gate opts out of.
# Distinct from SOLICITATION (how it's announced) and INSTRUMENT (the resulting contract):
# a "Tender/ITB" solicitation for a "Contract" instrument is a Procurement, which a
# grant-seeking org never wants. Also the vocabulary behind the Opportunity type dropdown.
OPPORTUNITY_TYPES = [
    "Grant/funding call", "Procurement", "Consultancy", "Training", "Loan",
    "Prize/Challenge", "Announcement", "Other",
]
# Classes the gate rejects by default for a grant-seeking org (each honours its own
# policies['exclusions'] flag — see core/auto_scorer.is_eligible).
OPPORTUNITY_TYPE_EXCLUSIONS = {
    "Procurement": "reject_procurement",
    "Consultancy": "reject_consultancies",
    "Training": "reject_training_only",
    "Loan": "reject_loans",
}

# Hosts that ONLY ever carry procurement/tender notices — the strongest possible signal,
# and language-independent (the UNGM notice that leaked was in Spanish, so no English label
# matched). Keyed on opportunity_link.
_PROCUREMENT_HOSTS = (
    "ungm.org", "tenders.un.org", "procurement-notices.undp.org",
    "wbgeprocure", "procnotices", "ted.europa.eu", "devbusiness.un.org",
)
# Tender/consultancy labels INCLUDING the non-English forms our sources actually publish.
_PROCUREMENT_LABELS = (
    "invitation to bid", "invitation to tender", "request for quotation",
    "request for tender", "procurement notice", "prior information notice",
    "adquisición", "adquisicion", "licitación", "licitacion", "convocatoria a licitación",
    "appel d'offres", "avis d'appel d'offres", "appel à concurrence",
)
# GRANT portals that carry funding calls even though their name/URL contains the word
# "tender". The EU "Funding & Tenders" portal is the big one: its URL path
# (ec.europa.eu/info/funding-tenders/...) makes detect_solicitation label a Horizon/EDCTP3
# grant call "Tender"/"Contract", which would then be thrown out as procurement. A dry-run
# over live data caught exactly that — legitimate EDCTP3 calls (€88.9M) misfiled. On these
# hosts a tender-shaped solicitation is NOT evidence of procurement.
_GRANT_PORTAL_HOSTS = (
    "ec.europa.eu/info/funding-tenders", "funding-tenders.ec.europa.eu",
    "grants.gov", "ukri.org", "nih.gov", "nihr.ac.uk", "wellcome.org",
)
_CONSULTANCY_LABELS = (
    "expression of interest for consult", "request for expressions of interest",
    "consulting services", "consultancy services", "individual consultant",
    "terms of reference", "services de consultant", "consultoría", "consultoria",
)

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


# ── SYNONYMS (owner 2026-06-29) — single source of the phrasings that signal each
# canonical type, shared by the regex detector AND the LLM hint. ADDITIVE: phrases
# not already in the hand-tuned rules above are appended at LOWER priority, so this
# only widens coverage — it never reorders/removes a tuned match.
SOLICITATION_SYNONYMS: dict[str, list[str]] = {
    "NOFO": ["notice of funding opportunity", "funding opportunity announcement", "nofo", "foa"],
    "RFA": ["request for applications", "rfa"],
    "RFP": ["request for proposals", "rfp", "solicitation"],
    "CFP": ["call for proposals", "call for projects", "open call", "cfp"],
    "CFA": ["call for applications", "cfa"],
    "CfCN": ["call for concept notes", "concept note", "cfcn"],
    "EOI": ["call for expressions of interest", "expression of interest", "eoi"],
    "LOI": ["letter of intent", "letter of inquiry", "loi"],
    "RFI": ["request for information", "rfi"],
    "RFQ": ["request for quotation", "request for quote", "rfq"],
    "Tender": ["invitation to tender", "call for tenders", "tender notice", "tender"],
    "Bid": ["invitation to bid", "bid"],
    "ITB": ["itb"],
    "Procurement notice": ["procurement notice", "contract notice"],
    "Unsolicited": ["accepted on a rolling basis", "rolling basis", "always open", "unsolicited"],
    "Challenge": ["grand challenge", "challenge"],
}
INSTRUMENT_SYNONYMS: dict[str, list[str]] = {
    "Cooperative Agreement": ["cooperative agreement", "co-operative agreement"],
    "Seed fund": ["seed fund", "seed grant", "seed funding"],
    "Fellowship": ["fellowship"],
    "Scholarship": ["scholarship", "bursary"],
    "Equity/Investment": ["equity", "investment", "venture capital", "venture"],
    "Loan": ["concessional loan", "concessional", "development finance", "loan"],
    "In-kind/TA": ["in-kind", "technical assistance"],
    "Prize/Award": ["prize", "award"],
    "Contract": ["procurement contract", "procurement", "tender", "contract"],
    "Grant": ["grant", "fund"],
}

# Cross-type SYNONYM GROUPS — types interchangeable for SEARCH / SCAN expansion
# (owner: "CFP and RFP are synonyms"). expand_solicitation(["CFP"]) → the whole
# competitive-call family, so a source tagged one also catches the others.
SOLICITATION_GROUPS: list[frozenset[str]] = [
    frozenset({"NOFO", "RFA", "RFP", "CFP", "CFA"}),            # competitive funding calls
    frozenset({"EOI", "LOI", "CfCN"}),                         # preliminary interest / concept
    frozenset({"Tender", "Bid", "ITB", "Procurement notice", "RFQ"}),  # procurement
]
INSTRUMENT_GROUPS: list[frozenset[str]] = [
    frozenset({"Grant", "Cooperative Agreement", "Seed fund"}),
    frozenset({"Loan", "Equity/Investment"}),
    frozenset({"Fellowship", "Scholarship"}),
]


def _enrich(rules: list[tuple[str, str]], syn: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Append synonym phrases not already covered (lower priority → additive)."""
    have = {kw.strip() for kw, _ in rules}
    for t, phrases in syn.items():
        for p in phrases:
            pl = p.lower().strip()
            key = (" " + pl) if (pl.isalpha() and len(pl) <= 5) else pl   # acronym → word-anchored
            if pl not in have and key.strip() not in have:
                rules.append((key, t))
                have.add(pl)
    return rules


_SOLICITATION_RULES = _enrich(_SOLICITATION_RULES, SOLICITATION_SYNONYMS)
_INSTRUMENT_RULES = _enrich(_INSTRUMENT_RULES, INSTRUMENT_SYNONYMS)


def _expand(types, groups) -> list[str]:
    """Expand each canonical type to its synonym GROUP (interchangeable types),
    preserving the originals. CFP → {CFP, RFP, RFA, CFA, NOFO}. Unknown types pass
    through unchanged."""
    out: set[str] = set()
    for t in (types or []):
        out.add(t)
        for g in groups:
            if t in g:
                out |= set(g)
    return sorted(out)


def expand_solicitation(types) -> list[str]:
    return _expand(types, SOLICITATION_GROUPS)


def expand_instrument(types) -> list[str]:
    return _expand(types, INSTRUMENT_GROUPS)


def synonym_hint(axis: str = "solicitation") -> str:
    """Compact 'CANONICAL = syn; syn; …' string to drop into an LLM prompt so it
    normalises any wording/acronym to a canonical type."""
    syn = SOLICITATION_SYNONYMS if axis == "solicitation" else INSTRUMENT_SYNONYMS
    return " · ".join(f"{t} = {', '.join(ps)}" for t, ps in syn.items())


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


def detect_opportunity_type(candidate: dict[str, Any]) -> str | None:
    """What pursuing this opportunity actually IS (Grant/funding call · Procurement ·
    Consultancy · Training · Loan · Prize/Challenge · Announcement).

    Nothing populated `opportunity_type` before this (only the EU F&T scanner did), so the
    eligibility gate's type opt-out never fired and buying-goods tenders / consultancy EOIs
    reached grant pipelines. Signals, strongest first:

      1. HOST — ungm.org & friends only ever carry procurement notices. Language-independent,
         which matters: the tender that leaked was in Spanish, so no English label matched.
      2. TITLE/BODY LABELS — including Spanish/French forms our sources actually publish.
      3. The already-detected SOLICITATION family (Tender/Bid/ITB/RFQ/Procurement notice)
         and INSTRUMENT (Loan, Prize/Award) — reuses type_detect's own vocabulary rather
         than inventing a second one.

    Returns None when nothing is recognisable, so callers can decide how to fail."""
    blob = " ".join([
        str(candidate.get("opportunity_title") or ""),
        str(candidate.get("brief_description") or ""),
    ]).lower()
    link = str(candidate.get("opportunity_link") or "").lower()

    if any(h in link for h in _PROCUREMENT_HOSTS):
        return "Procurement"
    if any(lbl in blob for lbl in _PROCUREMENT_LABELS):
        return "Procurement"
    if any(lbl in blob for lbl in _CONSULTANCY_LABELS):
        return "Consultancy"

    sol = candidate.get("solicitation_type") or detect_solicitation(candidate)
    inst = candidate.get("instrument_type") or detect_instrument(candidate)
    on_grant_portal = any(h in link for h in _GRANT_PORTAL_HOSTS)
    if sol in SOLICITATION_GROUPS[2] and not on_grant_portal:
        return "Procurement"                     # Tender/Bid/ITB/Procurement notice/RFQ
    if on_grant_portal:
        return "Grant/funding call"              # a funding portal's calls ARE funding calls
    if inst == "Loan":
        return "Loan"
    if inst == "Prize/Award" or sol == "Challenge":
        return "Prize/Challenge"
    if inst in ("Grant", "Cooperative Agreement", "Seed fund") or sol in SOLICITATION_GROUPS[0]:
        return "Grant/funding call"
    if sol in SOLICITATION_GROUPS[1]:            # EOI/LOI/CfCN — a funding call unless the
        return "Grant/funding call"              # consultancy labels above already caught it
    return None
