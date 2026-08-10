"""MUST-5: co-financing and PRE-financing are different requirements.

TWO CORRECTIONS ARE RECORDED HERE, and the second reverses part of the first.

1. (2026-08-07) An unrecorded capacity is not a partial. The score used to fall through a
   trailing `else 0.5`, so a BLANK `org_cofinancing_capacity` — nothing recorded — rendered
   as the same ◐ "partial" as a real "limited". Both sides must be known. That still holds.

2. (2026-08-10, owner) The component was scoring the WRONG THING. It was one merged
   "Co-financing / pre-finance capacity" component, and these are different requirements:

     co-financing  — the funder expects the org to commit its OWN funds alongside the
                     award, as a condition of eligibility.
     pre-financing — the org must fund activities up front and be reimbursed later. A
                     cash-flow modality; a funder that reimburses in arrears has not asked
                     the applicant to contribute anything.

   The merge let `donor_prefinance_required = 'reimbursement_only'` ACTIVATE a co-financing
   requirement and then scored it against `org_cofinancing_capacity` — a pre-financing
   value measured against a co-financing capacity. This file previously asserted that
   behaviour was correct (`test_the_reported_row_still_reads_partial`); it was not.

   On a real funder whose `donor_cost_sharing_match_required` is explicitly "no", whose
   state-party co-financing is unset and whose min-secured-% is blank, the only thing
   activating the component was the reimbursement modality. It invented an eligibility
   requirement the funder never imposed and cost MUST-5 half its weight. 9 live rows were
   affected; all 9 moved from "Partial, with effort" to "Yes, fully met", and no decision
   changed.

   Pre-financing is now its own component, and it cannot be SCORED at all yet: rule 1
   applies to it too, and the org profile has no pre-financing capacity field — only
   `org_cofinancing_capacity`. Reading pre-financing off that field is precisely the
   conflation. It stays inactive until `org_prefinance_capacity` exists, while the
   reimbursement fact is still REPORTED as a delivery risk.

Run:  python -m unittest tests.test_cofinancing_unknown_capacity
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                      # noqa: E402
from core.org_profile import COFINANCING_LEVELS             # noqa: E402

# A funder that reimburses in arrears. NOT a co-financing requirement (correction 2).
REIMBURSE = {"donor_prefinance_required": "reimbursement_only"}
# A genuine co-financing requirement: the applicant must match part of the award.
MATCH = {"donor_cost_sharing_match_required": "yes"}


def _items(org, donor, rfp=None):
    return {i["key"]: i for i in CD.compliance_factors(org, rfp or {}, donor, {})}


def _item(org, donor, rfp=None):
    return _items(org, donor, rfp)["cofinance"]


def _prefin(org, donor, rfp=None):
    return _items(org, donor, rfp)["prefinance"]


class UnknownCapacityTests(unittest.TestCase):
    def test_an_unrecorded_capacity_is_not_a_partial(self):
        for blank in ({}, {"org_cofinancing_capacity": ""},
                      {"org_cofinancing_capacity": None},
                      {"org_cofinancing_capacity": "   "}):
            it = _item(blank, MATCH)
            self.assertFalse(it["active"], repr(blank))
            self.assertIsNone(it["score"], repr(blank))
            self.assertEqual(CD.component_mark(it)[0], "?", repr(blank))

    def test_an_unrecognised_capacity_value_is_also_not_a_partial(self):
        it = _item({"org_cofinancing_capacity": "somewhat"}, MATCH)
        self.assertFalse(it["active"])

    def test_it_no_longer_lands_in_the_denominator_unmeasured(self):
        act = {i["key"] for i in CD.compliance_factors({}, {}, MATCH, {})
               if i["active"] and i["score"] is not None}
        self.assertNotIn("cofinance", act)


class RecordedCapacityTests(unittest.TestCase):
    """strong/moderate → 1 · limited → 0.5 · none → 0, exactly as specified."""

    def test_the_whole_vocabulary_scores_as_specified(self):
        want = {"strong": 1.0, "moderate": 1.0, "limited": 0.5, "none": 0.0}
        for level in COFINANCING_LEVELS:
            it = _item({"org_cofinancing_capacity": level}, MATCH)
            self.assertTrue(it["active"], level)
            self.assertEqual(it["score"], want[level], level)

    def test_a_limited_capacity_against_a_real_requirement_reads_partial(self):
        # A GENUINE co-financing requirement + capacity 'limited' → ◐ is correct.
        it = _item({"org_cofinancing_capacity": "limited"}, MATCH)
        self.assertEqual(it["score"], 0.5)
        self.assertEqual(CD.component_mark(it)[0], "◐")
        self.assertIn("co-fund", it["_detail"])

    def test_no_capacity_at_all_is_a_real_zero(self):
        it = _item({"org_cofinancing_capacity": "none"}, MATCH)
        self.assertEqual(it["score"], 0.0)
        self.assertEqual(CD.component_mark(it)[0], "✗")


class ActivationTests(unittest.TestCase):
    ORG = {"org_cofinancing_capacity": "limited"}

    def test_silence_on_both_sides_leaves_it_inactive(self):
        self.assertFalse(_item(self.ORG, {})["active"])

    def test_a_donor_requirement_activates_it(self):
        for donor in (MATCH,
                      {"donor_min_cofinancing_secured_pct": "20"},
                      {"donor_state_party_cofinancing_required": "yes"}):
            self.assertTrue(_item(self.ORG, donor)["active"], donor)

    def test_a_numeric_secured_percentage_activates_it(self):
        # It is a NUMBER ("20" = 20% must already be secured) but was tested with
        # `_truthy`, which only accepts yes/true/required — so a donor stating a real
        # threshold never activated the check at all.
        for pct in ("20", 20, 5.5):
            self.assertTrue(
                _item(self.ORG, {"donor_min_cofinancing_secured_pct": pct})["active"], pct)

    def test_a_zero_or_blank_percentage_does_not_activate_it(self):
        for pct in ("0", 0, "", None):
            self.assertFalse(
                _item(self.ORG, {"donor_min_cofinancing_secured_pct": pct})["active"], pct)

    def test_a_cost_share_clause_in_the_call_activates_it(self):
        rfp = {"brief_description": "Cost-share required: 25% of total project cost."}
        self.assertTrue(_item(self.ORG, {}, rfp)["active"])

    def test_a_donor_explicitly_saying_no_does_not_activate_it(self):
        # _truthy rejects 'no' — a curated "not required" must not impose the gate.
        self.assertFalse(_item(self.ORG, {"donor_cost_sharing_match_required": "no"})["active"])

    def test_it_is_never_a_fatal_gate(self):
        it = _item({"org_cofinancing_capacity": "none"}, MATCH)
        self.assertFalse(it["fatal"])
        self.assertFalse(CD.fatal_decline({"org_cofinancing_capacity": "none"}, {}, MATCH)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CoFinancingIsNotPreFinancingTests(unittest.TestCase):
    """Correction 2 (owner 2026-08-10). The two are different requirements, they are
    scored against different org capabilities, and a payment modality is neither."""

    ORG = {"org_cofinancing_capacity": "limited"}

    def test_reimbursement_only_does_not_impose_a_cofinancing_requirement(self):
        # The reported case: the funder reimburses in arrears and asks for no match. That
        # is not a co-financing requirement, so there is nothing to score.
        it = _item(self.ORG, REIMBURSE)
        self.assertFalse(it["active"])
        self.assertIsNone(it["score"])

    def test_the_real_funder_shape_reads_fully_met(self):
        # cost-sharing explicitly "no", state-party co-financing unset, min-secured-% blank,
        # prefinance = reimbursement_only. Nothing is imposed → MUST-5's all-clear carries
        # it, instead of a ◐ 0.5 invented from the payment modality.
        donor = {"donor_cost_sharing_match_required": "no",
                 "donor_state_party_cofinancing_required": None,
                 "donor_min_cofinancing_secured_pct": None,
                 "donor_prefinance_required": "reimbursement_only"}
        self.assertFalse(_item(self.ORG, donor)["active"])
        self.assertEqual(CD.derive_cofinancing(self.ORG, {}, donor, {}), "Yes, fully met")

    def test_the_two_components_are_separate(self):
        keys = _items(self.ORG, MATCH)
        self.assertIn("cofinance", keys)
        self.assertIn("prefinance", keys)
        self.assertEqual(keys["cofinance"]["name"], "Co-financing capacity")
        self.assertEqual(keys["prefinance"]["name"], "Pre-financing capacity")

    def test_a_cofinancing_requirement_does_not_activate_prefinancing(self):
        self.assertTrue(_item(self.ORG, MATCH)["active"])
        self.assertFalse(_prefin(self.ORG, MATCH)["active"])

    def test_prefinancing_is_never_scored_off_the_cofinancing_capacity(self):
        # There is no org pre-financing field yet, so both sides are not known and the
        # component must stay out of the denominator — NOT borrow the co-financing value.
        for donor in (REIMBURSE, {"donor_prefinance_required": "yes"},
                      {"donor_prefinance_required": "required"}):
            with self.subTest(donor=donor):
                it = _prefin(self.ORG, donor)
                self.assertFalse(it["active"], donor)
                self.assertIsNone(it["score"], donor)

    def test_prefinancing_scores_once_the_org_records_its_own_capacity(self):
        org = {"org_cofinancing_capacity": "limited", "org_prefinance_capacity": "none"}
        it = _prefin(org, {"donor_prefinance_required": "yes"})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)
        # ...and it reads the PRE-financing field, not the co-financing one.
        org2 = dict(org, org_prefinance_capacity="strong")
        self.assertEqual(_prefin(org2, {"donor_prefinance_required": "yes"})["score"], 1.0)

    def test_a_reimbursing_funder_is_still_reported_as_a_delivery_risk(self):
        # Not scored, but not silently dropped either: it is a real cash-flow constraint.
        it = _prefin(self.ORG, REIMBURSE)
        self.assertFalse(it["active"])
        self.assertIn("reimburses in arrears", it["_detail"])
        self.assertIn("not an eligibility condition", it["_detail"])

    def test_a_donor_with_nothing_stated_gets_no_prefinance_detail(self):
        self.assertIsNone(_prefin(self.ORG, {}).get("_detail"))

    def test_prefinancing_is_never_a_fatal_gate(self):
        self.assertFalse(_prefin(self.ORG, REIMBURSE)["fatal"])

    def test_competitiveness_no_longer_reads_a_payment_modality(self):
        # PREFER-8 scored `donor_prefinance_required` against org_cofinancing_capacity
        # through _COFIN_FLAGS — the same conflation in a second place.
        self.assertNotIn("donor_prefinance_required", CD._COFIN_FLAGS)
        self.assertIn("donor_cost_sharing_match_required", CD._COFIN_FLAGS)
