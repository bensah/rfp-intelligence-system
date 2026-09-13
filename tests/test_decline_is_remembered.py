"""A human Decline (or delete) must be remembered by the SCAN, not just by the model.

THE REPORTED CASE. "We have declined this several times and it keeps coming back."

WHAT WAS ACTUALLY HAPPENING. There were two records of a decision and only one was being
written for the purpose the user had in mind:

  * `decision_log.log_decision` → `scan_decisions`. This is the ML TRAINING LABEL. Nothing
    reads `scan_decisions` at gate time — not `is_eligible`, not the deduplicator, not the
    scan. 65 human decisions sat in that table having no effect whatsoever on what the next
    scan ingested.
  * `seen_ledger.record` → `rfp_seen`. This is the tombstone the SCAN actually consults
    (`scan_pipeline` loads it via `fetch_all()` and feeds it to `find_duplicates`). It was
    called at INGEST and nowhere else.

So the system recorded "we have seen this" but never "we looked at this and said no". While
the declined row still existed, the live deduplicator covered for the gap. The moment it was
deleted — the obvious way to make an unwanted call go away — the only remaining evidence
went with it, and the next scan re-ingested the call. `views/rfp_editor.py` deleted with a
bare `.delete()`, writing no tombstone at all.

Fixed by writing the tombstone at the two moments a human closes a call out: on a Decline,
and on a delete. A Park or a Proceed is live work and must NOT be tombstoned.

Run:  python -m unittest tests.test_decline_is_remembered
"""
import io
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import seen_ledger as SL                                    # noqa: E402

_ROW = {"uid": "AS-260911-1110301", "opportunity_title": "A call",
        "opportunity_link": "https://example.org/call", "funding_agency": "A funder",
        "call_submission_deadline": None, "call_award_value": None}


class ADeclineIsTombstonedTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._p = mock.patch.object(
            SL, "record", side_effect=lambda rows, reason="ingested":
                self.calls.append((list(rows), reason)) or len(list(rows)))
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_a_decline_records_a_tombstone(self):
        self.assertTrue(SL.record_decision(_ROW, "Decline"))
        self.assertEqual(len(self.calls), 1)
        rows, reason = self.calls[0]
        self.assertEqual(rows[0]["uid"], _ROW["uid"])
        self.assertEqual(reason, "human_decline")

    def test_case_and_spacing_do_not_matter(self):
        for spelling in ("decline", "  DECLINE ", "Declined", "Rejected", "Not Approved"):
            self.calls.clear()
            self.assertTrue(SL.record_decision(_ROW, spelling), spelling)

    def test_park_and_proceed_are_not_tombstoned(self):
        # Live work. Tombstoning these would delete the pipeline from under the reviewer.
        for decision in ("Park", "Proceed", "", None):
            self.calls.clear()
            self.assertFalse(SL.record_decision(_ROW, decision), repr(decision))
            self.assertEqual(self.calls, [])

    def test_a_human_close_passes_its_reason_through(self):
        SL.record_human_close(_ROW, reason="human_reject")
        self.assertEqual(self.calls[0][1], "human_reject")


class TheCallSitesAreWiredTests(unittest.TestCase):
    """Wiring is the whole point — `record_decision` existing but uncalled is the bug."""

    def _src(self, *parts):
        with io.open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_both_decision_paths_tombstone_a_decline(self):
        # Records and Review are two separate decision screens; a fix to one only is the
        # kind of half-fix that makes the bug look intermittent.
        for page in ("review_rfp.py", "rfp_editor.py"):
            src = self._src("views", page)
            self.assertIn("seen_ledger.record_decision", src, page)
            self.assertIn("decision_log.log_decision", src,
                          f"{page}: the ML label must still be written too")

    def test_delete_tombstones_before_it_deletes(self):
        src = self._src("views", "rfp_editor.py")
        tomb = src.index("seen_ledger.record_human_close")
        delete = src.index('sb.table("rfp_submissions").delete()')
        self.assertLess(tomb, delete,
                        "a failed delete must not leave an un-tombstoned row behind")

    def test_the_scan_still_reads_the_ledger(self):
        # The tombstone is only worth writing because this is what consults it.
        src = self._src("core", "scan_pipeline.py")
        self.assertIn("seen_ledger.fetch_all()", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
