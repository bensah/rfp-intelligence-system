"""Detect an opportunity's TYPE (Grant / RFP / CFP / EOI / Tender / Cooperative
Agreement / Fellowship / …) from whatever signal a source gives.

Donors label type differently: grants.gov exposes a "Funding Instrument Type"
(Grant, Cooperative Agreement, …); most others only imply it in the title/URL.
`detect()` normalises all of that to one canonical type used across the scanner
(rfp_submissions.opportunity_type, scan_decisions.opportunity_type, and the
source_registry.opportunity_types aggregate). Keep the vocabulary aligned with
verification._TYPE_OPTS.
"""
from __future__ import annotations

from typing import Any

# Longest / most-specific phrases first so e.g. "cooperative agreement" wins over
# the bare "agreement", and "request for proposal" over a stray "proposal".
_RULES: list[tuple[str, str]] = [
    ("cooperative agreement", "Cooperative Agreement"),
    ("expression of interest", "EOI"),
    ("request for information", "RFI"),
    ("request for proposal", "RFP"),
    ("request for applications", "Grant"),
    ("call for proposal", "CFP"),
    ("call for application", "Grant"),
    ("call for expression", "EOI"),
    ("letter of inten", "LOI"),
    ("procurement notice", "Procurement notice"),
    ("contract award", "Contract award"),
    ("invitation to bid", "Tender"),
    ("invitation to tender", "Tender"),
    ("seed fund", "Seed Fund"),
    ("seed grant", "Seed Fund"),
    ("fellowship", "Fellowship"),
    ("scholarship", "Scholarship"),
    ("internship", "Internship"),
    ("consultan", "Consultancy"),
    ("tender", "Tender"),
    ("solicitation", "Tender"),
    ("procurement", "Procurement notice"),
    ("prize", "Award"),
    ("award", "Award"),
    (" rfp", "RFP"),
    (" cfp", "CFP"),
    (" eoi", "EOI"),
    (" rfi", "RFI"),
    (" loi", "LOI"),
    ("cooperative", "Cooperative Agreement"),
    ("grant", "Grant"),
    ("fund", "Grant"),
    ("call", "CFP"),
]


def detect(candidate: dict[str, Any]) -> str | None:
    """Return a canonical opportunity type, or None if nothing matched.

    Precedence: the donor's explicit funding-instrument field (e.g. grants.gov's,
    carried on `_funding_instrument` / `funding_window`) is weighed first by being
    placed at the front of the searched text, then the title, then the URL."""
    fi = (candidate.get("_funding_instrument") or candidate.get("funding_window")
          or candidate.get("funding_type") or "")
    blob = (f"{fi} {candidate.get('opportunity_title') or ''} "
            f"{candidate.get('opportunity_link') or ''}").lower()
    for kw, typ in _RULES:
        if kw in blob:
            return typ
    return None
