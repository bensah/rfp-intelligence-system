"""No verifiable closing date means block, and a year on the page is not a closing date.

THE REPORTED CASE. Expired calls from one donor-catalogue funder kept reappearing in the
pipeline every week, after being dealt with more than once. Owner's rule: "unless an
explicit deadline is detected, block all."

THREE INDEPENDENT REASONS THEY SURVIVED.

1. A YEAR ANYWHERE COUNTED AS PROOF THE WINDOW WAS OPEN. `insufficient_data_reject`
   kept any undated row where `_latest_year_in(blob)` found a current-or-future year, and
   the blob was URL + title + body + page text. One of these calls announces itself as
   "The Global South E-Health Observatory launches its 2026 call for project proposals!" -
   its own title satisfied the test, and the window had closed years earlier.

2. THE PAGE SAID SO IN PLAIN WORDS AND NOBODY READ IT. `_SUBMIT_LABELS` had no entry for
   "open until", the phrasing this funder uses, so "Open until 30 December 2017" fell into
   the unlabelled bucket, came back confidence='low', and the scan pipeline discards
   anything below medium. A date you cannot label confidently is still decisive when it is
   nine years old: "is this the deadline" and "has this window closed" are different
   questions.

3. NOTHING AGED THE STORE. `mark_closed_past_deadline` existed with NO CALLER, and its
   predicate `deadline < today` can never be true for a NULL deadline, so an undated row
   was Open forever by construction. Screening is handed the whole Open set every run:
   719 Open rows on the live store, 258 already past their deadline, 30 with none at all.

Measured after the fix: all 10 of that funder's rows are blocked, and 17 of the 30 undated
Open rows overall. The other 13 are kept because they were POSTED within the stale-posting
window - a recently-posted undated call is plausibly live, which is the existing rule.

Run:  python -m unittest tests.test_expiry_no_deadline
"""
import os
import sys
import unittest
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import auto_scorer as A                                     # noqa: E402
from core import deadline_extract as DE                               # noqa: E402
from core import extracted_store as ES                                # noqa: E402

THIS_YEAR = date.today().year


def _cand(**kw):
    c = {"opportunity_title": "", "opportunity_link": "", "brief_description": "",
         "_page_text": "", "notes": "", "call_submission_deadline": None,
         "date_posted": None, "call_award_value": None,
         "call_geographic_scope": [], "call_domain_areas": []}
    c.update(kw)
    return c


class AYearIsNotAnOpenWindowTests(unittest.TestCase):
    def test_a_year_in_the_title_is_not_a_closing_date(self):
        # The reported row, verbatim.
        blob = ("The Global South E-Health Observatory launches its %d call for "
                "project proposals!" % THIS_YEAR)
        self.assertEqual(A._latest_year_in(blob), THIS_YEAR)   # the weak test still finds it
        self.assertIsNone(A._live_window_year(blob))           # the real question

    def test_a_year_in_a_url_slug_is_not_a_closing_date(self):
        self.assertIsNone(A._live_window_year(
            "https://example.org/en/call-for-project/%d-call-for-projects/" % THIS_YEAR))

    def test_a_conference_date_is_not_a_closing_date(self):
        # A proximity heuristic read this as an open window because "apply" sat nearby.
        # Deciding WHICH date on a page is the closing one needs a labelled extractor.
        self.assertIsNone(A._live_window_year(
            "Winners will be awarded at the Observatory's 10th annual conference, to be "
            "held at the end of %d at the foundation's headquarters. To apply, see the "
            "guidelines." % THIS_YEAR))

    def test_a_real_stated_closing_date_does_count(self):
        self.assertEqual(
            A._live_window_year("Applications close on 30 September %d. Submit via the "
                                "portal." % THIS_YEAR), THIS_YEAR)


class TheStatedWindowIsReadTests(unittest.TestCase):
    def test_open_until_is_recognised_as_a_submission_label(self):
        r = DE.extract_deadline("Open until 30 December 2017", scan_year=2026, title="")
        self.assertEqual(r["deadline"], "2017-12-30")
        self.assertIn(r["confidence"], ("high", "medium"))

    def test_a_future_open_until_is_read_too(self):
        r = DE.extract_deadline("Applications are open until 15 March %d" % (THIS_YEAR + 1),
                                scan_year=THIS_YEAR, title="")
        self.assertEqual(str(r["deadline"])[:4], str(THIS_YEAR + 1))

    def test_a_low_confidence_past_window_still_expires_the_call(self):
        # Not trusted enough to publish as the deadline; decisive about the page.
        bad_future, why = A.deadline_in_future(_cand(_expired_window="2017-12-30"))
        self.assertFalse(bad_future)
        self.assertIn("2017-12-30", why)

    def test_a_stated_past_window_beats_a_newer_year_elsewhere(self):
        # "Open until 30 December 2017" beside a current-year copyright line.
        cand = _cand(_page_text="Open until 30 December 2017. Copyright %d Foundation."
                                % THIS_YEAR)
        self.assertFalse(A.deadline_in_future(cand)[0])


