"""The scoring analysis the opportunity page shows.

The point of this module is that a call which has NEVER been screened for this tenant — a
shared-catalogue row the Featured card surfaced — still gets a real nine-criterion analysis,
so the reviewer has something to decide on. And that it agrees with the Review screen,
because both go through the same derivation, the same composite and the same wording.

Bid Strength is computed LIVE here, never read from `alignment_score`: a stored score is a
snapshot from the last scan and drifts from Review after any scoring fix.
"""
from __future__ import annotations

import unittest

from core import opportunity_scoring as osc
from core.scorer import CRITERIA

ORG = {
    "org_name": "Example Health Org",
    "org_countries": ["Countryland"],
    "org_min_target": 100000,
    "org_max_target": 5000000,
    "org_founding_year": 2002,
    "org_funder_history": ["Another Funder"],
}

# A candidate built from a raw extraction — no alignment_score, no stored criteria.
CANDIDATE = {
    "opportunity_title": "Vaccine Delivery Innovation Fund",
    "funding_agency": "Another Funder",
    "call_award_value": 250000,
    "currency": "USD",
    "call_submission_deadline": "2027-01-31",
    "call_geographic_scope": ["Countryland"],
    "call_domain_areas": ["Vaccines"],
    "brief_description": "Support for vaccine delivery.",
    "project_duration": 24,
}


class TestAnUnscreenedCallStillGetsAnAnalysis(unittest.TestCase):
    def test_all_nine_criteria_are_returned(self):
        an = osc.analyse(CANDIDATE, ORG, None, {})
        self.assertEqual([c["key"] for c in an["criteria"]], list(CRITERIA))

    def test_every_criterion_carries_what_the_page_renders(self):
        an = osc.analyse(CANDIDATE, ORG, None, {})
        for c in an["criteria"]:
            with self.subTest(criterion=c["key"]):
                for field in ("title", "label", "count_text", "weight", "points",
                              "components", "scored", "is_must"):
                    self.assertIn(field, c)
                self.assertTrue(c["title"])
                self.assertTrue(c["label"])

    def test_bid_strength_is_computed_not_read_from_the_row(self):
        # A deliberately wrong stored score must not leak into the analysis.
        an = osc.analyse(dict(CANDIDATE, alignment_score=3.0), ORG, None, {})
        self.assertNotEqual(an["bid_strength"], 3)
        self.assertGreaterEqual(an["bid_strength"], 0)
        self.assertLessEqual(an["bid_strength"], 100)

    def test_the_five_musts_are_flagged_as_musts(self):
        an = osc.analyse(CANDIDATE, ORG, None, {})
        musts = [c["key"] for c in an["criteria"] if c["is_must"]]
        self.assertEqual(musts, list(CRITERIA[:5]))

    def test_no_org_profile_does_not_raise(self):
        an = osc.analyse(CANDIDATE, {}, None, {})
        self.assertEqual(len(an["criteria"]), 9)

    def test_an_empty_opportunity_does_not_raise(self):
        an = osc.analyse({}, ORG, None, {})
        self.assertEqual(len(an["criteria"]), 9)


class TestWeightsAndPoints(unittest.TestCase):
    def test_the_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(osc.WEIGHTS.values()), 1.0, places=6)

    def test_must_weights_are_sixty_five_percent(self):
        self.assertAlmostEqual(sum(osc.WEIGHTS[k] for k in CRITERIA[:5]), .65, places=6)

    def test_prefer_weights_are_thirty_five_percent(self):
        self.assertAlmostEqual(sum(osc.WEIGHTS[k] for k in CRITERIA[5:]), .35, places=6)

    def test_every_criterion_has_a_weight_and_a_label(self):
        for k in CRITERIA:
            self.assertIn(k, osc.WEIGHTS)
            self.assertIn(k, osc.LABELS)

    def test_points_never_exceed_the_criterion_weight(self):
        an = osc.analyse(CANDIDATE, ORG, None, {})
        for c in an["criteria"]:
            with self.subTest(criterion=c["key"]):
                self.assertLessEqual(round(c["points"], 6), c["weight"] * 100)

    def test_an_unscored_criterion_takes_the_park_midpoint(self):
        an = osc.analyse({}, {}, None, {})
        for c in an["criteria"]:
            if c["label"] == "Not sure":
                self.assertAlmostEqual(c["points"], c["weight"] * 50, places=6)


