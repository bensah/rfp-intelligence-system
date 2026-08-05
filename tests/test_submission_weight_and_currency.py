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

    def test_only_progress_completed_opens_the_gate(self):
        # A donor decision (or a recorded date) does NOT qualify a row on its own. An earlier
        # version accepted them, which would have resurrected a phantom 1 on exactly the rows
        # migration 089 reset to 0 — e.g. donor_decision set while Progress is "Not Started".
        for dd in ("Approved", "Under Review", "Not Approved"):
            self.assertEqual(
                submission_weight({"progress_status": "Not Started",
                                   "donor_decision": dd, "submissions": 0}), 0, dd)
        self.assertEqual(submission_weight({"date_completed": "2026-04-03"}), 0)

    def test_bad_data_cannot_leak_through_the_gate(self):
        # submissions > 0 on a non-Completed row is corrupt; the multiplier still zeroes it.
        self.assertEqual(
            submission_weight({"progress_status": "Not Started", "submissions": 5}), 0)

    def test_a_completed_row_counts_at_least_once(self):
        self.assertEqual(
            submission_weight({"progress_status": "Completed", "submissions": 0}), 1)

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


class ExcelSubmissionsImportTests(unittest.TestCase):
    """`_int(get("Submissions")) or 1` corrupted the import: Python treats 0 as falsy, so
    every Excel 0 (an RFP never submitted) became 1 — putting a phantom submission on 242
    rows. The sheet's value is authoritative and must survive verbatim, 0 included."""

    def setUp(self):
        import sys as _s
        _sp = os.path.join(_ROOT, "scripts")
        if _sp not in _s.path:
            _s.path.insert(0, _sp)
        from migrate_excel import _submissions_value
        self.f = _submissions_value

    def test_zero_survives_the_import(self):
        self.assertEqual(self.f(0, "Not Started"), 0)
        self.assertEqual(self.f("0", "Discontinued"), 0)

    def test_counts_are_preserved_verbatim(self):
        self.assertEqual(self.f(1, "Completed"), 1)
        self.assertEqual(self.f(2, "Completed"), 2)      # the twice-submitted RFP

    def test_blank_falls_back_to_the_progress_status(self):
        self.assertEqual(self.f(None, "Completed"), 1)
        self.assertEqual(self.f("", "Not Started"), 0)

    def test_never_negative(self):
        self.assertEqual(self.f(-3, "Completed"), 0)