class TheScreeningGateTests(unittest.TestCase):
    def test_an_undated_call_with_no_expiry_evidence_is_KEPT(self):
        # COURSE CORRECTION (owner, 2026-08-17). An earlier version of this rule blocked
        # here, on the grounds that no current closing date could be confirmed. That reads
        # "we could not confirm this is open" as "this is closed", and it is wrong in the
        # direction that costs money: an open-ended fund can never produce the evidence
        # being demanded. Rejection now requires POSITIVE evidence of expiry.
        cand = _cand(opportunity_title="Its %d call for project proposals!" % THIS_YEAR,
                     _page_text="A" * 400, call_award_value=50000,
                     call_geographic_scope=["Kenya"])
        self.assertFalse(A.insufficient_data_reject(cand)[0])

    def test_a_genuinely_rolling_call_survives(self):
        cand = _cand(opportunity_title="Rolling small grants programme",
                     brief_description="Applications are accepted on a rolling basis.",
                     _page_text="Applications are accepted on a rolling basis all year.",
                     call_award_value=50000, call_geographic_scope=["Kenya"],
                     call_domain_areas=["Health"])
        self.assertFalse(A.insufficient_data_reject(cand)[0])

    def test_a_recently_posted_undated_call_survives(self):
        # The existing posting-date grace: plausibly live, so not our business to drop.
        cand = _cand(opportunity_title="Future Employment Support - EOI",
                     date_posted=(date.today() - timedelta(days=10)).isoformat(),
                     _page_text="B" * 400)
        self.assertFalse(A.insufficient_data_reject(cand)[0])

    def test_a_long_stale_undated_call_is_blocked(self):
        cand = _cand(opportunity_title="An old call",
                     date_posted=(date.today() - timedelta(days=900)).isoformat(),
                     _page_text="C" * 400, call_award_value=1000)
        self.assertTrue(A.insufficient_data_reject(cand)[0])

    def test_a_call_with_a_real_future_deadline_is_untouched(self):
        cand = _cand(call_submission_deadline=(date.today()
                                               + timedelta(days=30)).isoformat())
        self.assertFalse(A.insufficient_data_reject(cand)[0])
        self.assertTrue(A.deadline_in_future(cand)[0])


