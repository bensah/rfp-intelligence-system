"""Pre-financing gets its own question on BOTH sides, and the auto-scorer stays intact.

Owner 2026-08-10. MUST-5's pre-financing component could not score at all, because there
was nothing to score against: the org profile only had `org_cofinancing_capacity`, and
reading pre-financing off that is exactly the conflation that made a Gates-shaped record
read "Partial, with effort" when nothing was required. So:

  org side    `org_prefinance_capacity`   none | limited | moderate | strong (blank = not
                                          recorded → unscored, never assumed)
  donor side  `donor_prefinance_required`  the SAME tri-state as every other requirement:
                                          Required / Not required / Not sure

The second class here answers a separate question the owner asked: does a human's
component override change what the AUTO-SCORER produces? It must not. Human verdicts are
per-row display/decision data; the scan's own scoring path must be untouched by them, or
the system stops being independently reliable.
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


def _prefin(org, donor):
    return {i["key"]: i for i in
            CD.compliance_factors(org, {}, donor, {})}["prefinance"]


REQUIRED = {"donor_prefinance_required": "yes"}
NOT_REQUIRED = {"donor_prefinance_required": "no"}
NOT_SURE = {"donor_prefinance_required": "not_sure"}
LEGACY_REIMBURSE = {"donor_prefinance_required": "reimbursement_only"}


class TheOrgFieldTests(unittest.TestCase):
    def test_the_profile_carries_a_prefinance_capacity(self):
        self.assertIn("org_prefinance_capacity", OP.DEFAULT_PROFILE)

    def test_it_defaults_to_not_recorded_not_to_a_guess(self):
        # A default of "limited" would silently assert a capability nobody stated.
        self.assertIsNone(OP.DEFAULT_PROFILE["org_prefinance_capacity"])

    def test_it_uses_the_same_four_levels_as_cofinancing(self):
        for level in OP.COFINANCING_LEVELS:
            with self.subTest(level=level):
                it = _prefin({"org_prefinance_capacity": level}, REQUIRED)
                self.assertTrue(it["active"], level)
                self.assertIsNotNone(it["score"], level)

    def test_the_levels_score_as_specified(self):
        want = {"strong": 1.0, "moderate": 1.0, "limited": 0.5, "none": 0.0}
        for level, score in want.items():
            with self.subTest(level=level):
                self.assertEqual(
                    _prefin({"org_prefinance_capacity": level}, REQUIRED)["score"], score)

    def test_it_needs_no_rename_ledger_entry(self):
        # _RENAMED_KEYS migrates PRE-EXISTING keys to their new names. This field is new,
        # so it has no legacy name — adding it there would claim a rename that never
        # happened.
        self.assertNotIn("prefinance_capacity", OP._RENAMED_KEYS)
        self.assertNotIn("org_prefinance_capacity", OP._RENAMED_KEYS.values())


class TheDonorTriStateTests(unittest.TestCase):
    def test_required_activates_the_component(self):
        org = {"org_prefinance_capacity": "limited"}
        self.assertTrue(_prefin(org, REQUIRED)["active"])

    def test_not_required_and_not_sure_leave_it_alone(self):
        org = {"org_prefinance_capacity": "limited"}
        for donor in (NOT_REQUIRED, NOT_SURE, {}):
            with self.subTest(donor=donor):
                self.assertFalse(_prefin(org, donor)["active"], donor)

    def test_the_legacy_payment_modality_is_not_a_requirement(self):
        # "reimbursement_only" describes when money arrives, not who may apply.
        org = {"org_prefinance_capacity": "limited"}
        it = _prefin(org, LEGACY_REIMBURSE)
        self.assertFalse(it["active"])
        self.assertIn("reimburses in arrears", it["_detail"])

    def test_an_unrecorded_org_capacity_stays_unscored_even_when_required(self):
        # Both sides must be known — it must never borrow the co-financing answer.
        org = {"org_cofinancing_capacity": "strong"}          # no prefinance field
        it = _prefin(org, REQUIRED)
        self.assertFalse(it["active"])
        self.assertIsNone(it["score"])
        self.assertIn("isn't recorded", it["_detail"])

    def test_it_never_reads_the_cofinancing_capacity(self):
        strong_cofin = {"org_cofinancing_capacity": "strong",
                        "org_prefinance_capacity": "none"}
        self.assertEqual(_prefin(strong_cofin, REQUIRED)["score"], 0.0)

    def test_it_is_never_a_fatal_gate(self):
        org = {"org_prefinance_capacity": "none"}
        self.assertFalse(_prefin(org, REQUIRED)["fatal"])


class TheAutoScorerIsIndependentOfHumanOverridesTests(unittest.TestCase):
    """The owner's question: with human edits in play, does the automated scoring still
    run normally? It must — overrides are per-row review data, not scoring inputs."""

    ORG = {"org_cofinancing_capacity": "limited", "org_founding_year": 2007,
           "org_operating_countries": ["Countryland"]}
    RFP = {"opportunity_title": "A call", "funding_agency": "A Funder",
           "call_award_value": 250000, "call_geographic_scope": ["Countryland"],
           "call_submission_deadline": "2027-01-31"}

    def test_derive_criteria_takes_no_overrides_argument(self):
        # The derivation cannot see overrides even in principle — they are applied to the
        # BREAKDOWN afterwards, for display, by factor_breakdown(overrides=...).
        import inspect
        self.assertNotIn("overrides",
                         inspect.signature(CD.derive_criteria).parameters)

    def test_the_derivation_is_identical_with_and_without_overrides_stored(self):
        base = CD.derive_criteria(self.RFP, self.ORG, {}, {})
        with_ov = CD.derive_criteria(
            {**self.RFP, "criteria_component_overrides":
                {"qualification": {"applicant_type": 0.0},
                 "cofinancing": {"cofinance": 0.0}}},
            self.ORG, {}, {})
        self.assertEqual(base, with_ov)

    def test_a_stored_override_column_does_not_change_the_fatal_verdict(self):
        a = CD.fatal_decline(self.ORG, self.RFP, {}, {})
        b = CD.fatal_decline(
            self.ORG, {**self.RFP, "criteria_component_overrides":
                       {"geographic_fit": {"geo_presence": 0.0}}}, {}, {})
        self.assertEqual(a, b)

    def test_overrides_only_reach_the_breakdown_when_passed_explicitly(self):
        without = {i["key"]: i for i in
                   CD.factor_breakdown(self.RFP, self.ORG, {}, {})["qualification"]}
        withov = {i["key"]: i for i in
                  CD.factor_breakdown(self.RFP, self.ORG, {}, {},
                                      overrides={"qualification":
                                                 {"applicant_type": 0.0}})
                  ["qualification"]}
        self.assertIsNone(without["applicant_type"].get("_override"))
        self.assertTrue(withov["applicant_type"]["_override"])
        self.assertEqual(withov["applicant_type"]["score"], 0.0)

    def test_an_override_never_silently_widens_to_other_components(self):
        bd = CD.factor_breakdown(self.RFP, self.ORG, {}, {},
                                 overrides={"qualification": {"applicant_type": 1.0}})
        touched = [i["key"] for i in bd["qualification"] if i.get("_override")]
        self.assertEqual(touched, ["applicant_type"])

    def test_an_override_on_one_criterion_does_not_touch_another(self):
        bd = CD.factor_breakdown(self.RFP, self.ORG, {}, {},
                                 overrides={"qualification": {"applicant_type": 0.0}})
        for key in ("cofinancing", "capacity", "competitiveness", "bid_effort"):
            with self.subTest(criterion=key):
                self.assertFalse(any(i.get("_override") for i in bd[key]), key)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AClearedComponentSurvivesTheRoundTripTests(unittest.TestCase):
    """A reviewer clearing a component ("—") persists as a NULL in
    criteria_component_overrides. `apply_component_overrides` used to call float(None),
    raise, and hit its `continue` — so a saved clear was silently ignored and the derived
    score reappeared on the next render."""

    ORG = {"org_cofinancing_capacity": "limited", "org_founding_year": 2007}
    RFP = {"opportunity_title": "A call", "funding_agency": "A Funder",
           "call_award_value": 250000, "call_submission_deadline": "2027-01-31"}

    def _comp(self, overrides):
        bd = CD.factor_breakdown(self.RFP, self.ORG, {"donor_cost_sharing_match_required":
                                                      "yes"}, {}, overrides=overrides)
        return {i["key"]: i for i in bd["cofinancing"]}["cofinance"]

    def test_without_an_override_the_derivation_scores_it(self):
        it = self._comp(None)
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.5)          # capacity 'limited'

    def test_a_stored_null_clears_the_component(self):
        it = self._comp({"cofinancing": {"cofinance": None}})
        self.assertFalse(it["active"])
        self.assertIsNone(it["score"])
        self.assertIsNone(it["met"])
        self.assertTrue(it["_cleared"])
        self.assertTrue(it["_override"])

    def test_a_cleared_component_leaves_the_denominator(self):
        from core import criteria_review as CR
        bd = CD.factor_breakdown(self.RFP, self.ORG,
                                 {"donor_cost_sharing_match_required": "yes"}, {},
                                 overrides={"cofinancing": {"cofinance": None}})
        keys = [i["key"] for i in CR.active_components(bd["cofinancing"])]
        self.assertNotIn("cofinance", keys)

    def test_a_stored_score_still_overrides_normally(self):
        it = self._comp({"cofinancing": {"cofinance": 1.0}})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 1.0)
        self.assertFalse(it.get("_cleared"))

    def test_a_stored_zero_is_a_score_not_a_clear(self):
        # 0 is falsy — it must NOT be mistaken for "cleared".
        it = self._comp({"cofinancing": {"cofinance": 0.0}})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)
        self.assertFalse(it.get("_cleared"))

    def test_junk_in_the_column_is_ignored_not_treated_as_a_clear(self):
        it = self._comp({"cofinancing": {"cofinance": "banana"}})
        self.assertTrue(it["active"])            # untouched by the bad override
        self.assertEqual(it["score"], 0.5)
