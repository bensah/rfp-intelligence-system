"""A funder's "ACRONYM <dash> Name" prefix must split on ANY dash variant.

Funder strings reach us from a submit form, an Excel migration and a crawler, so the
separator is an ASCII hyphen in some rows and an EN DASH (U+2013) or EM DASH (U+2014) in
others. Splitting only on " - " silently returned NO donor record for the en/em-dash rows.
That is not a cosmetic miss: with no donor record the confidence band loses its donor side
entirely AND every donor-imposed MUST-1 / MUST-5 component stays inactive, so MUST-1 reads
"Not sure" for a funder the org has a fully-researched profile for.
"""
from __future__ import annotations

import unittest

from core import data_quality as dq
from core.criteria_derive import _funder_in_history
from core.donor_intel import split_funder_prefix

# Every dash we accept, with the plain ASCII hyphen first.
DASHES = ["-", "‐", "‑", "‒", "–", "—", "―", "−"]


class TestSplitFunderPrefix(unittest.TestCase):
    def test_every_dash_variant_splits_identically(self):
        for d in DASHES:
            with self.subTest(dash=repr(d)):
                self.assertEqual(
                    split_funder_prefix(f"BMGF {d} Gates Foundation"),
                    ("BMGF", "Gates Foundation"))

    def test_no_prefix_returns_none(self):
        for funder in ("Gates Foundation", "Unitaid", "", None):
            with self.subTest(funder=funder):
                self.assertIsNone(split_funder_prefix(funder))

    def test_a_hyphenated_name_is_not_a_prefix(self):
        # The separator needs spaces on BOTH sides, so a hyphenated donor name survives.
        self.assertIsNone(split_funder_prefix("Wellcome-Trust"))

    def test_splits_on_the_first_separator_only(self):
        self.assertEqual(
            split_funder_prefix("WB – World Bank – IDA"),
            ("WB", "World Bank – IDA"))

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(split_funder_prefix("  FCDO — UK Aid  "),
                         ("FCDO", "UK Aid"))


class TestFunderHistoryDashVariants(unittest.TestCase):
    """PREFER-7 reads the same "ACRONYM - Name" shape out of org_funder_history."""

    HIST = ["Gates Foundation", "Wellcome Trust"]

    def test_every_dash_variant_finds_the_funder_in_history(self):
        for d in DASHES:
            with self.subTest(dash=repr(d)):
                self.assertTrue(_funder_in_history(f"BMGF {d} Gates Foundation", self.HIST))

    def test_an_unrelated_funder_still_does_not_match(self):
        self.assertFalse(_funder_in_history("Sida – Swedish Development Agency",
                                            self.HIST))

    def test_empty_history_never_matches(self):
        self.assertFalse(_funder_in_history("BMGF – Gates Foundation", []))


class TestDonorMatchedIsDistinctFromZeroPercent(unittest.TestCase):
    """"donor 0%" used to mean either "no profile" or "profile, nothing researched"."""

    def test_no_donor_record_is_not_matched(self):
        self.assertFalse(dq.donor_matched(None))
        self.assertFalse(dq.donor_matched({}))

    def test_a_record_with_nothing_answered_is_still_matched(self):
        donor = {"donor": "Some Funder", "donor_tax_exempt_status_required": None}
        self.assertTrue(dq.donor_matched(donor))
        self.assertEqual(dq.donor_completeness(donor)[0], 0)   # 0% — but it EXISTS

    def test_completeness_counts_answered_requirement_fields(self):
        donor = {"donor": "F", "donor_a_required": "yes", "donor_b_required": "no",
                 "donor_c_required": "not_sure", "donor_d_required": None}
        self.assertEqual(dq.donor_completeness(donor), (75, 3, 4))


if __name__ == "__main__":
    unittest.main()
