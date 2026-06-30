"""E3c — data quality / completeness → prediction confidence.

The auto Proceed/Park/Decline is only as good as the data behind it: a thin donor mapping
or a sparsely-extracted call makes the prediction shakier. These metrics turn that into a
visible CONFIDENCE band so a reviewer weights a "Proceed" built on 30%-complete data
differently from one on 90%.

  donor_completeness(donor) → fraction of the donor's eligibility-driving requirement
                              fields that are ANSWERED (Required / Not Required / Not Sure;
                              blank = genuinely missing — the only thing counted incomplete)
  call_completeness(rfp)    → fraction of the key extracted call fields that are present
  confidence_band(d, c)     → ("High" | "Medium" | "Low", blended %) from the two
"""
from __future__ import annotations

from typing import Any

# An explicit requirement response (matches the donor form's tri-state); blank = missing.
_ANSWERED = {"yes", "no", "not_sure"}

# Key extracted call fields the prediction leans on — presence = the extractor got it.
_CALL_KEY_FIELDS = (
    "call_submission_deadline", "call_award_value", "call_geographic_scope",
    "call_domain_areas", "brief_description", "instrument_type", "funding_agency",
)


def _nonblank(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, tuple, dict)):
        return len(v) > 0
    return str(v).strip() != ""


def donor_completeness(donor: dict | None) -> tuple[int, int, int]:
    """(pct, answered, total) over the donor's REQUIREMENT fields (the '*_required' gates
    that drive MUST/PREFER). Answered = an explicit Required/Not Required/Not Sure; blank =
    genuinely missing. No donor / no fields → (0, 0, 0)."""
    if not donor:
        return 0, 0, 0
    fields = [k for k in donor if isinstance(k, str) and k.endswith("_required")]
    if not fields:
        return 0, 0, 0
    answered = sum(1 for f in fields
                   if str(donor.get(f) or "").strip().lower() in _ANSWERED)
    return round(100 * answered / len(fields)), answered, len(fields)


def call_completeness(rfp: dict | None) -> tuple[int, int, int]:
    """(pct, present, total) over the key extracted call fields."""
    if not rfp:
        return 0, 0, 0
    present = sum(1 for f in _CALL_KEY_FIELDS if _nonblank(rfp.get(f)))
    return round(100 * present / len(_CALL_KEY_FIELDS)), present, len(_CALL_KEY_FIELDS)


def confidence_band(donor_pct: int, call_pct: int) -> tuple[str, int]:
    """Blend donor + call completeness into a confidence band. The call is weighted higher
    (the prediction reads the call directly; the donor mapping is corroboration). Returns
    (band, blended_pct): High >= 75 · Medium >= 45 · Low otherwise."""
    blended = round(0.6 * call_pct + 0.4 * donor_pct)
    band = "High" if blended >= 75 else ("Medium" if blended >= 45 else "Low")
    return band, blended
