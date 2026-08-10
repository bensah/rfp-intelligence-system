"""The co-financing / pre-financing match-making matrix (owner 2026-08-10).

Two INDEPENDENT components, each scored the same way — a three-level org capacity against a
tri-state funder requirement:

    org capacity        score   reads as
    none                0.0     Not met
    limited             0.5     Partial, with effort
    strong              1.0     Yes, fully met

    requirement         effect
    Required            ACTIVE, scores as above
    Not required        nothing imposed  -> unscored, out of the denominator
    Not sure / blank    nothing known    -> unscored, out of the denominator

BOTH SIDES must be known. An org capacity on its own is never scored (a funder that asks
for no match cannot fail us on one) and a requirement on its own is never scored either.

"moderate" was dropped from the vocabulary: it scored 1.0, exactly like "strong", so it was
a third label for a second outcome. Stored "moderate" values still score 1.0.
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                      # noqa: E402
from core import org_profile as OP                          # noqa: E402

SCORES = {"none": 0.0, "limited": 0.5, "strong": 1.0}
REQUIRED, NOT_REQUIRED, NOT_SURE = "yes", "no", "not_sure"


def _c(org, donor):
    return {i["key"]: i for i in CD.compliance_factors(org, {}, donor, {})}


def _cofin(capacity=None, requirement=None):
    org = {"org_cofinancing_capacity": capacity} if capacity is not None else {}
    donor = {"donor_cofinancing_required": requirement} if requirement else {}
    return _c(org, donor)["cofinance"]


def _prefin(capacity=None, requirement=None):
    org = {"org_prefinance_capacity": capacity} if capacity is not None else {}
    donor = {"donor_prefinance_required": requirement} if requirement else {}
    return _c(org, donor)["prefinance"]


class TheVocabularyTests(unittest.TestCase):
    def test_three_levels_only(self):
        self.assertEqual(OP.COFINANCING_LEVELS, ("none", "limited", "strong"))

    def test_each_level_maps_to_one_score(self):
        for level, score in SCORES.items():
            with self.subTest(level=level):
                self.assertEqual(CD._capacity_score(level), score)

    def test_a_stored_moderate_still_scores_like_strong(self):
        # Dropped from the picker, but profiles saved with it must not change meaning.
        self.assertEqual(CD._capacity_score("moderate"), 1.0)

    def test_an_unrecorded_capacity_has_no_score(self):
        for raw in (None, "", "   ", "somewhat", "medium"):
            with self.subTest(raw=raw):
                self.assertIsNone(CD._capacity_score(raw))


class TheFullMatrixTests(unittest.TestCase):
    """3 capacities x 3 requirements, for BOTH components."""

    def test_required_scores_the_capacity(self):
        for level, score in SCORES.items():
            for get in (_cofin, _prefin):
                with self.subTest(level=level, component=get.__name__):
                    it = get(level, REQUIRED)
                    self.assertTrue(it["active"], level)
                    self.assertEqual(it["score"], score, level)

    def test_not_required_never_scores(self):
        for level in SCORES:
            for get in (_cofin, _prefin):
                with self.subTest(level=level, component=get.__name__):
                    it = get(level, NOT_REQUIRED)
                    self.assertFalse(it["active"], level)
                    self.assertIsNone(it["score"], level)

    def test_not_sure_never_scores(self):
        for level in SCORES:
            for get in (_cofin, _prefin):
                with self.subTest(level=level, component=get.__name__):
                    self.assertFalse(get(level, NOT_SURE)["active"], level)

    def test_a_blank_requirement_never_scores(self):
        for level in SCORES:
            for get in (_cofin, _prefin):
                with self.subTest(level=level, component=get.__name__):
                    self.assertFalse(get(level, None)["active"], level)

    def test_a_requirement_with_no_recorded_capacity_never_scores(self):
        for get in (_cofin, _prefin):
            with self.subTest(component=get.__name__):
                it = get(None, REQUIRED)
                self.assertFalse(it["active"])
                self.assertIsNone(it["score"])
                self.assertIn("isn't recorded", it["_detail"])

    def test_the_reported_case_limited_against_a_real_requirement(self):
        # Org 'limited' + funder requires it → 0.5 "Partial, with effort". This is the ONLY
        # shape that should produce a partial.
        it = _cofin("limited", REQUIRED)
        self.assertEqual(it["score"], 0.5)
        self.assertEqual(CD.component_mark(it)[0], "◐")

    def test_limited_capacity_alone_is_not_a_partial(self):
        # The reported bug: capacity 'limited' produced ◐ on a funder that required nothing.
        it = _cofin("limited", NOT_REQUIRED)
        self.assertFalse(it["active"])
        self.assertEqual(CD.component_mark(it)[0], "?")


class TheTwoComponentsAreIndependentTests(unittest.TestCase):
    def test_a_cofinancing_requirement_does_not_score_prefinancing(self):
        org = {"org_cofinancing_capacity": "strong", "org_prefinance_capacity": "none"}
        items = _c(org, {"donor_cofinancing_required": REQUIRED})
        self.assertTrue(items["cofinance"]["active"])
        self.assertFalse(items["prefinance"]["active"])

    def test_a_prefinancing_requirement_does_not_score_cofinancing(self):
        org = {"org_cofinancing_capacity": "none", "org_prefinance_capacity": "strong"}
        items = _c(org, {"donor_prefinance_required": REQUIRED})
        self.assertFalse(items["cofinance"]["active"])
        self.assertTrue(items["prefinance"]["active"])

    def test_each_reads_its_OWN_capacity(self):
        org = {"org_cofinancing_capacity": "none", "org_prefinance_capacity": "strong"}
        donor = {"donor_cofinancing_required": REQUIRED,
                 "donor_prefinance_required": REQUIRED}
        items = _c(org, donor)
        self.assertEqual(items["cofinance"]["score"], 0.0)
        self.assertEqual(items["prefinance"]["score"], 1.0)

    def test_both_can_score_at_once(self):
        org = {"org_cofinancing_capacity": "limited", "org_prefinance_capacity": "limited"}
        donor = {"donor_cofinancing_required": REQUIRED,
                 "donor_prefinance_required": REQUIRED}
        items = _c(org, donor)
        self.assertEqual([items["cofinance"]["score"], items["prefinance"]["score"]],
                         [0.5, 0.5])
        self.assertEqual(CD.derive_cofinancing(org, {}, donor, {}),
                         "Partial, with effort")

    def test_neither_is_ever_a_fatal_gate(self):
        org = {"org_cofinancing_capacity": "none", "org_prefinance_capacity": "none"}
        donor = {"donor_cofinancing_required": REQUIRED,
                 "donor_prefinance_required": REQUIRED}
        items = _c(org, donor)
        self.assertFalse(items["cofinance"]["fatal"])
        self.assertFalse(items["prefinance"]["fatal"])


class TheLegacyColumnsStillWorkTests(unittest.TestCase):
    """Migration 092 adds the plainly-named column; the three older ones must keep
    activating the component so no curated research is lost."""

    ORG = {"org_cofinancing_capacity": "limited"}

    def test_cost_sharing_match_still_activates_it(self):
        self.assertTrue(_c(self.ORG,
                           {"donor_cost_sharing_match_required": "yes"})["cofinance"]["active"])

    def test_state_party_cofinancing_still_activates_it(self):
        self.assertTrue(_c(self.ORG,
                           {"donor_state_party_cofinancing_required": "yes"})["cofinance"]["active"])

    def test_a_positive_min_secured_percentage_still_activates_it(self):
        for pct in ("20", 20, 5.5):
            with self.subTest(pct=pct):
                self.assertTrue(_c(self.ORG,
                                   {"donor_min_cofinancing_secured_pct": pct})["cofinance"]["active"])

    def test_a_zero_or_blank_percentage_does_not(self):
        for pct in ("0", 0, "", None):
            with self.subTest(pct=pct):
                self.assertFalse(_c(self.ORG,
                                    {"donor_min_cofinancing_secured_pct": pct})["cofinance"]["active"])

    def test_the_new_column_wins_when_answered(self):
        # Explicitly "Not required" must beat a stale legacy cost-sharing flag.
        donor = {"donor_cofinancing_required": NOT_REQUIRED,
                 "donor_cost_sharing_match_required": "yes"}
        self.assertFalse(_c(self.ORG, donor)["cofinance"]["active"])

    def test_a_legacy_explicit_no_does_not_activate_it(self):
        self.assertFalse(_c(self.ORG,
                            {"donor_cost_sharing_match_required": "no"})["cofinance"]["active"])

    def test_the_legacy_payment_modality_still_activates_nothing(self):
        it = _c({"org_prefinance_capacity": "limited"},
                {"donor_prefinance_required": "reimbursement_only"})["prefinance"]
        self.assertFalse(it["active"])
        self.assertIn("reimburses in arrears", it["_detail"])


class TheCallCanImposeCoFinancingTests(unittest.TestCase):
    def test_a_cost_share_clause_in_the_call_activates_it(self):
        org = {"org_cofinancing_capacity": "limited"}
        rfp = {"brief_description": "Cost-share required: 25% of total project cost."}
        it = {i["key"]: i for i in CD.compliance_factors(org, rfp, {}, {})}["cofinance"]
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
