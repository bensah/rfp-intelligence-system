""""We could not read this page" and "this is not a call" are different findings.

`rfp_signal_gate` judges a candidate on its title, URL, description and notes. When there
was nothing to read, it returned "no valid RFP signal (no call wording, deadline, or award
amount)" — a statement about the OPPORTUNITY that the evidence does not support. It reads
as "this is not a call" when the truth is "nobody managed to read this page".

WHY IT MATTERS EVEN THOUGH NO VERDICT CHANGES. An unreadable page cannot be screened, so
rejecting it is right either way. What changes is the reject log, which is what anyone
diagnosing coverage actually reads — and a misleading reason there costs real time. A
Fondation Pierre Fabre URL that was a plain 404 was logged as "no valid RFP signal" and
read as a gate problem for an hour before the page turned out to be gone. Measured on a
random sample of the live reject log, 4 of 36 pages were unreachable and every one carried
the misleading reason.

THE SILENT HALF. A 404/410 or a soft-404 body already set `_dead_page`, so those reported
"dead link" correctly. But a request that RAISES — timeout, DNS failure, connection reset,
TLS error — left NO mark at all: `live_check.recheck_and_enrich` logged at debug level and
returned False, and the candidate went on to be judged on its title. That is now recorded
as `_fetch_failed`, deliberately NOT as `_dead_page`: a timeout is not evidence the
resource is gone, and a bot wall or a slow server must not become a permanent verdict.

Re-gating the same 22 live rejects with live_check run first: 1 now reports the fetch
failure, 9 keep "no valid RFP signal" (readable text, genuinely not calls), the rest are
caught earlier by the aggregator / deadline gates with their own better reasons, and
NOTHING was accepted — confirming this sharpens wording without loosening the gate.

Run:  python -m unittest tests.test_unreadable_page_reason
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

from core import auto_scorer as A                                     # noqa: E402
from core import live_check as LC                                     # noqa: E402

_BASE = {"opportunity_title": "Community-Led Practices for Maternal Health",
         "opportunity_link": "https://example.org/funding-opportunity/community-led/",
         "brief_description": None, "notes": None}


def _cand(**kw):
    c = dict(_BASE)
    c.update(kw)
    return c


class TheReasonSaysWhichItIsTests(unittest.TestCase):
    def test_a_failed_fetch_says_so_and_names_the_error(self):
        ok, why = A.rfp_signal_gate(
            _cand(_fetch_failed=True, _fetch_error="ConnectTimeout: timed out"))
        self.assertFalse(ok)
        self.assertIn("could not be fetched", why)
        self.assertIn("ConnectTimeout", why)
        self.assertNotIn("no valid RFP signal", why)

    def test_no_text_at_all_claims_only_what_is_known(self):
        # Distinct from a failed fetch: nothing here says a fetch was even attempted (a
        # listing-derived candidate whose enrichment was capped looks exactly like this),
        # so the wording must not assert that one failed.
        ok, why = A.rfp_signal_gate(_cand())
        self.assertFalse(ok)
        self.assertIn("no readable description or page text", why)

    def test_readable_text_that_is_not_a_call_keeps_the_original_reason(self):
        # The 9-of-22 case. This is a real finding about the opportunity and must not be
        # softened into a complaint about readability.
        ok, why = A.rfp_signal_gate(_cand(
            brief_description="We are a foundation supporting bold ideas.",
            _page_text="About our foundation, our people and our history. " * 40))
        self.assertFalse(ok)
        self.assertEqual(why,
                         "no valid RFP signal (no call wording, deadline, or award amount)")

    def test_a_known_dead_page_keeps_its_more_precise_reason(self):
        # rfp_signal_gate runs BEFORE error_page_reject in is_eligible, so without this a
        # 404 would fall into the no-text branch and LOSE "dead link (HTTP 404)" —
        # trading a precise reason for a vaguer one, the opposite of the point.
        ok, why = A.rfp_signal_gate(
            _cand(_dead_page=True, _dead_reason="dead link (HTTP 404)"))
        self.assertFalse(ok)
        self.assertEqual(why, "dead link (HTTP 404)")

    def test_a_dead_page_outranks_a_failed_fetch(self):
        ok, why = A.rfp_signal_gate(_cand(
            _dead_page=True, _dead_reason="dead link (HTTP 410)",
            _fetch_failed=True, _fetch_error="ReadTimeout"))
        self.assertIn("410", why)


class NoVerdictChangesTests(unittest.TestCase):
    """The gate must be exactly as strict as before — only the wording moves."""

    def test_a_real_call_is_still_accepted(self):
        ok, _ = A.rfp_signal_gate(
            _cand(opportunity_title="Request for Proposals: Maternal Health"))
        self.assertTrue(ok)

    def test_an_unreadable_page_is_still_rejected(self):
        for extra in ({"_fetch_failed": True}, {}, {"_dead_page": True}):
            self.assertFalse(A.rfp_signal_gate(_cand(**extra))[0], extra)

    def test_a_trusted_source_with_a_deadline_still_passes(self):
        # Step 6 — unchanged, and it runs BEFORE the new wording branches.
        ok, _ = A.rfp_signal_gate(
            _cand(call_submission_deadline="2026-12-01", _fetch_failed=True))
        self.assertTrue(ok, "the new branches must not pre-empt an accepting path")


class TheFetchFailureIsRecordedTests(unittest.TestCase):
    def test_a_raising_request_marks_the_candidate(self):
        cand = _cand()
        with mock.patch("requests.get", side_effect=TimeoutError("timed out")):
            out = LC.recheck_and_enrich(cand)
        self.assertFalse(out, "return value must stay False — the pipeline re-gates on it")
        self.assertTrue(cand["_fetch_failed"])
        self.assertIn("TimeoutError", cand["_fetch_error"])

    def test_it_is_not_flagged_as_a_dead_page(self):
        # A timeout is not evidence the resource is gone. `_dead_page` is a hard reject;
        # a bot wall or a slow server must not become a permanent verdict.
        cand = _cand()
        with mock.patch("requests.get", side_effect=OSError("connection reset")):
            LC.recheck_and_enrich(cand)
        self.assertNotIn("_dead_page", cand)

    def test_a_404_still_sets_dead_page_not_fetch_failed(self):
        cand = _cand()
        resp = mock.MagicMock(status_code=404)
        with mock.patch("requests.get", return_value=resp):
            LC.recheck_and_enrich(cand)
        self.assertTrue(cand.get("_dead_page"))
        self.assertNotIn("_fetch_failed", cand)


if __name__ == "__main__":
    unittest.main(verbosity=2)