class RollingCallsAreNeverExpiredTests(unittest.TestCase):
    """An open-ended fund has no closing date to pass (owner, 2026-08-17)."""

    ROLLING = dict(
        opportunity_title="Request for Proposals: Global Health and Wellbeing",
        brief_description="Open call for proposals.",
        _page_text=("We plan to accept applications and will review applications on a "
                    "rolling basis, though we expect to take some months."),
        call_award_value=100_000, call_geographic_scope=["Kenya"],
        call_domain_areas=["Health"])

    def test_rolling_wording_in_the_PAGE_BODY_is_recognised(self):
        # It read only title + brief + notes, and a funder states this in prose, so a
        # genuinely open-ended fund looked identical to an abandoned page.
        self.assertTrue(A.is_rolling_call(_cand(**self.ROLLING)))

    def test_a_rolling_call_survives_both_gates(self):
        cand = _cand(**self.ROLLING)
        self.assertTrue(A.deadline_in_future(cand)[0])
        self.assertFalse(A.insufficient_data_reject(cand)[0])

    def test_a_rolling_call_posted_years_ago_still_survives(self):
        # The whole point of open-ended: age is not evidence of closure.
        cand = _cand(date_posted="2021-01-01", **self.ROLLING)
        self.assertTrue(A.deadline_in_future(cand)[0])
        self.assertFalse(A.insufficient_data_reject(cand)[0])

    def test_rolling_is_recorded_as_a_state_not_a_blank(self):
        # A fabricated 31-December deadline is worse than none: everything downstream
        # treats it as fact and the call silently "expires" at new year.
        self.assertEqual(A.ROLLING_WINDOW, "Rolling")
        self.assertTrue(A.is_rolling_call(_cand(funding_window="Rolling")))

    def test_the_pipeline_marks_a_rolling_call_and_leaves_the_deadline_null(self):
        import io
        with io.open(os.path.join(_ROOT, "core", "scan_pipeline.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("ROLLING_WINDOW", src)
        self.assertIn("is_rolling_call(cand)", src)


class ThePostedDateIsRecoveredTests(unittest.TestCase):
    """The stale-posting rule is the only evidence an undated page can offer."""

    def test_an_unlabelled_date_under_the_heading_is_read_as_the_posting_date(self):
        # The reported page: a 2026 call stamped 17/10/2025 under its heading, arriving
        # with date_posted NULL and therefore ageless.
        got, how = DE.extract_posted_date(
            "The Observatory launches its 2026 call for project proposals! 17/10/2025 "
            "Join the community of award winners...",
            title="The Observatory launches its 2026 call for project proposals!")
        self.assertEqual(got, "2025-10-17")
        self.assertEqual(how, "page-head")

    def test_a_labelled_publication_date_wins(self):
        got, how = DE.extract_posted_date("Published: 28 February 2025. Details follow.")
        self.assertEqual(got, "2025-02-28")
        self.assertEqual(how, "labelled")

    def test_a_future_date_is_never_a_posting_date(self):
        # That is a deadline, an event or an award ceremony. This can only make a page
        # look older, never younger.
        self.assertEqual(DE.extract_posted_date(
            "Prizes will be awarded on 30 December %d." % (THIS_YEAR + 1))[0], None)

    def test_a_submission_labelled_date_is_not_a_posting_date(self):
        self.assertIsNone(DE.extract_posted_date(
            "Application deadline: 15 January 2020.")[0])

    def test_a_stale_posting_blocks_at_the_screening_gate_too(self):
        # The rule lived only in deadline_in_future; the screening pass runs this gate, so
        # without it a page from 2017 could still reach a review week.
        cand = _cand(opportunity_title="An old call", date_posted="2017-09-29",
                     _page_text="C" * 400, call_award_value=1000)
        blocked, why = A.insufficient_data_reject(cand)
        self.assertTrue(blocked)
        self.assertIn("2017-09-29", why)


class TheStoreIsAgedTests(unittest.TestCase):
    def test_the_undated_sweep_exists_and_is_bounded_by_a_window(self):
        # The gap that made an undated row Open forever: `deadline < today` is never true
        # for NULL, so a second sweep is needed for undated rows.
        self.assertTrue(callable(ES.mark_closed_stale_undated))
        self.assertEqual(ES._STALE_UNDATED_DAYS, A._STALE_POSTING_DAYS)

    def test_a_bad_date_is_not_a_crash(self):
        self.assertEqual(ES.mark_closed_stale_undated("not-a-date"), 0)

    def test_the_cron_ages_the_store_before_screening(self):
        # Wiring is the whole point - the function existed and nothing called it.
        import io
        with io.open(os.path.join(_ROOT, "scripts", "run_scan.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("mark_closed_past_deadline", src)
        self.assertIn("mark_closed_stale_undated", src)
        self.assertLess(src.index("mark_closed_past_deadline"),
                        src.index("screen_all_tenants"),
                        "the store must be aged BEFORE screening reads the Open set")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ThreeMonthWindowTests(unittest.TestCase):
    """The undated-call window is three months, not six (owner, 2026-08-18).

    Six months meant a call posted in January was still plausible in August. The window
    exists to protect a genuine rolling call found late, or one whose source was verified
    after publication — not to keep a closed page alive for half a year."""

    def test_the_window_is_ninety_days(self):
        self.assertEqual(A._STALE_POSTING_DAYS, 90)

    def test_a_call_posted_four_months_ago_with_no_deadline_is_expired(self):
        from datetime import date, timedelta
        cand = {"date_posted": (date.today() - timedelta(days=120)).isoformat()}
        keep, why = A.deadline_in_future(cand)
        self.assertFalse(keep)
        self.assertIn("120d ago", why)

    def test_a_call_posted_last_month_is_still_plausible(self):
        from datetime import date, timedelta
        cand = {"date_posted": (date.today() - timedelta(days=30)).isoformat()}
        self.assertTrue(A.deadline_in_future(cand)[0])

    def test_an_explicit_rolling_call_is_never_aged_out(self):
        from datetime import date, timedelta
        cand = {"date_posted": (date.today() - timedelta(days=400)).isoformat(),
                "funding_window": "Rolling",
                "opportunity_title": "Applications accepted on a rolling basis"}
        self.assertTrue(A.deadline_in_future(cand)[0])
