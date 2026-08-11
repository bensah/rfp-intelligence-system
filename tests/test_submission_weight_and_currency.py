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

from core import records                                            # noqa: E402
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


class TheLedgerFlagsItsOwnInconsistenciesTests(unittest.TestCase):
    """Total Submitted read 12 above a list of 13 rows, and nothing in the app said so — the
    discrepancy was found by a human comparing two numbers on a dashboard.

    The owner's rule stands: only Progress = Completed opens the gate, because a bare
    donor_decision once resurrected counts migration 089 had deliberately reset. That makes the
    DATA the thing to keep honest, so the mismatch has to be reportable rather than waiting to
    be noticed. This reports; it never writes."""

    def test_a_donor_decision_over_an_unfinished_progress_is_flagged(self):
        bad = records.submission_inconsistencies([
            {"uid": "A", "progress_status": "Not Started", "donor_decision": "Under Review"}])
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["issue"], "decided_not_completed")
        self.assertEqual(bad[0]["uid"], "A")

    def test_every_decision_that_proves_receipt_is_flagged(self):
        for dd in ("Approved", "Under Review", "Not Approved"):
            with self.subTest(decision=dd):
                self.assertEqual(len(records.submission_inconsistencies(
                    [{"uid": "A", "progress_status": "In Progress",
                      "donor_decision": dd}])), 1)

    def test_the_reverse_is_flagged_too(self):
        # Marked Completed while the donor decision still says nothing was sent. One of the two
        # is wrong, and this one COUNTS today, so it may be inflating the total.
        bad = records.submission_inconsistencies([
            {"uid": "B", "progress_status": "Completed",
             "donor_decision": "Not submitted"}])
        self.assertEqual([b["issue"] for b in bad], ["completed_not_sent"])

    def test_a_consistent_ledger_reports_nothing(self):
        self.assertEqual(records.submission_inconsistencies([
            {"uid": "A", "progress_status": "Completed", "donor_decision": "Under Review"},
            {"uid": "B", "progress_status": "Completed", "donor_decision": "Approved"},
            {"uid": "C", "progress_status": "Not Started", "donor_decision": "Not submitted"},
            {"uid": "D", "progress_status": "Discontinued", "donor_decision": ""},
            {"uid": "E", "progress_status": "In Progress", "donor_decision": None},
        ]), [])

    def test_it_reports_and_never_writes(self):
        # A validation check that mutates is a migration wearing a disguise.
        rows = [{"uid": "A", "progress_status": "Not Started",
                 "donor_decision": "Approved", "submissions": 0}]
        before = [dict(r) for r in rows]
        records.submission_inconsistencies(rows)
        self.assertEqual(rows, before)

    def test_the_flagged_row_carries_what_a_human_needs_to_fix_it(self):
        bad = records.submission_inconsistencies([
            {"uid": "A", "progress_status": "Not Started", "donor_decision": "Approved",
             "submissions": 2}])[0]
        for key in ("uid", "donor_decision", "progress_status", "submissions", "issue"):
            self.assertIn(key, bad)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(records.submission_inconsistencies([]), [])
        self.assertEqual(records.submission_inconsistencies(None), [])


