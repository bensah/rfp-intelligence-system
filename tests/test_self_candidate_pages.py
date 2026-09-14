"""A call with no separate page of its own must still be detectable.

THE REPORTED CASE. Two live calls found via LinkedIn that the system had never surfaced.
They failed for two unrelated reasons, and only one was a coverage gap:

  * Coefficient Giving WAS a registered source and the scraper DID find the call — the miss
    was downstream (synthesis failed at scan time, leaving call_domain_areas empty, so the
    theme gate rejected it). See tests.test_rescreen_reads_the_page for that family.
  * The Audacious Project was not registered at all — and registering it changed nothing,
    because it returned 0 candidates.

WHY 0. `_extract_candidates_from_html` is pure ANCHOR extraction: a source page produces
candidates only by LINKING OUT to them. That models a donor who publishes an index of calls,
and fails completely for one whose call has no separate page — an always-open application
like audaciousproject.org/apply, a single-programme foundation, a fund whose "how to apply"
IS the opportunity. Such a source scans cleanly and reports nothing, which reads in every log
and counter as "this donor has nothing open" rather than "this scraper cannot see this shape
of page". That is the worst kind of gap: silent, and indistinguishable from success.

`_self_candidate` emits the page itself when anchor extraction found nothing and the page
reads as a call. It does NOT lower the bar — the candidate goes through the same enrichment
and the same `is_eligible` gate as any other.

SECOND BUG, found by fixing the first. The page then died on `deadline_in_future`'s
year-fallback ("latest year on page is 2025"), even though the extractor reads it as
ROLLING with high confidence. That branch infers expiry from the ABSENCE of a current-year
mention — evidence only for a call that has a window at all. An always-open application
states no dates by design, so it was being retired for the accident of not having been
reworded this year. The stale-posting branch directly above it already exempted rolling
calls; this one now does too.

Run:  python -m unittest tests.test_self_candidate_pages
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

from core import auto_scorer as A                                     # noqa: E402
from core import scraper as S                                         # noqa: E402

_BODY = (
    "Is your idea audacious? Are you a changemaker with a bold vision? Are you a non-profit "
    "with an experienced team equipped to receive large scale philanthropic support? "
    "Submit your idea. We look for ideas that cover a wide range of issues, from global "
    "health and climate change, to social justice and education. Read on to see if your "
    "idea is a good fit. Eligibility criteria: your organisation must be a registered "
    "non-profit with a proven concept. How to apply: submit an expression of interest; "
    "applications are accepted on a rolling basis through our online portal. The "
    "application process has several stages and our team reviews submissions year-round "
    "as they arrive from around the world."
)


def _page(title="Apply | The Audacious Project", h1="Submit your idea", body=_BODY):
    return (f"<html><head><title>{title}</title></head><body>"
            f"<h1>{h1}</h1><p>{body}</p></body></html>")


class TheSelfCandidateTests(unittest.TestCase):
    def _cand(self, url="https://audaciousproject.org/apply", **kw):
        # Enrichment fetches the network; the shape is what matters here.
        import unittest.mock as mock
        with mock.patch.object(S, "_enrich_candidate", lambda c: None):
            return S._self_candidate("The Audacious Project", url, _page(**kw))

    def test_a_page_that_is_the_call_becomes_a_candidate(self):
        cand = self._cand()
        self.assertIsNotNone(cand)
        self.assertEqual(cand["opportunity_link"], "https://audaciousproject.org/apply")
        self.assertTrue(cand["_self_candidate"])

    def test_a_generic_title_is_replaced_by_the_h1(self):
        # "Apply" names the nav item, not the call; a reviewer scanning a week's list
        # cannot tell what it is.
        self.assertEqual(self._cand()["opportunity_title"], "Submit your idea")

    def test_a_thin_page_is_not_a_candidate(self):
        self.assertIsNone(self._cand(body="Coming soon."))

    def test_a_page_with_no_call_wording_is_not_a_candidate(self):
        self.assertIsNone(self._cand(
            title="About | The Audacious Project", h1="Who we are",
            body="We are a foundation supporting bold ideas. " * 30))

    def test_weak_wording_alone_is_not_enough(self):
        # THE REASON THIS USES THE PROJECT'S OWN TEST. An earlier version of
        # _self_candidate carried its own regex that accepted "how to apply" /
        # "eligibility criteria" / "apply now" on their own — looser than the codebase's
        # settled rule, under which those are _RFP_WEAK_PHRASES and count only alongside
        # corroborating detail. Every funder's generic grants page says "how to apply".
        self.assertIsNone(self._cand(
            title="Grants | A Foundation", h1="Grants",
            body=("Apply now for funding opportunities. How to apply: read the "
                  "eligibility criteria and follow the application process. " * 8)))

    def test_the_rule_is_the_same_one_the_search_scan_uses(self):
        # web_search._fetch_signals validates a search hit with exactly this pair; a second
        # private vocabulary here would drift from it.
        from core import auto_scorer as _A
        self.assertTrue(S._page_body_is_a_call(
            "We invite a request for proposals from eligible organisations."))
        self.assertTrue(S._page_body_is_a_call("Submit an EOI by Friday."))
        self.assertFalse(S._page_body_is_a_call("We are a foundation supporting ideas."))
        self.assertFalse(S._page_body_is_a_call(""))
        # Sourced from auto_scorer, not redefined here.
        self.assertTrue(any(p in "a request for proposals" for p in
                            _A._RFP_STRONG_PHRASES))

    def test_a_listing_url_is_never_emitted_as_a_call(self):
        # Emitting an index page would put a listing URL in the pipeline — the thing
        # auto_scorer._LISTING_URL_RE exists to stop.
        self.assertIsNone(self._cand(url="https://audaciousproject.org/grants/list"))

    def test_it_only_runs_when_anchor_extraction_found_nothing(self):
        # The zero-candidate guard is load-bearing, not incidental. A donor's INDEX page
        # carries strong call wording too — unitaid.org/calls-for-proposals/ contains
        # "call for proposals" — so emitting a page that verifies as a call regardless of
        # whether it linked out would inject listing pages into the pipeline. Having
        # linked to real calls is the evidence that a page is an index rather than a call.
        import io as _io
        with _io.open(os.path.join(_ROOT, "core", "scraper.py"), encoding="utf-8") as fh:
            src = fh.read()
        call = src.index("_self_candidate(name, url, r.text)")
        guard = src.rindex("if not cands:", 0, call)
        self.assertLess(call - guard, 200,
                        "the self-candidate must never pre-empt real linked candidates")

    def test_the_rendered_path_has_the_same_fallback(self):
        # Several never-producing sources are html_js. If only the static path had the
        # fallback, the two would disagree about what is findable purely because of how a
        # donor's site is built.
        import io as _io
        with _io.open(os.path.join(_ROOT, "core", "scraper.py"), encoding="utf-8") as fh:
            src = fh.read()
        js = src[src.index("def _scan_html_js"):]
        js = js[:js.index(chr(10) + "def ", 10)]
        self.assertIn("_self_candidate(name, url, html_text)", js)
        self.assertIn("if not cands:", js)


class RollingCallsSurviveTheYearFallbackTests(unittest.TestCase):
    def _cand(self, **kw):
        c = {"opportunity_title": "", "opportunity_link": "", "brief_description": "",
             "_page_text": "", "notes": "", "call_submission_deadline": None,
             "date_posted": None, "call_award_value": None,
             "call_geographic_scope": [], "call_domain_areas": []}
        c.update(kw)
        return c

    def test_a_rolling_page_whose_newest_year_is_past_is_kept(self):
        last_year = date.today().year - 1
        cand = self._cand(
            opportunity_title="Submit your idea",
            _page_text=f"Copyright {last_year}. Applications are accepted on a rolling "
                       "basis year-round; there is no fixed deadline.")
        self.assertTrue(A._is_rolling_call(cand), "fixture must read as rolling")
        ok, why = A.deadline_in_future(cand)
        self.assertTrue(ok, f"a rolling call was retired for being open: {why}")

    def test_a_non_rolling_page_whose_newest_year_is_past_is_still_expired(self):
        # The rule this branch exists for must not be weakened.
        last_year = date.today().year - 1
        cand = self._cand(
            opportunity_title="Call for proposals",
            _page_text=f"Applications were due 30 December {last_year}.")
        ok, why = A.deadline_in_future(cand)
        self.assertFalse(ok)
        self.assertIn(str(last_year), why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
