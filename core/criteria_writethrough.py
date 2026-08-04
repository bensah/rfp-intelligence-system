"""Write-through for criterion components that are backed by an ORG-PROFILE donor list.

Some MUST/PREFER components aren't properties of the RFP at all — they answer "is THIS
call's donor in one of our org-profile donor lists?" (e.g. MUST-5 "Authorized signatory
(this donor)" reads `org_authorized_signatory_donors`). They are DERIVED on every render,
so editing them on the Review screen and saving the RFP changed nothing: the next render
re-derived from the same profile list and the component snapped back.

This module decides what the profile list SHOULD become, given the reviewer's component
verdict. It is deliberately pure (no Streamlit, no DB) so it can be unit-tested; the caller
supplies the current list, the donor-matching function, and performs the actual save.

Contract: score 1.0 → this call's funder must be IN the list; 0.0 → it must be OUT.
0.5 (partial) is intentionally a NO-OP — "partially obtained" isn't representable in a
membership list, so we never guess.
"""
from __future__ import annotations

from typing import Any, Callable

# (criterion key, component key) -> org-profile field the component reads from.
WRITE_THROUGH: dict[tuple[str, str], str] = {
    ("cofinancing", "authorized_signatory"): "org_authorized_signatory_donors",
}


def plan_writethrough(
    component_scores: dict[str, dict[str, float]],
    profile: dict[str, Any],
    donor: dict | None,
    rfp: dict,
    match_fn: Callable[[Any, dict | None, dict], bool],
) -> tuple[dict[str, list[str]], list[str]]:
    """Work out the profile-list changes implied by the reviewer's component scores.

    Returns ``(changes, notes)`` where `changes` maps org-profile field -> the new list and
    `notes` is human-readable text for the UI. Both are empty when nothing should change.

    `match_fn(names, donor, rfp) -> bool` is the app's canonical donor matcher, injected so
    this module stays free of heavy imports (and so tests can supply a simple stub).
    """
    funder = str(rfp.get("funding_agency") or "").strip()
    changes: dict[str, list[str]] = {}
    notes: list[str] = []
    if not funder or not component_scores:
        return changes, notes

    for (crit, comp), field in WRITE_THROUGH.items():
        score = (component_scores.get(crit) or {}).get(comp)
        if score is None:
            continue
        current = [str(x).strip() for x in (profile.get(field) or []) if str(x).strip()]
        listed = match_fn(current, donor, rfp)
        if score >= 1.0 and not listed:
            changes[field] = current + [funder]
            notes.append(f"added **{funder}** to your org profile")
        elif score <= 0.0 and listed:
            kept = [x for x in current if not match_fn([x], donor, rfp)]
            if len(kept) != len(current):
                changes[field] = kept
                notes.append(f"removed **{funder}** from your org profile")
        # 0.5 → deliberate no-op (see module docstring).
    return changes, notes
