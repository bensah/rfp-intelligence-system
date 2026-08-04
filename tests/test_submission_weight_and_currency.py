"""Tests for submission weighting and the request currency (core/records.py).

SUBMISSION WEIGHTING — an RFP can be submitted to a donor MORE THAN ONCE. Counting rows
under-reports every submission-derived indicator: an RFP submitted twice and now under review
is TWO applications under review. Total Submitted, Approved, Under Review, Not Approved and
the win rate must all use the same weight or they drift apart (Under Review read 7 when the
truth was 8).

REQUEST CURRENCY — `amount_requested` is what WE asked for; `currency` is the unit the CALL
was advertised in. They are not the same: a Canadian call advertised in CAD can be answered
with a USD budget. Converting the request with the call's currency mis-stated it — a
USD 715,400 request rendered as "$509,530 USD" because it was rated as CAD.

Run:  python -m unittest tests.test_submission_weight_and_currency
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.records import (submission_weight, submission_weights,      # noqa: E402
                          requested_currency)


class SubmissionWeightTests(unittest.TestCase):
    def test_completed_counts_its_submissions(self):
        self.assertEqual(submission_weight({"progress_status": "Completed", "submissions": 2}), 2)

    def test_completed_without_a_count_is_one(self):
        for v in (None, "", "junk"):
            self.assertEqual(
                submission_weight({"progress_status": "Completed", "submissions": v}), 1, repr(v))

    def test_never_submitted_contributes_nothing(self):
        for ps in ("Not Started", "In Progress", "Discontinued", "Missed", "", None):
            self.assertEqual(
                submission_weight({"progress_status": ps, "submissions": 5}), 0, repr(ps))

    def test_a_donor_decision_proves_submission(self):
        # Backdated intake often never sets progress_status, but a donor can only decide on
        # a proposal it received — so the row IS submitted.
        for dd in ("Approved", "Under Review", "Not Approved"):
            self.assertEqual(submission_weight({"donor_decision": dd, "submissions": 3}), 3, dd)

    def test_a_recorded_submission_date_proves_submission(self):
        self.assertEqual(submission_weight({"date_completed": "2026-04-03"}), 1)

    def test_not_submitted_decision_does_not_count(self):
        self.assertEqual(
            submission_weight({"donor_decision": "Not submitted", "submissions": 4}), 0)

    def test_the_reported_case_under_review_7_becomes_8(self):
        import pandas as pd
        df = pd.DataFrame([
            {"progress_status": "Completed", "donor_decision": "Under Review", "submissions": 1},
            {"progress_status": "Completed", "donor_decision": "Under Review", "submissions": 1},
            {"progress_status": "Completed", "donor_decision": "Under Review", "submissions": 1},
            {"progress_status": "Completed", "donor_decision": "Under Review", "submissions": 1},
            {"progress_status": "Completed", "donor_decision": "Under Review", "submissions": 1},
            {"progress_status": "Completed", "donor_decision": "Submitted", "submissions": 1},
            {"progress_status": "Completed", "donor_decision": "Under Review", "submissions": 2},
        ])
        self.assertEqual(len(df), 7)                       # what row-counting reported
        self.assertEqual(int(submission_weights(df).sum()), 8)   # the truth

    def test_empty_frame_is_safe(self):
        import pandas as pd
        self.assertEqual(int(submission_weights(pd.DataFrame()).sum()), 0)


class RequestedCurrencyTests(unittest.TestCase):
    def test_submission_currency_wins_over_the_calls_currency(self):
        # THE REPORTED BUG: call advertised in CAD, budget submitted in USD.
        self.assertEqual(
            requested_currency({"currency": "CAD $", "currency_secured": "USD"}), "USD")

    def test_falls_back_to_the_call_currency_for_legacy_rows(self):
        for v in (None, "", "   "):
            self.assertEqual(
                requested_currency({"currency": "GBP", "currency_secured": v}), "GBP", repr(v))

    def test_missing_everything_is_empty_not_an_error(self):
        self.assertEqual(requested_currency({}), "")

    def test_works_on_an_object_without_get(self):
        class Row:
            currency = "EUR"
            currency_secured = ""
        self.assertEqual(requested_currency(Row()), "EUR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