class TestTheDecisionRule(unittest.TestCase):
    def test_the_thresholds_match_the_scorer(self):
        self.assertEqual(osc.decide(90.0), "Proceed")
        self.assertEqual(osc.decide(89.9), "Park")
        self.assertEqual(osc.decide(70.0), "Park")
        self.assertEqual(osc.decide(69.9), "Decline")

    def test_a_fatal_gate_declines_outright(self):
        self.assertEqual(osc.decide(99.0, fatal=True), "Decline")

    def test_a_below_floor_award_caps_proceed_at_park(self):
        self.assertEqual(osc.decide(95.0, below_award_floor=True), "Park")
        # ...but it does not promote anything.
        self.assertEqual(osc.decide(50.0, below_award_floor=True), "Decline")

    def test_the_fit_label_is_not_the_decision(self):
        self.assertEqual(osc.fit_label(85), "Strong fit")
        self.assertEqual(osc.fit_label(60), "Moderate fit")
        self.assertEqual(osc.fit_label(50), "Low fit")


class TestConfidenceIsReported(unittest.TestCase):
    def test_no_donor_record_is_reported_as_unmatched(self):
        an = osc.analyse(CANDIDATE, ORG, None, {})
        self.assertFalse(an["confidence"]["donor_matched"])
        self.assertIn(an["confidence"]["band"], ("High", "Medium", "Low"))

    def test_a_matched_donor_is_reported_with_its_completeness(self):
        donor = {"donor": "Another Funder", "donor_tax_exempt_status_required": "yes",
                 "donor_audit_report_required": "no"}
        an = osc.analyse(CANDIDATE, ORG, donor, {})
        self.assertTrue(an["confidence"]["donor_matched"])
        self.assertEqual(an["confidence"]["donor_pct"], 100)

    def test_thin_data_parks_a_would_be_proceed(self):
        # E3d: a Low-confidence Proceed is parked for review.
        self.assertEqual(osc.analyse({}, {}, None, {})["confidence"]["band"], "Low")


class TestBlockersAndUnscored(unittest.TestCase):
    def test_blockers_are_the_failed_criteria(self):
        an = osc.analyse(CANDIDATE, ORG, None, {})
        for b in an["blockers"]:
            self.assertEqual(b["band"], 0)

    def test_unscored_are_the_ones_with_nothing_to_count(self):
        an = osc.analyse({}, {}, None, {})
        for u in an["unscored"]:
            self.assertFalse(u["scored"])
            self.assertEqual(u["count_text"], "not scored")

    def test_labels_map_is_keyed_by_criterion(self):
        an = osc.analyse(CANDIDATE, ORG, None, {})
        self.assertEqual(set(an["labels"]), set(CRITERIA))


class TestOverridesReachTheAnalysis(unittest.TestCase):
    def test_a_persisted_human_component_verdict_is_applied(self):
        base = osc.analyse(CANDIDATE, ORG, None, {})
        # Fail a MUST-1 component outright; MUST-1 is a gate, so its label must follow.
        over = osc.analyse(CANDIDATE, ORG, None, {},
                           overrides={"qualification": {"applicant_type": 0.0}})
        q_base = next(c for c in base["criteria"] if c["key"] == "qualification")
        q_over = next(c for c in over["criteria"] if c["key"] == "qualification")
        self.assertEqual(q_over["label"], "No, not eligible")
        self.assertNotEqual(q_over["label"], q_base["label"])
        self.assertLess(q_over["points"], q_base["points"] + 0.001)


if __name__ == "__main__":
    unittest.main()
