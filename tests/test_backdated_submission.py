"""Regression tests for backdated-intake handling (PREFER-9 bid effort).

Proposals entered via Excel or the in-app form MONTHS after they went to the donor arrive
with a deadline already in the past. The time component ("Time before the deadline") must
not read them as "not enough time" — they were submitted on time.

`_is_completed` is the counter-validation: it now accepts ANY durable proof of submission
(Progress = Completed, a real donor decision, or a recorded submission date), matching the
app-wide SUBMITTED rule in views/report.py::_submitted_mask. `needs_submission_check` is the
intake guard that flags a past-deadline row with no such proof.

Run:  python -m unittest tests.test_backdated_submission
"""
import os
import sys
import unittest
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.criteria_derive import (                    # noqa: E402
    _is_completed, needs_submission_check, _bid_effort_factors,
)

_PAST = (date.today() - timedelta(days=60)).isoformat()
_FUTURE = (date.today() + timedelta(days=30)).isoformat()


def _time_factor(rfp):
    return next(f for f in _bid_effort_factors(rfp, {}) if f["key"] == "bid_time")


class IsCompletedTests(unittest.TestCase):
    def test_progress_completed_counts(self):
        self.assertTrue(_is_completed({"progress_status": "Completed"}))

    def test_donor_decision_counts_as_submitted(self):
        # A donor can only decide on a proposal it RECEIVED — this is the case that was
        # missing: an under-review RFP whose progress_status was never set to Completed.
        for dd in ("Under Review", "approved", "Not Approved"):
            self.assertTrue(_is_completed({"donor_decision": dd}), dd)

    def test_recorded_submission_date_counts(self):
        self.assertTrue(_is_completed({"date_completed": "2026-04-03"}))

    def test_not_submitted_is_false(self):
        self.assertFalse(_is_completed({"progress_status": "Not Started",
                                        "donor_decision": "Not submitted"}))
        self.assertFalse(_is_completed({}))

    def test_in_progress_is_not_submitted(self):
        self.assertFalse(_is_completed({"progress_status": "In Progress"}))


class BidEffortCounterValidationTests(unittest.TestCase):
    def test_past_deadline_but_submitted_scores_full(self):
        f = _time_factor({"call_submission_deadline": _PAST, "donor_decision": "Under Review"})
        self.assertEqual(f["score"], 1.0)
        self.assertIn("already completed", f["name"].lower())

    def test_past_deadline_not_submitted_still_fails(self):
        # A genuinely missed opportunity must STILL score 0 — we don't paper over those.
        f = _time_factor({"call_submission_deadline": _PAST,
                          "progress_status": "Not Started"})
        self.assertEqual(f["score"], 0.0)

    def test_future_deadline_unaffected(self):
        f = _time_factor({"call_submission_deadline": _FUTURE})
        self.assertEqual(f["score"], 1.0)
        self.assertIn("time before the deadline", f["name"].lower())


class IntakeGuardTests(unittest.TestCase):
    def test_flags_past_deadline_with_no_submission_proof(self):
        self.assertTrue(needs_submission_check(
            {"call_submission_deadline": _PAST, "progress_status": "Not Started"}))

    def test_silent_when_submitted(self):
        self.assertFalse(needs_submission_check(
            {"call_submission_deadline": _PAST, "donor_decision": "Under Review"}))
        self.assertFalse(needs_submission_check(
            {"call_submission_deadline": _PAST, "progress_status": "Completed"}))

    def test_silent_for_future_or_missing_deadline(self):
        self.assertFalse(needs_submission_check({"call_submission_deadline": _FUTURE}))
        self.assertFalse(needs_submission_check({"call_submission_deadline": None}))
        self.assertFalse(needs_submission_check({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
