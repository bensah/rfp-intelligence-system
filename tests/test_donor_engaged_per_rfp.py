"""PREFER-7 "Donor already engaged" is a human answer, per opportunity (action #10).

The system cannot know in real time whether anyone has approached this funder about THIS
call — a meeting, a concept note or an EOI leaves no trace a crawler can see. It was
INFERRED from `org_engaged_donors`, a per-DONOR list that answers a different question
("have we ever engaged this funder?") and is empty in practice, so the tier scored ✗ on
every row.

It is now a reviewer-set field on the opportunity (migration 091):
    yes     — we have engaged this funder about this opportunity
    partial — contact made via a third party on our behalf (the owner's case)
    no      — no contact about this opportunity
    unset   — EXCLUDED from PREFER-7 rather than scored 0; the system may not guess.

The per-donor list is kept as a fallback for rows nobody has answered yet, so nothing
that worked before stops working.

Run:  python -m unittest tests.test_donor_engaged_per_rfp
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

BMGF = {"donor": "Bill & Melinda Gates Foundation",
        "canonical_key": "bill_melinda_gates_foundation",
        "donor_aliases": "Gates Foundation; BMGF; Grand Challenges"}
RFP = {"funding_agency": "Grand Challenges"}
ORG = {}                       # no relationship data of any kind


def _eng(org, rfp, donor=BMGF):
    return {i["key"]: i for i in CD._relationship_factors(org, rfp, donor)}["rel_engaged"]


class HumanAnswerTests(unittest.TestCase):
    def test_yes_is_a_full_tier(self):
        it = _eng(ORG, {**RFP, "donor_engaged": "yes"})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 1.0)
        self.assertEqual(CD.component_mark(it)[0], "✓")

    def test_partial_is_the_third_party_case(self):
        it = _eng(ORG, {**RFP, "donor_engaged": "partial"})
        self.assertEqual(it["score"], 0.5)
        self.assertEqual(CD.component_mark(it)[0], "◐")
        self.assertIn("third party", it["_detail"])

    def test_no_is_a_measured_zero(self):
        it = _eng(ORG, {**RFP, "donor_engaged": "no"})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)
        self.assertEqual(CD.component_mark(it)[0], "✗")

    def test_unanswered_is_excluded_not_scored(self):
        for blank in (None, "", "   ", "maybe"):
            it = _eng(ORG, {**RFP, "donor_engaged": blank})
            self.assertFalse(it["active"], repr(blank))
            self.assertIsNone(it.get("score"), repr(blank))
            self.assertEqual(CD.component_mark(it)[0], "?", repr(blank))

    def test_the_answer_is_case_insensitive(self):
        self.assertEqual(_eng(ORG, {**RFP, "donor_engaged": "Yes"})["score"], 1.0)
        self.assertEqual(_eng(ORG, {**RFP, "donor_engaged": " PARTIAL "})["score"], 0.5)

    def test_the_row_says_where_the_answer_came_from(self):
        self.assertIn("set by reviewer", _eng(ORG, {**RFP, "donor_engaged": "yes"})["_detail"])


class OrgListFallbackTests(unittest.TestCase):
    ENGAGED_ORG = {"org_engaged_donors": ["Bill & Melinda Gates Foundation"]}

    def test_the_org_list_still_answers_an_unanswered_row(self):
        it = _eng(self.ENGAGED_ORG, RFP)
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 1.0)
        self.assertIn("org profile", it["_detail"])

    def test_the_reviewer_overrides_the_org_list(self):
        it = _eng(self.ENGAGED_ORG, {**RFP, "donor_engaged": "no"})
        self.assertEqual(it["score"], 0.0)
        self.assertIn("set by reviewer", it["_detail"])

    def test_the_org_list_is_still_matched_canonically(self):
        # "Grand Challenges" must resolve to the Gates Foundation.
        self.assertEqual(_eng(self.ENGAGED_ORG, RFP)["score"], 1.0)


class LabelAgreesWithTheComponentTests(unittest.TestCase):
    def test_an_engaged_answer_lifts_the_label_to_some_contact(self):
        self.assertEqual(
            CD.derive_funder_relationship(ORG, {**RFP, "donor_engaged": "yes"}, BMGF),
            "Some contact")

    def test_partial_also_counts_as_contact(self):
        self.assertEqual(
            CD.derive_funder_relationship(ORG, {**RFP, "donor_engaged": "partial"}, BMGF),
            "Some contact")

    def test_an_explicit_no_is_relationship_data_so_the_label_is_none_not_unsure(self):
        # Answering "no" IS information — the criterion is no longer undetermined.
        self.assertEqual(
            CD.derive_funder_relationship(ORG, {**RFP, "donor_engaged": "no"}, BMGF),
            "None")

    def test_no_relationship_data_at_all_is_still_not_sure(self):
        self.assertIsNone(CD.derive_funder_relationship(ORG, RFP, BMGF))

    def test_a_grantee_still_outranks_everything(self):
        org = {"org_funder_history": ["Bill & Melinda Gates Foundation"]}
        self.assertEqual(
            CD.derive_funder_relationship(org, {**RFP, "donor_engaged": "no"}, BMGF),
            "Current/past grantee")

    def test_the_component_and_the_label_never_disagree(self):
        for ans in ("yes", "partial", "no", None):
            rfp = {**RFP, "donor_engaged": ans}
            it = _eng(ORG, rfp)
            label = CD.derive_funder_relationship(ORG, rfp, BMGF)
            engaged_counts = (it["score"] or 0) > 0 if it["active"] else False
            self.assertEqual(engaged_counts, label == "Some contact", repr(ans))


if __name__ == "__main__":
    unittest.main(verbosity=2)
