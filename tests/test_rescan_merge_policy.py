"""Regression tests for the rescan merge policy (core/scan_pipeline._build_merge_payload).

A rescan that re-finds an opportunity must NOT rewrite what the user already has:

  * `search_date` is the FIRST-discovery date and is IMMUTABLE. It used to be stamped with
    now() on every rescan, so months-old rows all showed today's date and every
    search→submission cycle-time metric was wrong. Last-seen now has its own column.
  * A populated cell is never overwritten by machine-scraped data (fill-the-gap only).
  * A CONTRADICTING value is no longer silently discarded — the stored value still wins,
    but the difference is recorded in `merge_conflicts` for human review (a funder moving a
    deadline or restating an award size is real news).

Excel migration is deliberately unaffected: it overwrites by design, because a human typed
the workbook.

Run:  python -m unittest tests.test_rescan_merge_policy
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.scan_pipeline import (                       # noqa: E402
    _build_merge_payload, _payload_meaningful, _MERGE_BOOKKEEPING,
)

_POL = {"themes": {}, "exclusions": {}}
_ORIGINAL = "2026-03-01T09:00:00+00:00"


def _existing(**over):
    row = {"uid": "X-1", "search_date": _ORIGINAL, "opportunity_title": "Curated title",
           "alignment_score": 88, "call_submission_deadline": "2026-08-12",
           "brief_description": "A human-curated description."}
    row.update(over)
    return row


def _candidate(**over):
    cand = {"opportunity_title": "Scraped title", "opportunity_link": "https://x/1"}
    cand.update(over)
    return cand


class DiscoveryDateTests(unittest.TestCase):
    def test_search_date_is_never_rewritten(self):
        p = _build_merge_payload(_candidate(), _existing(), _POL)
        self.assertNotIn("search_date", p, "a rescan must not rewrite the discovery date")

    def test_last_seen_is_stamped_instead(self):
        p = _build_merge_payload(_candidate(), _existing(), _POL)
        self.assertIn("last_seen_at", p)
        self.assertNotEqual(p["last_seen_at"], _ORIGINAL)

    def test_blank_search_date_is_backfilled_once(self):
        p = _build_merge_payload(_candidate(), _existing(search_date=None), _POL)
        self.assertEqual(p.get("search_date"), p.get("last_seen_at"))


class NoOverwriteTests(unittest.TestCase):
    def test_populated_cell_is_not_overwritten(self):
        p = _build_merge_payload(
            _candidate(call_submission_deadline="2026-09-30"), _existing(), _POL)
        self.assertNotIn("call_submission_deadline", p)

    def test_blank_cell_is_filled(self):
        p = _build_merge_payload(
            _candidate(call_submission_deadline="2026-09-30"),
            _existing(call_submission_deadline=None), _POL)
        self.assertEqual(p.get("call_submission_deadline"), "2026-09-30")

    def test_title_is_never_touched(self):
        p = _build_merge_payload(_candidate(), _existing(), _POL)
        self.assertNotIn("opportunity_title", p)


class ConflictFlaggingTests(unittest.TestCase):
    def test_contradiction_is_recorded_for_review(self):
        p = _build_merge_payload(
            _candidate(call_submission_deadline="2026-09-30"), _existing(), _POL)
        mc = p.get("merge_conflicts") or {}
        self.assertIn("call_submission_deadline", mc)
        self.assertEqual(mc["call_submission_deadline"]["kept"], "2026-08-12")
        self.assertEqual(mc["call_submission_deadline"]["incoming"], "2026-09-30")
        self.assertIn("seen_at", mc["call_submission_deadline"])

    def test_agreement_records_nothing(self):
        p = _build_merge_payload(
            _candidate(call_submission_deadline="2026-08-12"), _existing(), _POL)
        self.assertNotIn("merge_conflicts", p)

    def test_earlier_unreviewed_conflicts_are_preserved(self):
        prior = {"currency": {"kept": "USD", "incoming": "EUR", "seen_at": "2026-07-01"}}
        p = _build_merge_payload(
            _candidate(call_submission_deadline="2026-09-30"),
            _existing(merge_conflicts=prior), _POL)
        self.assertIn("currency", p["merge_conflicts"])
        self.assertIn("call_submission_deadline", p["merge_conflicts"])

    def test_string_encoded_prior_conflicts_are_tolerated(self):
        p = _build_merge_payload(
            _candidate(call_submission_deadline="2026-09-30"),
            _existing(merge_conflicts='{"currency": {"kept": "USD"}}'), _POL)
        self.assertIn("call_submission_deadline", p["merge_conflicts"])


class NoOpReportingTests(unittest.TestCase):
    def test_bookkeeping_only_payload_is_not_an_update(self):
        self.assertFalse(_payload_meaningful({k: "x" for k in _MERGE_BOOKKEEPING}))

    def test_a_flagged_conflict_counts_as_meaningful(self):
        self.assertTrue(_payload_meaningful({"last_seen_at": "x", "merge_conflicts": {"a": 1}}))

    def test_a_gap_fill_counts_as_meaningful(self):
        self.assertTrue(_payload_meaningful({"last_seen_at": "x", "currency": "USD"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
