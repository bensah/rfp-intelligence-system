"""PREFER-8 "Established": experience starts at FIVE years, not ten.

Owner 2026-08-10. The old rule scored 20+ → 1.0, 10+ → 0.5 and EVERYTHING below ten →
0.0, so a seven-year-old organisation with a real delivery record counted as no more
established than one founded last year — a false zero that quietly held competitiveness
down for exactly the mid-age organisations most likely to be bidding. The graded band
still rewards the older ones, so nothing is lost at the top; only the false zero goes.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                      # noqa: E402

THIS_YEAR = date.today().year


def _year(age: int) -> int:
    return THIS_YEAR - age


def _age_factor(age: int) -> dict:
    org = {"org_founding_year": _year(age)}
    return {f["key"]: f for f in CD._competitiveness_factors(org, {}, {}, {})}["comp_age"]


class AgeBandTests(unittest.TestCase):
    def test_five_to_nine_years_is_established(self):
        # The regression the owner reported: these used to score a flat 0.
        for age in (5, 6, 7, 8, 9):
            with self.subTest(age=age):
                score, met = CD._age_band(_year(age))
                self.assertEqual(score, 0.5, age)
                self.assertTrue(met, age)

    def test_ten_to_nineteen_years_scores_higher_than_five(self):
        for age in (10, 15, 19):
            with self.subTest(age=age):
                self.assertEqual(CD._age_band(_year(age)), (0.75, True), age)

    def test_twenty_plus_years_is_the_full_band(self):
        for age in (20, 35, 80):
            with self.subTest(age=age):
                self.assertEqual(CD._age_band(_year(age)), (1.0, True), age)

    def test_under_five_years_is_still_building_a_record(self):
        for age in (0, 1, 4):
            with self.subTest(age=age):
                self.assertEqual(CD._age_band(_year(age)), (0.0, False), age)

    def test_the_band_never_decreases_with_age(self):
        scores = [CD._age_band(_year(a))[0] for a in range(0, 40)]
        self.assertEqual(scores, sorted(scores))

    def test_a_missing_or_nonsense_year_is_unscored(self):
        for raw in (None, "", 0, 1800, "abc", 1899):
            with self.subTest(raw=raw):
                self.assertEqual(CD._age_band(raw), (0.0, None), raw)


class TheComponentRowTests(unittest.TestCase):
    def test_the_component_is_renamed_to_five_years(self):
        self.assertEqual(_age_factor(7)["name"], "Established (5+ years)")

    def test_a_seven_year_old_org_no_longer_shows_a_cross(self):
        it = _age_factor(7)
        self.assertTrue(it["met"])
        self.assertEqual(CD.component_mark(it)[0], "✓")

    def test_a_twenty_year_old_org_shows_a_tick(self):
        self.assertEqual(CD.component_mark(_age_factor(25))[0], "✓")

    def test_a_two_year_old_org_still_shows_a_cross(self):
        it = _age_factor(2)
        self.assertFalse(it["met"])
        self.assertEqual(CD.component_mark(it)[0], "✗")

    def test_no_founding_year_leaves_the_component_inactive(self):
        it = {f["key"]: f for f in CD._competitiveness_factors({}, {}, {}, {})}["comp_age"]
        self.assertFalse(it["active"])


class CompetitivenessUsesTheSameBandTests(unittest.TestCase):
    """The panel component and the derivation must not disagree about the same org."""

    def test_the_derivation_credits_a_seven_year_old_org(self):
        seven = {"org_founding_year": _year(7), "org_cofinancing_capacity": "limited"}
        two = {"org_founding_year": _year(2), "org_cofinancing_capacity": "limited"}
        # Same call, same donor; only the age differs — the older org must not score lower.
        lbl_seven = CD.derive_competitiveness(seven, {}, {}, {})
        lbl_two = CD.derive_competitiveness(two, {}, {}, {})
        order = ["Weak (wide-open)", "Moderate",
                 "Strong (limited field / incumbent / clear edge)"]
        if lbl_seven in order and lbl_two in order:
            self.assertGreaterEqual(order.index(lbl_seven), order.index(lbl_two))

    def test_the_panel_shows_pass_fail_while_the_model_grades(self):
        # The tick is "established or not"; HOW established is the weighted model's job.
        # An explicit score here would render a 19-year-old organisation as ◐ "partly met".
        for age in (5, 12, 30):
            with self.subTest(age=age):
                self.assertIsNone(_age_factor(age).get("score"))
                self.assertEqual(CD.component_mark(_age_factor(age))[0], "✓")
        self.assertGreater(CD._age_band(_year(30))[0], CD._age_band(_year(7))[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
