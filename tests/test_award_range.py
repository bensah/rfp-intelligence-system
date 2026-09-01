"""Ranged / tiered award capture + display.

Covers the fix for calls that publish SEVERAL award amounts (tiers or a range)
rather than a single figure — e.g. the Grand Challenges pathogen-sequencing RFP
whose "Award Structure and Funding Level" section lists Tier 1/2/3 of "up to
US$300,000 / 600,000 / 800,000". The Value field must read "US $300,000 – US
$800,000", while a single-amount call keeps showing the one figure.
"""
import unittest

from core import extract
from core import opportunity_detail as od


class TestTiersToBounds(unittest.TestCase):
    def test_up_to_tiers_span_min_cap_to_max_cap(self):
        # "up to X" tiers → amount_min null/0, amount_max the cap.
        tiers = [
            {"stage": "Tier 1", "amount_min": 0, "amount_max": 300000},
            {"stage": "Tier 2", "amount_min": None, "amount_max": 600000},
            {"stage": "Tier 3", "amount_max": 800000},
        ]
        self.assertEqual(extract.tiers_to_bounds(tiers), (300000, 800000))

    def test_explicit_min_max_ranges(self):
        tiers = [
            {"stage": "Seed", "amount_min": 50000, "amount_max": 100000},
            {"stage": "Scale", "amount_min": 200000, "amount_max": 500000},
        ]
        self.assertEqual(extract.tiers_to_bounds(tiers), (50000, 500000))

    def test_empty_and_malformed(self):
        self.assertEqual(extract.tiers_to_bounds([]), (None, None))
        self.assertEqual(extract.tiers_to_bounds(None), (None, None))
        self.assertEqual(extract.tiers_to_bounds(["nonsense", {}]), (None, None))


class TestAwardHeadline(unittest.TestCase):
    def test_range_when_floor_differs_from_ceiling(self):
        row = {"call_award_floor": 300000, "call_award_ceiling": 800000,
               "grant_amount": 800000, "currency": "USD"}
        self.assertEqual(od.award_headline(row), "US $300,000 – US $800,000")

    def test_single_value_when_no_range(self):
        # A single clear value keeps the current behaviour — one figure, no dash.
        row = {"call_award_floor": None, "call_award_ceiling": None,
               "grant_amount": 500000, "currency": "USD"}
        self.assertEqual(od.award_headline(row), "US $500,000")

    def test_floor_equal_ceiling_is_single(self):
        row = {"call_award_floor": 500000, "call_award_ceiling": 500000,
               "grant_amount": 500000, "currency": "USD"}
        self.assertEqual(od.award_headline(row), "US $500,000")

    def test_submissions_schema_value_column(self):
        # rfp_submissions carries call_award_value, not grant_amount.
        row = {"call_award_value": 250000, "currency": "USD"}
        self.assertEqual(od.award_headline(row), "US $250,000")

    def test_only_ceiling_known_shows_it(self):
        row = {"call_award_ceiling": 800000, "currency": "USD"}
        self.assertEqual(od.award_headline(row), "US $800,000")

    def test_no_amount_returns_empty(self):
        self.assertEqual(od.award_headline({"currency": "USD"}), "")

    def test_bounds_reader(self):
        self.assertEqual(
            od.award_range_bounds({"call_award_floor": 1, "call_award_ceiling": 9}),
            (1.0, 9.0))
        self.assertEqual(od.award_range_bounds({}), (None, None))


if __name__ == "__main__":
    unittest.main()
