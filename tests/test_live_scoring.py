"""Regression tests for core.live_scoring — the per-row memo that stops the record tables
re-scoring every row on navigation / pagination / delete.

Guards the performance contract:
  * a rerun with the SAME rows scores NOTHING (memo hit);
  * only a NEW or EDITED row is (re)scored;
  * deleting a row scores nothing (survivors stay cached) and evicts the gone row;
  * a changed profile signature re-scores every row.

assess_row is monkeypatched to a counter so we assert exactly how many rows were scored.

Run:  python -m unittest tests.test_live_scoring
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import live_scoring as LS      # noqa: E402


class MemoScoringTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        # Deterministic fake scorer that records which uid it scored.
        LS.assess_row = lambda row: (self.calls.append(row.get("uid"))
                                     or {"alignment_score": 50.0,
                                         "auto_recommendation": "Park"})

    def _rows(self):
        return [
            {"uid": "A", "opportunity_title": "Alpha", "amount_requested": 100},
            {"uid": "B", "opportunity_title": "Beta", "amount_requested": 200},
        ]

    def test_first_pass_scores_all(self):
        memo = {}
        scores, n = LS.scores_for(self._rows(), "sig1", memo)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(self.calls), ["A", "B"])
        self.assertEqual(scores["A"]["alignment_score"], 50.0)

    def test_second_pass_scores_nothing(self):
        memo = {}
        LS.scores_for(self._rows(), "sig1", memo)
        self.calls.clear()
        _, n = LS.scores_for(self._rows(), "sig1", memo)   # identical rerun
        self.assertEqual(n, 0)                             # pure memo hit — no re-score
        self.assertEqual(self.calls, [])

    def test_reorder_is_a_hit(self):
        memo = {}
        LS.scores_for(self._rows(), "sig1", memo)
        self.calls.clear()
        _, n = LS.scores_for(list(reversed(self._rows())), "sig1", memo)
        self.assertEqual(n, 0)                             # order-independent

    def test_only_edited_row_rescored(self):
        memo = {}
        rows = self._rows()
        LS.scores_for(rows, "sig1", memo)
        self.calls.clear()
        rows[1] = {**rows[1], "amount_requested": 999}     # edit B
        _, n = LS.scores_for(rows, "sig1", memo)
        self.assertEqual(n, 1)
        self.assertEqual(self.calls, ["B"])                # A untouched

    def test_delete_scores_nothing_and_evicts(self):
        memo = {}
        LS.scores_for(self._rows(), "sig1", memo)
        self.assertEqual(len(memo), 2)
        self.calls.clear()
        _, n = LS.scores_for([self._rows()[0]], "sig1", memo)   # B deleted
        self.assertEqual(n, 0)                             # survivor A stays cached
        self.assertEqual(len(memo), 1)                     # B evicted → bounded

    def test_profile_change_rescopes_all(self):
        memo = {}
        LS.scores_for(self._rows(), "sig1", memo)
        self.calls.clear()
        _, n = LS.scores_for(self._rows(), "sig2", memo)   # profile edited
        self.assertEqual(n, 2)
        self.assertEqual(len(memo), 2)                     # old-sig entries evicted

    def test_rows_without_uid_skipped(self):
        memo = {}
        scores, n = LS.scores_for([{"opportunity_title": "no uid"}], "sig1", memo)
        self.assertEqual(n, 0)
        self.assertEqual(scores, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
