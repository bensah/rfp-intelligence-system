"""Geographic fit as TWO questions, because a call asks two.

MUST-4 has always answered one: does the work happen where we work? So a call whose money
goes to the Global South scored as a fit for an organisation operating in the Global South —
correct, and beside the point when the call also says *applicants must be headquartered in
the Occitania region of France*. That call reached a review week with an 81/100 bid
strength, and the disqualifying sentence was sitting unused in its own brief description
and in the LLM's key-risks. The two facts are independent:

  A. WHERE THE APPLICANT MUST BE BASED — an eligibility rule about the organisation. Only
     the call can state it, and most calls do not. When it is absent there is nothing to
     score, so the component is DROPPED from the denominator rather than guessed at or
     scored zero; inventing a restriction no funder wrote is how you decline work you
     could have won.
  B. WHERE THE WORK HAPPENS — the intervention geography, matched against where we
     operate. Always scored: when neither the call nor the donor states one, the honest
     reading is that the funder does not restrict it, so it counts as global and passes.

The score is the components that APPLY: 2/2 when the call states an applicant rule, 1/1
when it does not. Reporting 1/2 for a call that never had a component A would penalise
every ordinary call for a restriction it did not impose.

Within each component the existing tier convention holds — our own footprint scores 1.0, a
qualifying partner 0.5 — so "we can reach it through a partner" stays visible rather than
collapsing to pass/fail.

WHAT THIS MODULE WILL NOT DO. It never treats an unconfigured organisation profile as an
absence of reach: an org that has recorded no countries is unknown, not disqualified, and
scoring it zero once auto-Declined every scoped call for a tenant who had simply not
finished onboarding. Unknown stays unknown, and the caller Parks it for a human.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

# An applicant-base rule the call itself states. `eligibility_countries` is the call's own
# "who may apply" list — until now used only as a last-resort stand-in for the work
# geography, which is precisely the confusion this module ends.
_APPLICANT_FIELDS = ("call_applicant_base_scope", "eligibility_countries")
# Where the work happens: the call governs, the donor is a fallback, and donor intel may
# never WIDEN an explicit call restriction (a broad donor scope must not turn an
# India-only call into a pass).
_WORK_CALL_FIELDS = ("call_geographic_scope",)
_WORK_DONOR_FIELDS = ("donor_geographic_scope", "donor_funded_geographies")

OWN = 1.0
VIA_PARTNER = 0.5
NO_MATCH = 0.0


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("|", ";").split(";")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    return []


def _first_stated(source: dict | None, fields: Iterable[str]) -> list[str]:
    for field in fields:
        values = _as_list((source or {}).get(field))
        if values:
            return values
    return []


def _places(values: Iterable[str]) -> list[str]:
    """Drop the values that are labels rather than places ("Regional", "Various"), so a
    scope consisting only of those reads as UNSTATED instead of as somewhere we are not.

    Delegates to the scorer's own list rather than keeping a second opinion about what
    counts as a place — `canonical_geo` accepts "Regional" happily, which is exactly the
    trap `_drop_non_geographies` exists to close."""
    try:
        from core.criteria_derive import _drop_non_geographies
        return list(_drop_non_geographies(list(values)))
    except Exception:
        return [str(v).strip() for v in values if str(v or "").strip()]


def _covers(ours: list[str], theirs: list[str]) -> bool:
    try:
        from core.criteria_derive import _covers_scope
        return bool(_covers_scope(ours, theirs))
    except Exception:
        lower = {str(o).strip().lower() for o in ours}
        return any(str(t).strip().lower() in lower for t in theirs)


def _overlap(ours: list[str], theirs: list[str]) -> bool:
    """A REAL country overlap, as opposed to a match on an open-to-anyone tier. The
    distinction matters only for the explanation — claiming "based in scope" for a call
    that is simply open to everyone reads as a falsehood to anyone checking."""
    try:
        from core.criteria_derive import _country_overlap
        return bool(_country_overlap(ours, theirs))
    except Exception:
        return bool({str(o).strip().lower() for o in ours}
                    & {str(t).strip().lower() for t in theirs})


def _partner_countries(org: dict) -> list[str]:
    """Countries where a QUALIFYING partner is established. Partner HQ country is not
    captured as its own field yet (only donors have one), so the partner's recorded
    country stands in — it is the same fact for every partner on file today."""
    out: list[str] = []
    for partner in (org.get("partners") or org.get("trusted_partners") or []):
        if isinstance(partner, dict):
            country = partner.get("hq_country") or partner.get("country")
            if country:
                out.append(str(country))
        elif isinstance(partner, str) and partner.strip():
            out.append(partner.strip())
    return out


def _known_geography(org: dict, org_settings: dict | None) -> bool:
    return bool(_as_list(org.get("org_registered_countries"))
                or _as_list(org.get("org_operating_countries"))
                or _partner_countries(org)
                or str((org_settings or {}).get("org_is_us_entity", "")).lower() == "true")


def _match(ours: list[str], partners: list[str], required: list[str]) -> tuple[float, str]:
    """Tiered match of our footprint against what the call requires."""
    if ours and _covers(ours, required):
        return (OWN, "ours is in scope" if _overlap(ours, required)
                else "the call is open to any country")
    if partners and _covers(partners, required):
        return VIA_PARTNER, "a partner is in scope"
    return NO_MATCH, "neither we nor a partner is in scope"


def applicant_component(org: dict, rfp: dict,
                        org_settings: dict | None = None) -> dict:
    """A. Where must the APPLICANT be based? Active only when the call says so."""
    required = _places(_first_stated(rfp, _APPLICANT_FIELDS))
    if not required:
        return {"key": "applicant_base", "active": False, "score": None,
                "required": [], "why": "the call does not say where an applicant must be "
                                       "based — nothing to score"}
    if not _known_geography(org, org_settings):
        return {"key": "applicant_base", "active": False, "score": None,
                "required": required,
                "why": "our own registered countries are not recorded"}
    registered = _as_list(org.get("org_registered_countries"))
    score, why = _match(registered, _partner_countries(org), required)
    return {"key": "applicant_base", "active": True, "score": score,
            "required": required,
            "why": f"applicants must be based in {', '.join(required[:4])} — {why}"}


def operations_component(org: dict, rfp: dict, donor: dict | None = None,
                         org_settings: dict | None = None) -> dict:
    """B. Where does the WORK happen? Always active; unstated reads as global."""
    required = _places(_first_stated(rfp, _WORK_CALL_FIELDS))
    source = "call"
    if not required:
        required = _places(_first_stated(donor, _WORK_DONOR_FIELDS))
        source = "donor"
    if not required:
        # Neither says. A funder that does not restrict the geography has not excluded us,
        # and treating silence as a restriction was how open calls got Parked as "Not sure".
        return {"key": "operations", "active": True, "score": OWN, "required": [],
                "source": "unstated",
                "why": "neither the call nor the funder restricts where the work happens "
                       "— treated as global"}
    if not _known_geography(org, org_settings):
        return {"key": "operations", "active": False, "score": None, "required": required,
                "source": source,
                "why": "our own operating countries are not recorded"}
    operating = _as_list(org.get("org_operating_countries"))
    registered = _as_list(org.get("org_registered_countries"))
    score, why = _match(operating or registered, _partner_countries(org), required)
    return {"key": "operations", "active": True, "score": score, "required": required,
            "source": source,
            "why": f"work in {', '.join(required[:4])} — {why}"}


def evaluate(org: dict, rfp: dict, donor: dict | None = None,
             org_settings: dict | None = None) -> dict:
    """Both components, and the MUST-4 result they add up to.

    `{"score", "denom", "components", "label", "why"}` — denom is how many components the
    call actually poses (2 when it states an applicant rule, 1 when it does not, 0 when
    our own geography is unknown and nothing can be measured)."""
    org = org or {}
    rfp = rfp or {}
    components = [applicant_component(org, rfp, org_settings),
                  operations_component(org, rfp, donor, org_settings)]
    active = [c for c in components if c["active"]]
    score = sum(float(c["score"] or 0.0) for c in active)
    denom = len(active)
    return {"score": score, "denom": denom, "components": components,
            "label": label_for(score, denom, components),
            "why": "; ".join(c["why"] for c in components if c["active"])
                   or "; ".join(c["why"] for c in components)}


def label_for(score: float, denom: int, components: list[dict]) -> str:
    """The words shown on the criteria card. A failed APPLICANT rule is named explicitly:
    "no presence there" would describe the wrong thing entirely — we may work exactly where
    the money goes and still be barred from applying."""
    if not denom:
        return "Not sure"
    applicant = next((c for c in components if c["key"] == "applicant_base"), None)
    if applicant and applicant["active"] and applicant["score"] == NO_MATCH:
        return "Not eligible to apply from here"
    if score >= denom:
        return "Yes, our own presence"
    if score <= 0:
        return "No presence there"
    return "Yes, via a partner"


def bid_strength(org: dict, rfp: dict, org_settings: dict | None = None,
                 donor: dict | None = None) -> tuple[float, int]:
    """`(score, denom)` in the shape the scorer already consumes."""
    result = evaluate(org, rfp, donor, org_settings)
    return result["score"], result["denom"]
