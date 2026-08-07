"""MUST-5 co-financing: an unrecorded capacity is not a partial (action #9).

WHAT THE DIAGNOSIS FOUND. The reported "◐ Co-financing / pre-finance capacity" on the
Grand Challenges row was CORRECT, and for a reason worth writing down: the CALL says
nothing about co-financing, but the DONOR record does —
`donor_prefinance_required = 'reimbursement_only'`, i.e. the funder reimburses rather
than advancing funds, so the org must carry the cost first. Donor intel is the designed
fallback when the call is silent (the call-first precedence rule), and the org's own
`org_cofinancing_capacity` is 'limited', so 0.5 is the honest answer. No bug there.

THE REAL DEFECT was next to it: the score fell through a trailing `else 0.5`, so a
BLANK org_cofinancing_capacity — nothing recorded — also rendered as ◐ "partial". That
made "we can partly co-finance" and "nobody has told us" look identical on the card.
Both sides must now be known.

NOT stated by call or donor → the component stays inactive. When NOTHING across MUST-5
is imposed, the criterion is carried by the single `compliance_all_clear` component,
which scores a full pass — that is where "assume it is not required, score 1" is
delivered, rather than by making this component permanently active with no requirement
behind it.

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

# The real trigger on the reported row: the funder reimburses, so we pre-finance.
REIMBURSE = {"donor_prefinance_required": "reimbursement_only"}
MATCH = {"donor_cost_sharing_match_required": "yes"}


def _item(org, donor, rfp=None):
    return {i["key"]: i for i in
            CD.compliance_factors(org, rfp or {}, donor, {})}["cofinance"]


class UnknownCapacityTests(unittest.TestCase):
    def test_an_unrecorded_capacity_is_not_a_partial(self):
        for blank in ({}, {"org_cofinancing_capacity": ""},
                      {"org_cofinancing_capacity": None},
                      {"org_cofinancing_capacity": "   "}):
            it = _item(blank, REIMBURSE)
            self.assertFalse(it["active"], repr(blank))
            self.assertIsNone(it["score"], repr(blank))
            self.assertEqual(CD.component_mark(it)[0], "?", repr(blank))

    def test_an_unrecognised_capacity_value_is_also_not_a_partial(self):
        it = _item({"org_cofinancing_capacity": "somewhat"}, REIMBURSE)
        self.assertFalse(it["active"])

    def test_it_no_longer_lands_in_the_denominator_unmeasured(self):
        act = {i["key"] for i in CD.compliance_factors({}, {}, REIMBURSE, {})
               if i["active"] and i["score"] is not None}
        self.assertNotIn("cofinance", act)


class RecordedCapacityTests(unittest.TestCase):
    """strong/moderate → 1 · limited → 0.5 · none → 0, exactly as specified."""

    def test_the_whole_vocabulary_scores_as_specified(self):
        want = {"strong": 1.0, "moderate": 1.0, "limited": 0.5, "none": 0.0}
        for level in COFINANCING_LEVELS:
            it = _item({"org_cofinancing_capacity": level}, REIMBURSE)
            self.assertTrue(it["active"], level)
            self.assertEqual(it["score"], want[level], level)

    def test_the_reported_row_still_reads_partial(self):
        # Donor requires money up front; our capacity is 'limited'. ◐ is correct.
        it = _item({"org_cofinancing_capacity": "limited"}, REIMBURSE)
        self.assertEqual(it["score"], 0.5)
        self.assertEqual(CD.component_mark(it)[0], "◐")
        self.assertIn("requires money up front", it["_detail"])

    def test_no_capacity_at_all_is_a_real_zero(self):
        it = _item({"org_cofinancing_capacity": "none"}, MATCH)
        self.assertEqual(it["score"], 0.0)
        self.assertEqual(CD.component_mark(it)[0], "✗")


class ActivationTests(unittest.TestCase):
    ORG = {"org_cofinancing_capacity": "limited"}

    def test_silence_on_both_sides_leaves_it_inactive(self):
        self.assertFalse(_item(self.ORG, {})["active"])

    def test_a_donor_requirement_activates_it(self):
        for donor in (REIMBURSE, MATCH,
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
