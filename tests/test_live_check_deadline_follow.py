"""An expired call must not reach the pipeline just because its deadline was never found.

THE LEAK, traced on a real row. A funder publishes the call on its own page but puts the
application WINDOW only on a companion calendar page. Two paths already knew to follow
that link — the scraper's own crawl and the Chromium deep read — but the path that
actually runs for a candidate discovered from a listing, `live_check.recheck_and_enrich`,
had only the plain-text extractor. So:

  1. the page text carries no date       -> no deadline
  2. no deadline                          -> the deadline gate has nothing to reject on
  3. `date_posted` was never persisted    -> the stale-posting rule had nothing either
  4. the TITLE says "2026" (the award year, though the window closed the previous
     November) -> the "latest year on the page is in the past" fallback sees a CURRENT
     year and passes
  5. the call is admitted, ~9 months expired

Both gates were defeated by the same misleading year. The fix is not another heuristic:
it is to FIND the deadline, and to keep the page text and posting date so the existing
rules have their evidence. Those rules are correct and already written — they were being
starved of input.

Nothing here touches the network: the fetch and the follow helpers are stubbed.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import live_check as LC                            # noqa: E402
from core.auto_scorer import deadline_in_future              # noqa: E402

TODAY = date.today()
LAST_YEAR = TODAY.replace(year=TODAY.year - 1)

# A call page that names no date at all — the shape that leaked. The title carries a
# FUTURE-SOUNDING year, which is exactly what defeated the year-based fallbacks.
PAGE = (
    "<html><head><title>The Observatory launches its "
    f"{TODAY.year} call for project proposals!</title></head><body>"
    "<p>The Observatory has opened its call for project proposals. Proposals may be "
    "submitted by entities working in the Global South. Register your project on our "
    "partner calendar page.</p>"
    "<a href='https://partner.example/call-for-projects-applications-open/'>Apply</a>"
    "</body></html>"
)


class _Resp:
    def __init__(self, text=PAGE, status=200):
        self.text, self.status_code = text, status


def _cand(**kw):
    c = {"opportunity_title": f"The Observatory launches its {TODAY.year} call",
         "opportunity_link": "https://funder.example/en/call-for-project/observatory/",
         "funding_agency": "A Health Funder"}
    c.update(kw)
    return c


def _run(cand, *, companion=None, pdf=None, pdf_dl=None, page=PAGE):
    """recheck_and_enrich with the network and both follow helpers stubbed."""
    with mock.patch("requests.get", return_value=_Resp(page)), \
         mock.patch("core.scraper._follow_companion_for_deadline",
                    return_value=companion), \
         mock.patch("core.scraper._find_application_pdf", return_value=pdf), \
         mock.patch("core.scraper._try_pdf_guide_deadline",
                    return_value=(pdf_dl, None)):
        return LC.recheck_and_enrich(cand)


class TheCompanionFollowRunsOnTheCheapPathTests(unittest.TestCase):
    def test_a_companion_deadline_is_recovered(self):
        c = _cand()
        self.assertTrue(_run(c, companion=LAST_YEAR))
        self.assertEqual(c["call_submission_deadline"], LAST_YEAR)
        self.assertTrue(c["_deadline_from_companion"])

    def test_the_recovered_deadline_expires_the_call(self):
        c = _cand()
        _run(c, companion=LAST_YEAR)
        ok, reason = deadline_in_future(c)
        self.assertFalse(ok)
        self.assertIn("deadline passed", reason)

    def test_a_guide_pdf_deadline_is_tried_first(self):
        c = _cand()
        _run(c, pdf="https://funder.example/guide.pdf", pdf_dl=LAST_YEAR,
             companion=TODAY + timedelta(days=90))
        # The PDF answer wins; the companion is only consulted when it is still missing.
        self.assertEqual(c["call_submission_deadline"], LAST_YEAR)
        self.assertEqual(c["_deadline_from_guide_pdf"],
                         "https://funder.example/guide.pdf")

    def test_the_companion_is_only_followed_when_still_undated(self):
        called = []

        def _spy(soup, url):
            called.append(url)
            return LAST_YEAR

        c = _cand()
        with mock.patch("requests.get", return_value=_Resp()), \
             mock.patch("core.scraper._follow_companion_for_deadline", _spy), \
             mock.patch("core.scraper._find_application_pdf",
                        return_value="https://f.example/g.pdf"), \
             mock.patch("core.scraper._try_pdf_guide_deadline",
                        return_value=(LAST_YEAR, None)):
            LC.recheck_and_enrich(c)
        self.assertEqual(called, [])          # the PDF already answered

    def test_an_existing_deadline_is_never_overwritten(self):
        future = TODAY + timedelta(days=60)
        c = _cand(call_submission_deadline=future)
        _run(c, companion=LAST_YEAR)
        self.assertEqual(c["call_submission_deadline"], future)

    def test_a_follow_that_raises_does_not_break_the_check(self):
        c = _cand()
        with mock.patch("requests.get", return_value=_Resp()), \
             mock.patch("core.scraper._follow_companion_for_deadline",
                        side_effect=RuntimeError("network")), \
             mock.patch("core.scraper._find_application_pdf",
                        side_effect=RuntimeError("network")), \
             mock.patch("core.scraper._try_pdf_guide_deadline",
                        return_value=(None, None)):
            self.assertTrue(LC.recheck_and_enrich(c))
        self.assertIsNone(c.get("call_submission_deadline"))


class ThePageTextIsKeptTests(unittest.TestCase):
    """Without it the extraction stored raw_text = "" and the evidence was thrown away."""

    def test_the_fetched_text_lands_on_the_candidate(self):
        c = _cand()
        _run(c, companion=LAST_YEAR)
        self.assertIn("Global South", c.get("_page_text") or "")

    def test_a_longer_existing_text_is_not_replaced_by_a_shorter_one(self):
        long_text = "x" * 9000
        c = _cand(_page_text=long_text)
        _run(c, companion=LAST_YEAR)
        self.assertEqual(c["_page_text"], long_text)


class TwoINDEPENDENTDefencesTests(unittest.TestCase):
    """The owner asked for extraction to reject first and the eligibility scan to reject
    second. Each of these rejects the call on its own, so losing one is not a leak."""

    LINK = "https://funder.example/en/call-for-project/observatory/"

    def _base(self):
        return {"opportunity_title": f"launches its {TODAY.year} call for proposals",
                "opportunity_link": self.LINK}

    def test_a_past_deadline_alone_rejects(self):
        ok, reason = deadline_in_future({**self._base(),
                                         "call_submission_deadline": LAST_YEAR})
        self.assertFalse(ok)
        self.assertIn("deadline passed", reason)

    def test_an_old_posting_date_alone_rejects(self):
        ok, reason = deadline_in_future({**self._base(),
                                         "date_posted": LAST_YEAR.isoformat()})
        self.assertFalse(ok)
        self.assertIn("no deadline + not a rolling call", reason)

    def test_a_recently_posted_undated_call_is_still_kept(self):
        # The stale rule must not drop a call published last week whose date didn't parse.
        recent = (TODAY - timedelta(days=10)).isoformat()
        ok, _ = deadline_in_future({**self._base(), "date_posted": recent})
        self.assertTrue(ok)

    def test_an_explicitly_rolling_call_is_kept_however_old(self):
        c = {**self._base(), "date_posted": LAST_YEAR.isoformat(),
             "brief_description": "Applications are accepted on a rolling basis."}
        ok, _ = deadline_in_future(c)
        self.assertTrue(ok)

    def test_a_future_sounding_title_year_no_longer_rescues_it(self):
        # THE ORIGINAL DEFEAT: the title names the current year (the award cycle), so the
        # "latest year on the page is past" fallback saw a current year and passed. With
        # either piece of real evidence present, that no longer matters.
        titled = {**self._base(),
                  "opportunity_title": f"its {TODAY.year} call for project proposals!"}
        self.assertFalse(deadline_in_future({**titled,
                                             "call_submission_deadline": LAST_YEAR})[0])
        self.assertFalse(deadline_in_future({**titled,
                                             "date_posted": LAST_YEAR.isoformat()})[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
