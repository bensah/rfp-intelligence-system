"""Regression tests for EU Funding & Tenders (SEDIA) two-stage handling.

Covers:
  * scraper._eu_deadlines  — parse a single date OR a two-stage [stage-1, stage-2] list,
    returning them sorted so the effective deadline is the LAST (stage-2) date.
  * scraper._eu_budget     — total + per-grant floor/ceiling + expected #grants.
  * auto_scorer.closed_call_hard_reject — a future stage-2 deadline OVERRIDES a bare portal
    "Closed" status (the EU portal marks a two-stage topic Closed after stage-1), while a
    genuinely past/absent deadline still rejects, and strong closure prose always rejects.

Pure unit tests — no network. A dummy Supabase env lets auto_scorer import.

Run:  python -m unittest tests.test_euft_twostage
"""
import os
import sys
import unittest
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import scraper as S            # noqa: E402
from core import auto_scorer as A        # noqa: E402

_FUTURE = "2099-09-17"
_PAST = "2000-03-04"


class EuDeadlinesTests(unittest.TestCase):
    def test_single_date(self):
        self.assertEqual(S._eu_deadlines("2026-03-04T00:00:00.000+0000"),
                         [date(2026, 3, 4)])

    def test_two_stage_list_sorted_max_last(self):
        dls = S._eu_deadlines(["2026-09-17T00:00:00.000+0000",
                               "2026-03-04T00:00:00.000+0000"])   # unsorted input
        self.assertEqual(dls, [date(2026, 3, 4), date(2026, 9, 17)])
        self.assertEqual(dls[-1], date(2026, 9, 17))              # effective = stage-2

    def test_empty(self):
        self.assertEqual(S._eu_deadlines(None), [])
        self.assertEqual(S._eu_deadlines([]), [])


class EuBudgetTests(unittest.TestCase):
    def test_total_floor_ceiling_awards(self):
        bo = ('{"budgetTopicActionMap":{"111":[{"minContribution":5000000,'
              '"maxContribution":7000000,"budgetYearMap":{"2026":"25000000"},'
              '"expectedGrants":5}]}}')
        total, cur, floor, ceil, awards = S._eu_budget(bo)
        self.assertEqual(total, 25000000.0)
        self.assertEqual(cur, "EUR")
        self.assertEqual(floor, 5000000.0)
        self.assertEqual(ceil, 7000000.0)
        self.assertEqual(awards, 5)

    def test_empty_blob(self):
        self.assertEqual(S._eu_budget(""), (None, None, None, None, None))
        self.assertEqual(S._eu_budget("not json"), (None, None, None, None, None))


class ClosedCallTwoStageTests(unittest.TestCase):
    def test_portal_closed_but_future_stage2_is_kept(self):
        cand = {"_closed": True, "call_submission_deadline": _FUTURE}
        rej, _ = A.closed_call_hard_reject(cand)
        self.assertFalse(rej)                     # stage-2 live → keep despite portal Closed

    def test_portal_closed_and_past_deadline_rejects(self):
        cand = {"_closed": True, "call_submission_deadline": _PAST}
        rej, reason = A.closed_call_hard_reject(cand)
        self.assertTrue(rej)
        self.assertIn("closed", reason.lower())

    def test_portal_closed_and_no_deadline_rejects(self):
        cand = {"_closed": True}
        rej, _ = A.closed_call_hard_reject(cand)
        self.assertTrue(rej)                      # no future date → trust the portal status

    def test_soft_badge_with_future_deadline_is_kept(self):
        cand = {"brief_description": "Call closed", "call_submission_deadline": _FUTURE}
        rej, _ = A.closed_call_hard_reject(cand)
        self.assertFalse(rej)                     # soft badge overridden by a live deadline

    def test_strong_prose_rejects_even_with_future_deadline(self):
        cand = {"brief_description": "This programme is no longer accepting applications.",
                "call_submission_deadline": _FUTURE}
        rej, _ = A.closed_call_hard_reject(cand)
        self.assertTrue(rej)                      # strong closure prose always wins

    def test_open_call_is_kept(self):
        cand = {"brief_description": "Apply now for our open grant.",
                "call_submission_deadline": _FUTURE}
        rej, _ = A.closed_call_hard_reject(cand)
        self.assertFalse(rej)


if __name__ == "__main__":
    unittest.main(verbosity=2)
