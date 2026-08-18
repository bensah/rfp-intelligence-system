"""Removing an RFP from a tenant's pipeline is a rejection, and rejections are kept.

Removal is the most decisive judgement a reviewer makes — more certain than a Decline,
which sits in the pipeline being argued about — and it was the only one the system threw
away. The row vanished, the scorer learned nothing, and the next scan brought the same call
back to be removed again. That loop is behind "this donor's expired calls keep leaking
back in".

Run:  python -m unittest tests.test_human_reject
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import decision_log        # noqa: E402

ROW = {"uid": "AS-260818-1225343",
       "opportunity_title": "Support for Associations in Occitania for Health in the South",
       "funding_agency": "Fondation Pierre Fabre",
       "opportunity_link": "https://example.org/call",
       "alignment_score": 81}


class _Capture:
    """Stands in for the decisions table, keeping what was inserted."""

    def __init__(self):
        self.rows = []

    def table(self, _name):
        return self

    def insert(self, record):
        self.rows.append(record)
        return self

    def execute(self):
        return mock.Mock(data=[])


class LogHumanRejectTests(unittest.TestCase):
    def test_a_removal_is_recorded_as_a_human_reject(self):
        cap = _Capture()
        with mock.patch.object(decision_log, "get_client", return_value=cap):
            self.assertEqual(decision_log.log_human_reject([ROW], by="a@example.org"), 1)
        rec = cap.rows[0]
        self.assertEqual(rec["event_type"], "human_reject")
        self.assertEqual(rec["rfp_uid"], ROW["uid"])
        self.assertEqual(rec["decided_by"], "a@example.org")

    def test_it_is_distinct_from_a_rule_firing(self):
        # A person overruling every rule is not the same signal as a gate rejecting a row;
        # training on them as one event type would flatten the difference.
        cap = _Capture()
        with mock.patch.object(decision_log, "get_client", return_value=cap):
            decision_log.log_human_reject([ROW])
        self.assertNotEqual(cap.rows[0]["event_type"], "system_reject")

    def test_the_evidence_is_captured_not_just_the_id(self):
        # After the delete the row is gone, so whatever is not copied here is lost.
        cap = _Capture()
        with mock.patch.object(decision_log, "get_client", return_value=cap):
            decision_log.log_human_reject([ROW])
        rec = cap.rows[0]
        for field in ("opportunity_title", "funding_agency", "opportunity_link",
                      "alignment_score"):
            self.assertEqual(rec[field], ROW[field], field)

    def test_every_row_of_a_batch_is_recorded(self):
        cap = _Capture()
        rows = [{**ROW, "uid": f"UID-{i}"} for i in range(4)]
        with mock.patch.object(decision_log, "get_client", return_value=cap):
            self.assertEqual(decision_log.log_human_reject(rows), 4)

    def test_a_logging_failure_never_raises(self):
        # The reviewer asked for the row to go. Losing the learning signal must not block
        # that, so the failure is swallowed and reported as a count.
        with mock.patch.object(decision_log, "get_client",
                               side_effect=RuntimeError("db down")):
            self.assertEqual(decision_log.log_human_reject([ROW]), 0)

    def test_no_rows_is_not_an_error(self):
        self.assertEqual(decision_log.log_human_reject([]), 0)
        self.assertEqual(decision_log.log_human_reject(None), 0)


class RecordsScreenTests(unittest.TestCase):
    """The button says what it does, and the order of operations is load-bearing."""

    def setUp(self):
        with open(os.path.join(_ROOT, "views", "rfp_records.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def test_a_client_tenant_sees_reject_and_a_developer_sees_delete(self):
        self.assertIn('_REMOVE_VERB = "Delete" if _IS_DEVELOPER_VIEW else "Reject"',
                      self.src)
        self.assertIn("is_developer_admin(user)", self.src)

    def test_the_role_flags_are_set_before_the_buttons_use_them(self):
        # They were briefly defined 250 lines after first use, which is a NameError the
        # moment anyone opens the page.
        self.assertLess(self.src.index("_REMOVE_VERB ="),
                        self.src.index("{_REMOVE_ICON} {_REMOVE_VERB}"))

    def test_the_rejection_is_logged_before_the_row_is_deleted(self):
        # The row IS the evidence; after the delete there is nothing left to record.
        block = self.src.split("Confirm {_verb}", 1)[1]
        self.assertLess(block.index("log_human_reject"),
                        block.index('table("rfp_submissions")'))

    def test_a_rejected_call_is_tombstoned_so_it_does_not_return(self):
        self.assertIn('record_one(_r, reason="human_reject")', self.src)

    def test_a_rejection_is_not_described_as_an_irreversible_delete(self):
        # "There is no undo" is true of a delete and misleading about a kept rejection.
        reject_copy = self.src.split("_reject = not _IS_DEVELOPER_VIEW", 1)[1][:1200]
        self.assertIn("records it as", reject_copy)
        self.assertIn("There is no undo", reject_copy)      # still shown to developers


if __name__ == "__main__":
    unittest.main(verbosity=2)
