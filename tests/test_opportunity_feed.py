"""Regression tests for the right-rail opportunity feed (BUG 6).

The three cards (Top Funding / Top Matches / Also Interesting) must be MUTUALLY
EXCLUSIVE — no opportunity appears in more than one card. Priority when an opportunity
qualifies for several: Top Matches (strong fit) → Top Funding (biggest/most-urgent) →
Also Interesting.

Pure unit test — no DB, no streamlit.

Run:  python -m unittest tests.test_opportunity_feed
"""
import os
import sys
import unittest
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import opportunity_feed as F      # noqa: E402

_TODAY = date(2026, 8, 1)
_FUTURE = "2099-12-31"


def _row(uid, *, amount=0, alignment=0.0, rec="", posted="2026-07-30", deadline=_FUTURE):
    return {
        "uid": uid, "opportunity_title": f"Call {uid}",
        "opportunity_link": f"https://x.org/{uid}",
        "funding_agency": "Funder", "call_award_value": amount,
        "alignment_score": alignment, "auto_recommendation": rec,
        "call_submission_deadline": deadline, "date_posted": posted,
        "call_geographic_scope": ["Kenya"],
    }


def _uids(bucket):
    return [it["uid"] for it in bucket]


class RailDedupTests(unittest.TestCase):
    def test_buckets_are_mutually_exclusive(self):
        rows = [
            _row("big-match", amount=5_000_000, alignment=0.9, rec="Proceed"),  # match + big
            _row("big-nonmatch", amount=9_000_000, alignment=0.1),             # big, no fit
            _row("fresh", amount=1000, alignment=0.0, posted="2026-07-31"),    # just interesting
            _row("old", amount=1000, alignment=0.0, posted="2026-01-01"),
        ]
        res = F.classify(rows, today=_TODAY)
        all_uids = _uids(res["top_funding"]) + _uids(res["top_matches"]) + _uids(res["other"])
        self.assertEqual(len(all_uids), len(set(all_uids)), f"duplicate across cards: {all_uids}")

    def test_strong_match_goes_to_matches_not_funding(self):
        rows = [_row("m", amount=9_000_000, alignment=0.95, rec="Proceed")]
        res = F.classify(rows, today=_TODAY)
        self.assertIn("m", _uids(res["top_matches"]))
        self.assertNotIn("m", _uids(res["top_funding"]))   # placed once, in Matches
        self.assertNotIn("m", _uids(res["other"]))

    def test_big_nonmatch_goes_to_funding_not_other(self):
        rows = [
            _row("big", amount=9_000_000, alignment=0.0),
            _row("small", amount=10, alignment=0.0, posted="2026-07-31"),
        ]
        res = F.classify(rows, today=_TODAY)
        self.assertIn("big", _uids(res["top_funding"]))
        self.assertNotIn("big", _uids(res["other"]))       # not duplicated into Also Interesting

    def test_expired_calls_excluded(self):
        rows = [_row("dead", amount=9_000_000, alignment=0.95, rec="Proceed",
                     deadline="2020-01-01")]
        res = F.classify(rows, today=_TODAY)
        self.assertEqual(_uids(res["top_matches"]), [])
        self.assertEqual(_uids(res["top_funding"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
