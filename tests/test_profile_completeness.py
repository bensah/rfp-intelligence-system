"""Regression tests for the Entity profile-completeness helpers.

Covers the RAG band thresholds (≤50 red · 50–80 amber · ≥80 green) and the
"screening-ready" nudge, which must name ONLY the piece a tenant is actually missing
(the reported bug: it told a user to add a country of operation they'd already set).

Run:  python -m unittest tests.test_profile_completeness
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import profile_completeness as C     # noqa: E402


class RagBandTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(C.rag_band(0.0), "red")
        self.assertEqual(C.rag_band(0.23), "red")
        self.assertEqual(C.rag_band(0.50), "red")      # ≤50 → red (boundary)
        self.assertEqual(C.rag_band(0.5001), "amber")
        self.assertEqual(C.rag_band(0.64), "amber")    # the screenshot case
        self.assertEqual(C.rag_band(0.79), "amber")
        self.assertEqual(C.rag_band(0.80), "green")    # ≥80 → green (boundary)
        self.assertEqual(C.rag_band(1.0), "green")

    def test_every_band_has_a_colour(self):
        for pct in (0.1, 0.6, 0.9):
            self.assertIn(C.rag_band(pct), C.RAG_COLOR)


class ReadinessGapTests(unittest.TestCase):
    def test_country_set_but_no_program_area_asks_only_for_area(self):
        # The reported bug: Cameroon operating-country IS set, program areas are not.
        gap = C.readiness_gap({"org_operating_countries": ["Cameroon"]})
        self.assertEqual(gap, ["at least one program area"])

    def test_program_area_but_no_country_asks_only_for_country(self):
        gap = C.readiness_gap({"org_domain_expertise": ["WCH - Immunization"]})
        self.assertEqual(gap, ["at least one country of operation"])

    def test_nothing_set_asks_for_both(self):
        gap = C.readiness_gap({})
        self.assertEqual(gap, ["at least one country of operation", "at least one program area"])

    def test_both_set_is_ready(self):
        self.assertEqual(
            C.readiness_gap({"org_operating_countries": ["Cameroon"],
                             "org_priority_areas": ["WCH - Immunization"]}),
            [])


class CompletenessTests(unittest.TestCase):
    def test_empty_is_zero(self):
        pct, missing = C.completeness({}, {})
        self.assertEqual(pct, 0.0)
        self.assertIn("countries of operation", missing)

    def test_geo_only_is_partial(self):
        pct, _ = C.completeness({}, {"org_operating_countries": ["Cameroon"],
                                     "org_registered_countries": ["Cameroon"]})
        self.assertTrue(0.0 < pct < 0.5)

    def test_zero_budget_counts_as_unset(self):
        _, missing = C.completeness({}, {"org_annual_budget": 0})
        self.assertIn("annual budget", missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
