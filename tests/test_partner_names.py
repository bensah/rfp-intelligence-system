"""An organisation's own name can contain the separator we split partners on.

`lead_applicant` / `sub_applicant` cells name jointly-applying organisations separated by ";" or
",", so counting partners means splitting on those. But one name can contain one, and the split
then invents a partner:

    "Northline Statistics Group Inc.; (NSG)"  ->  two bars, one of them a bare "(NSG)"

The same organisation written without a separator, "Northline Statistics Group (NSG)", was never
affected — which is what identifies the separator, not the parentheses, as the cause.

The fix re-attaches rather than relaxing the separator rule, and only re-attaches a piece it can
recognise as part of the name before it. So the failure direction matters and is asserted here:
an unrecognised piece stays a SEPARATE organisation (over-counted, visible on the chart, fixable
in the data) rather than being merged into its neighbour, which would silently understate a real
partner.

All organisation names below are invented.
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import partner_names as pn                       # noqa: E402


class TheReportedBugTests(unittest.TestCase):
    def test_an_abbreviation_after_a_semicolon_is_not_a_second_applicant(self):
        self.assertEqual(pn.split_pieces("Northline Statistics Group Inc.; (NSG)"),
                         ["Northline Statistics Group Inc. (NSG)"])

    def test_it_reads_the_same_as_the_never_split_form(self):
        # The shape that was never broken, and the shape that was, must now agree.
        self.assertEqual(pn.split_pieces("Northline Statistics Group (NSG)"),
                         ["Northline Statistics Group (NSG)"])

    def test_the_unaffected_shape_is_still_unaffected(self):
        self.assertEqual(pn.split_pieces("National Bureau of Records (NBR)"),
                         ["National Bureau of Records (NBR)"])

    def test_a_comma_before_the_abbreviation_works_too(self):
        self.assertEqual(pn.split_pieces("Northline Statistics Group, (NSG)"),
                         ["Northline Statistics Group (NSG)"])


class RealPartnersAreStillSplitTests(unittest.TestCase):
    """The separator rule itself is untouched."""

    def test_two_organisations_stay_two(self):
        self.assertEqual(pn.split_pieces("Org North; Org South"), ["Org North", "Org South"])

    def test_three_with_mixed_separators(self):
        self.assertEqual(pn.split_pieces("Org North; Org South, Org East"),
                         ["Org North", "Org South", "Org East"])

    def test_a_legal_suffix_after_a_comma_still_re_attaches(self):
        self.assertEqual(pn.split_pieces("Westvale Media Labs, Inc."), ["Westvale Media Labs, Inc."])

    def test_a_legal_suffix_does_not_swallow_a_following_partner(self):
        self.assertEqual(pn.split_pieces("Westvale Media Labs, Inc.; Org South"),
                         ["Westvale Media Labs, Inc.", "Org South"])


class OnlyARealAbbreviationIsReattachedTests(unittest.TestCase):
    """Re-attaching on 'looks short and shouty' would merge genuine partners. The initials have
    to match, and when they don't the piece stays separate — over-counting is visible and
    correctable, silent merging is not."""

    def test_a_parenthesised_piece_that_is_not_an_abbreviation_stays_separate(self):
        self.assertEqual(pn.split_pieces("Org North; (Riverside Trust)"),
                         ["Org North", "(Riverside Trust)"])

    def test_a_short_but_unrelated_abbreviation_stays_separate(self):
        self.assertEqual(pn.split_pieces("Northline Statistics Group; (XYZ)"),
                         ["Northline Statistics Group", "(XYZ)"])

    def test_a_bare_unparenthesised_abbreviation_stays_separate(self):
        # Without parentheses there is no signal it is an abbreviation rather than an org whose
        # name happens to be an acronym, so existing behaviour is left alone.
        self.assertEqual(pn.split_pieces("Northline Statistics Group; NSG"),
                         ["Northline Statistics Group", "NSG"])

    def test_an_abbreviation_first_is_not_merged_backwards(self):
        self.assertEqual(pn.split_pieces("(NSG); Northline Statistics Group"),
                         ["(NSG)", "Northline Statistics Group"])


class AbbreviationMatchingTests(unittest.TestCase):
    def test_joining_words_are_skipped(self):
        self.assertTrue(pn.is_acronym_of("NBR", "National Bureau of Records"))
        self.assertFalse(pn.is_acronym_of("NBOR", "National Bureau of Records"))

    def test_a_legal_suffix_may_be_counted_or_not(self):
        for token in ("NSG", "NSGI"):
            with self.subTest(token=token):
                self.assertTrue(pn.is_acronym_of(token, "Northline Statistics Group Inc."))

    def test_punctuation_and_case_are_ignored(self):
        self.assertTrue(pn.is_acronym_of("n.s.g.", "Northline Statistics Group"))

    def test_a_single_letter_is_not_an_abbreviation(self):
        self.assertFalse(pn.is_acronym_of("N", "Northline"))

    def test_an_empty_or_missing_name_matches_nothing(self):
        self.assertFalse(pn.is_acronym_of("NSG", ""))
        self.assertFalse(pn.is_acronym_of("", "Northline Statistics Group"))


class TheOrgStillCanonicalisesToOneBarTests(unittest.TestCase):
    """Re-attachment must not give the deploying org a second bar of its own: once
    "Full Name; (FN)" becomes "Full Name (FN)", the canonicaliser has to still recognise it."""

    def test_a_trailing_abbreviation_is_stripped_for_identity(self):
        self.assertEqual(pn.strip_trailing_acronym("Northline Statistics Group (NSG)"),
                         "Northline Statistics Group")

    def test_a_trailing_parenthetical_that_is_not_an_abbreviation_is_kept(self):
        # It is part of the name — dropping it would merge two distinct offices.
        self.assertEqual(pn.strip_trailing_acronym("Org North (Southern Office)"),
                         "Org North (Southern Office)")

    def test_a_name_without_a_parenthetical_is_unchanged(self):
        self.assertEqual(pn.strip_trailing_acronym("Org North"), "Org North")


class BlanksAreNotApplicantsTests(unittest.TestCase):
    def test_empty_input_yields_nothing(self):
        for v in ("", None, "   ", ";", " , ; "):
            with self.subTest(v=v):
                self.assertEqual(pn.split_pieces(v), [])

    def test_the_blank_equivalents_are_listed_for_the_caller_to_drop(self):
        for v in ("n/a", "not applicable", "tbd", "—"):
            with self.subTest(v=v):
                self.assertIn(v, pn.NA_VALUES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