class EmptyMeetingLogRowsAreNotNotesTests(unittest.TestCase):
    """The Excel import carried trailing sheet rows holding no note at all. They rendered as a
    line of em dashes with a red "Not Resolved" badge — which reads as an outstanding action
    nobody has dealt with, in a table whose whole purpose is tracking outstanding actions.

    Resolved/unresolved is deliberately not part of the test: a blank row's status is whatever
    the import defaulted to, so judging on it would keep exactly the rows being removed."""

    def test_a_real_note_is_kept(self):
        self.assertTrue(records.note_has_content(
            {"remarks": "Needs a partner institution", "actions": "Stop tracking",
             "owner": "A Person"}))

    def test_a_row_with_nothing_in_it_is_dropped(self):
        self.assertFalse(records.note_has_content(
            {"remarks": None, "actions": None, "owner": None, "deadline": None,
             "rfp_uid": None, "donor_title": None}))

    def test_the_spreadsheet_placeholders_count_as_empty(self):
        # A CSV round-trip turns blanks into these, and they are not content.
        self.assertFalse(records.note_has_content(
            {"remarks": "nan", "actions": "—", "owner": "  ", "rfp_uid": "NaN",
             "donor_title": "none", "deadline": "NaT"}))

    def test_ANY_single_field_is_enough_to_keep_a_row(self):
        # A note with only an owner, or only a linked RFP, is still somebody's record.
        for field in records.NOTE_CONTENT_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(records.note_has_content({field: "something"}))

    def test_the_resolved_flag_is_not_what_decides(self):
        # Both directions: a resolved blank is still dropped, an unresolved real note is kept.
        self.assertFalse(records.note_has_content({"is_resolved": True}))
        self.assertTrue(records.note_has_content(
            {"is_resolved": False, "remarks": "Real issue"}))

    def test_the_frame_helper_reports_how_many_it_dropped(self):
        import pandas as pd
        df = pd.DataFrame([
            {"remarks": "Real", "actions": "Do it", "owner": "A"},
            {"remarks": None, "actions": None, "owner": None},
            {"remarks": "nan", "actions": "—", "owner": ""},
            {"remarks": None, "actions": None, "owner": "B"},
        ])
        kept, dropped = records.drop_empty_notes(df)
        self.assertEqual((len(kept), dropped), (2, 2))

    def test_an_empty_frame_is_safe(self):
        import pandas as pd
        kept, dropped = records.drop_empty_notes(pd.DataFrame())
        self.assertEqual(dropped, 0)
        self.assertEqual(records.drop_empty_notes(None), (None, 0))


class OneValidationRuleNotTwoTests(unittest.TestCase):
    """The Summary page had its own copy of the donor_decision <-> progress_status check and
    `core.records` grew a second — two implementations of one rule, which is how they drift.
    Consolidating had to ADOPT the page's semantics, not just delete one of them: the page's
    version was broader in two ways the core helper had missed."""

    def test_a_BLANK_decision_means_not_submitted(self):
        # The page assumed this; losing it would have quietly narrowed the rule.
        for dd in ("", None, "   "):
            with self.subTest(decision=dd):
                bad = records.submission_inconsistencies(
                    [{"uid": "A", "donor_decision": dd, "progress_status": "Completed"}])
                self.assertEqual([b["issue"] for b in bad], ["completed_not_sent"])

    def test_ANY_progress_outside_the_pre_submit_set_contradicts_not_submitted(self):
        # Not only "Completed": a value nobody recognises is also a contradiction, and the
        # page's version caught it.
        bad = records.submission_inconsistencies(
            [{"uid": "A", "donor_decision": "Not submitted",
              "progress_status": "Weird Value"}])
        self.assertEqual([b["issue"] for b in bad], ["completed_not_sent"])

    def test_the_pre_submit_states_are_all_consistent_with_not_submitted(self):
        for ps in ("", "Not Started", "In Progress", "Discontinued", "Missed", "missing"):
            with self.subTest(progress=ps):
                self.assertEqual(records.submission_inconsistencies(
                    [{"uid": "A", "donor_decision": "Not submitted",
                      "progress_status": ps}]), [])

    def test_the_vocabularies_have_one_home(self):
        self.assertEqual(records.POST_SUBMIT_PROGRESS, frozenset({"completed"}))
        self.assertIn("missing", records.PRE_SUBMIT_PROGRESS)   # legacy spelling tolerated

    def test_the_summary_page_no_longer_defines_its_own_copy(self):
        import os
        path = os.path.join(_ROOT, "views", "summary_rfp.py")
        with open(path, encoding="utf-8") as fh:
            page = fh.read()
        for name in ("SUBMITTED_DECISIONS =", "PRE_SUBMIT_PROGRESS =",
                     "POST_SUBMIT_PROGRESS ="):
            self.assertNotIn(name, page, f"{name} is defined twice again")
        self.assertIn("submission_inconsistencies", page)
