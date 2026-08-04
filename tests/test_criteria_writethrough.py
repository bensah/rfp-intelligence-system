"""Regression tests for donor-list component write-through (core.criteria_writethrough).

MUST-5's "Authorized signatory (this donor)" is DERIVED from the org profile's
`org_authorized_signatory_donors` list — it is not a property of the RFP. Editing it on the
Review screen and saving used to change nothing: the next render re-derived the component
from the same list and it snapped back to ✗. Write-through pushes the reviewer's verdict
into the profile field the component actually reads.

Contract: 1.0 → the call's funder must be IN the list · 0.0 → OUT · 0.5 → no-op.

Run:  python -m unittest tests.test_criteria_writethrough
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.criteria_writethrough import WRITE_THROUGH, plan_writethrough    # noqa: E402

_FIELD = "org_authorized_signatory_donors"
_RFP = {"funding_agency": "GiveWell"}
_SIG = {"cofinancing": {"authorized_signatory": 1.0}}


def _match(names, _donor, rfp):
    """Stub of the app's canonical matcher: case-insensitive exact name match."""
    want = str(rfp.get("funding_agency") or "").strip().lower()
    return any(str(n).strip().lower() == want for n in (names or []))


class WriteThroughTests(unittest.TestCase):
    def test_score_1_adds_the_funder(self):
        prof = {_FIELD: ["Wellcome Trust"]}
        changes, notes = plan_writethrough(_SIG, prof, {}, _RFP, _match)
        self.assertEqual(changes[_FIELD], ["Wellcome Trust", "GiveWell"])
        self.assertTrue(notes and "GiveWell" in notes[0])

    def test_score_1_when_already_listed_is_a_noop(self):
        prof = {_FIELD: ["GiveWell"]}
        changes, notes = plan_writethrough(_SIG, prof, {}, _RFP, _match)
        self.assertEqual(changes, {})
        self.assertEqual(notes, [])

    def test_score_0_removes_the_funder(self):
        prof = {_FIELD: ["Wellcome Trust", "GiveWell"]}
        changes, notes = plan_writethrough(
            {"cofinancing": {"authorized_signatory": 0.0}}, prof, {}, _RFP, _match)
        self.assertEqual(changes[_FIELD], ["Wellcome Trust"])
        self.assertTrue(notes and "removed" in notes[0])

    def test_score_0_when_absent_is_a_noop(self):
        prof = {_FIELD: ["Wellcome Trust"]}
        changes, notes = plan_writethrough(
            {"cofinancing": {"authorized_signatory": 0.0}}, prof, {}, _RFP, _match)
        self.assertEqual(changes, {})

    def test_partial_score_never_guesses(self):
        # 0.5 isn't representable in a membership list — must not add or remove.
        prof = {_FIELD: ["Wellcome Trust"]}
        changes, notes = plan_writethrough(
            {"cofinancing": {"authorized_signatory": 0.5}}, prof, {}, _RFP, _match)
        self.assertEqual(changes, {})
        self.assertEqual(notes, [])

    def test_empty_profile_list_is_created(self):
        changes, _ = plan_writethrough(_SIG, {}, {}, _RFP, _match)
        self.assertEqual(changes[_FIELD], ["GiveWell"])

    def test_blank_funder_is_ignored(self):
        changes, notes = plan_writethrough(_SIG, {_FIELD: []}, {}, {"funding_agency": ""}, _match)
        self.assertEqual(changes, {})
        self.assertEqual(notes, [])

    def test_unmapped_component_is_ignored(self):
        changes, _ = plan_writethrough(
            {"cofinancing": {"audited_financials": 1.0}}, {_FIELD: []}, {}, _RFP, _match)
        self.assertEqual(changes, {})

    def test_no_component_scores_is_a_noop(self):
        self.assertEqual(plan_writethrough({}, {_FIELD: []}, {}, _RFP, _match), ({}, []))

    def test_blank_entries_are_cleaned_not_duplicated(self):
        prof = {_FIELD: ["  ", "Wellcome Trust", ""]}
        changes, _ = plan_writethrough(_SIG, prof, {}, _RFP, _match)
        self.assertEqual(changes[_FIELD], ["Wellcome Trust", "GiveWell"])

    def test_map_targets_the_real_profile_field(self):
        self.assertEqual(WRITE_THROUGH[("cofinancing", "authorized_signatory")], _FIELD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
