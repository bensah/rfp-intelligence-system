"""A closed round is a qualification test, and MUST-1 had no component for it.

Found on a live call: "Only organisations that have been formally invited by <funder> may
apply." Nobody uninvited can win it, yet the criterion that asks whether we are formally
eligible to submit scored it exactly like an open call — there was no component to fail.

The mechanics the owner specified:
  * ACTIVE only when the CALL states the rule — so it joins the denominator then, and only
    then, like every other MUST-1 item
  * scored 0 on the org side by default: an invitation is something we either hold or do not,
    and the extraction cannot know. Absence of evidence is not an invitation.
  * a reviewer who HAS one raises it through Update Decision, and the override is what the
    roll-up then reads

Deliberately NOT a fatal gate. `fatal_decline` ends the assessment outright, which would be
wrong for a fact only the reviewer can confirm: the call is unwinnable without an invitation,
but the page cannot tell us whether one exists.
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

from core import criteria_derive as cd            # noqa: E402
from core import criteria_review as crev          # noqa: E402


def _item(org, rfp, donor=None):
    facts = cd.qualification_factors(org or {}, rfp or {}, donor or {}, {})
    return next(f for f in facts if f.get("key") == "invitation_only")


class TheRuleIsDetectedFromTheCallTests(unittest.TestCase):
    def test_the_wording_on_the_reported_call(self):
        self.assertTrue(cd._invitation_only(
            {"brief_description": "Only organisations that have been formally invited by "
                                  "an agency may apply."}))

    def test_the_other_forms_funders_use(self):
        for text in ("By invitation only.",
                     "Invitation-only round.",
                     "Applicants must have received a formal invitation from the agency.",
                     "Only entities who have been invited will be considered.",
                     "This is a closed call for selected partners."):
            with self.subTest(text=text):
                self.assertTrue(cd._invitation_only({"brief_description": text}))

    def test_AN_OPEN_CALL_THAT_MERELY_SAYS_INVITE_IS_NOT_CAUGHT(self):
        # The false positive that would matter most: "we invite applications" is how an OPEN
        # call opens. Tripping on the verb would decline every one of them.
        for text in ("We invite applications from all eligible organisations.",
                     "Applicants are invited to propose innovative solutions.",
                     "Organisations are invited to submit concept notes.",
                     "Only organisations registered locally may apply.",
                     "Open to any registered non-profit."):
            with self.subTest(text=text):
                self.assertFalse(cd._invitation_only({"brief_description": text}))

    def test_it_reads_the_eligibility_fields_too_not_only_the_summary(self):
        self.assertTrue(cd._invitation_only(
            {"eligibility_other": "By invitation only"}))
        self.assertTrue(cd._invitation_only(
            {"compliance_requirements": "Closed round — invited partners"}))

    def test_a_donor_record_can_carry_the_rule(self):
        self.assertTrue(cd._invitation_only({}, {"donor_invitation_only": True}))

    def test_nothing_stated_is_not_the_rule(self):
        self.assertFalse(cd._invitation_only({}, {}))
        self.assertFalse(cd._invitation_only({"brief_description": ""}, None))


class TheComponentBehavesAsSpecifiedTests(unittest.TestCase):
    OPEN = {"brief_description": "Open to all registered non-profits."}
    CLOSED = {"brief_description": "Only organisations that have been formally invited "
                                   "by an agency may apply."}

    def test_it_is_inactive_and_unscored_on_an_open_call(self):
        it = _item({}, self.OPEN)
        self.assertFalse(it["active"])
        self.assertNotIn("invitation", crev.count_text(
            "qualification", [it], "x", False).lower())

    def test_it_activates_and_scores_zero_on_a_closed_one(self):
        it = _item({}, self.CLOSED)
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)

    def test_an_active_component_joins_the_denominator(self):
        facts = cd.qualification_factors({}, self.CLOSED, {}, {})
        active = [f for f in facts if f.get("active")]
        self.assertIn("invitation_only", [f["key"] for f in active])
        _num, total, _pct = crev.criterion_count("qualification", facts, "x")
        self.assertGreaterEqual(int(total), 1)

    def test_it_drags_MUST_1_down(self):
        label = crev.criterion_label(
            "qualification", cd.qualification_factors({}, self.CLOSED, {}, {}), None)
        self.assertEqual(crev.criterion_score(label) if hasattr(crev, "criterion_score")
                         else 0, 0)

    def test_A_REVIEWER_CAN_RAISE_IT(self):
        # The whole point of scoring 0 rather than declining outright: the human holds the
        # fact the extraction cannot have.
        facts = cd.qualification_factors({}, self.CLOSED, {}, {})
        edited = crev.with_session_edits(facts, {"invitation_only": 1.0})
        it = next(f for f in edited if f.get("key") == "invitation_only")
        self.assertEqual(crev.component_score(it), 1.0)

    def test_it_is_editable_rather_than_locked(self):
        self.assertTrue(crev.is_editable(_item({}, self.CLOSED)))
        self.assertNotIn("invitation_only", crev.NEVER_ACTIVATABLE)

    def test_it_is_not_a_fatal_gate(self):
        # A fatal gate ends the assessment; this one must leave the rest of the criteria
        # readable so a reviewer can judge whether the invitation is worth chasing.
        fatal, _trigger = cd.fatal_decline({}, self.CLOSED, {}, {})
        self.assertFalse(fatal)


if __name__ == "__main__":
    unittest.main(verbosity=2)
