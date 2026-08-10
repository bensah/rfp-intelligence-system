"""Which date on a call page is the SUBMISSION deadline.

THE LEAK. A funder published its application window on a calendar page that lists the
whole selection process, one row per stage:

    9 October to 7 November 2025        : applications open      <- the deadline
    14 November 2025                    : confirmation
    18 December 2025 to 23 January 2026 : expert evaluation

The extractor took the LATEST date it could find, so it returned 23 January 2026 — an
evaluation milestone eleven weeks after submissions had closed. That is the dangerous
direction: a closed call is handed a future deadline, sails through the deadline gate and
lands in a pipeline as though it were open.

Taking the earliest date instead would be just as wrong, because it would break the
extended-deadline behaviour this module has always had ("Deadline: 23 March" ... "Extended
deadline: 30 March" -> 30 March is real). The two cases pull in opposite directions, so
the rule cannot be a single min/max over all dates. It has to distinguish them:

    LABELLED   the text calls it a deadline. Authoritative, latest wins (extensions).
    UNLABELLED a bare window. Earliest END wins (later rows are downstream stages).

A labelled date always outranks an unlabelled one.

Two further defects fell out of the SAME cause and are covered below: the label's capture
was a flat 60-character run that included ".", so it read on into the next sentence. It
therefore (a) swallowed the following "Extended deadline" LABEL, which meant that label
never matched on its own and the documented extension behaviour silently never fired, and
(b) pulled unrelated dates from the next sentence into a labelled bucket, letting an
evaluation window outrank a real labelled deadline.

No network: every case here is plain text.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.scraper import _extract_deadline_from_text as extract   # noqa: E402

NEXT = date.today().year + 1


class AProcessCalendarTests(unittest.TestCase):
    """The shape that leaked. The submission row is FIRST; everything after it is a
    downstream stage that an applicant never has to meet."""

    CALENDAR = ("9 october to 7 november 2025: applications open. "
                "14 november 2025: confirmation of receipt. "
                "18 december 2025 to 23 january 2026: expert evaluation. "
                "february 2026: results announced.")

    def test_the_submission_window_end_is_returned(self):
        self.assertEqual(extract(self.CALENDAR), date(2025, 11, 7))

    def test_a_later_evaluation_milestone_is_not_returned(self):
        # The pre-fix answer, and the one that made an expired call look open.
        self.assertNotEqual(extract(self.CALENDAR), date(2026, 1, 23))

    def test_row_order_does_not_matter(self):
        rows = self.CALENDAR.split(". ")
        self.assertEqual(extract(". ".join(reversed(rows))), date(2025, 11, 7))


class AnExtendedDeadlineTests(unittest.TestCase):
    """Long-standing behaviour that must survive: when a funder extends, the later date
    is the real one. Both dates are LABELLED, so latest wins."""

    def test_the_extension_wins(self):
        self.assertEqual(
            extract(f"Deadline: 23 March {NEXT}. Extended deadline: 30 March {NEXT}."),
            date(NEXT, 3, 30))

    def test_the_extension_wins_in_lower_case_too(self):
        # Stored page text is not always original-case; the label match is case-insensitive
        # and the sentence-boundary rule must be too.
        self.assertEqual(
            extract(f"deadline: 23 march {NEXT}. extended deadline: 30 march {NEXT}."),
            date(NEXT, 3, 30))

    def test_the_extension_wins_across_a_newline(self):
        self.assertEqual(
            extract(f"Deadline: 23 March {NEXT}\nExtended deadline: 30 March {NEXT}"),
            date(NEXT, 3, 30))


class ALabelBeatsABareWindowTests(unittest.TestCase):
    def test_a_labelled_date_outranks_a_later_unlabelled_window(self):
        self.assertEqual(
            extract(f"Applications close on 1 June {NEXT}. "
                    f"Evaluation runs 1 July {NEXT} to 30 August {NEXT}."),
            date(NEXT, 6, 1))

    def test_a_bare_window_is_used_when_nothing_is_labelled(self):
        # A window needs an anchor word ("applications", "from", "open", "between") to be
        # read as one at all — a context-free pair of dates is deliberately ignored.
        self.assertEqual(extract(f"Applications from 1 June {NEXT} to 30 June {NEXT}"),
                         date(NEXT, 6, 30))

    def test_a_context_free_pair_of_dates_is_not_treated_as_a_window(self):
        self.assertIsNone(extract(f"1 June {NEXT} to 30 June {NEXT}"))


class TheLabelStopsAtASentenceBoundaryTests(unittest.TestCase):
    """The capture must not read into the next sentence — that is what caused both of the
    defects above. It must still tolerate the periods that appear INSIDE dates."""

    def test_an_abbreviated_month_is_not_cut_in_half(self):
        self.assertEqual(extract(f"Deadline: 15 Jan. {NEXT}"), date(NEXT, 1, 15))

    def test_a_period_in_following_prose_does_not_lose_the_date(self):
        self.assertEqual(extract(f"Deadline: 1 June {NEXT} for U.S. applicants only"),
                         date(NEXT, 6, 1))

    def test_a_second_unrelated_sentence_is_ignored(self):
        self.assertEqual(
            extract(f"Closing date: 15 May {NEXT}. The award period begins 1 "
                    f"September {NEXT} and runs for 36 months."),
            date(NEXT, 5, 15))


class TheOrdinaryPhrasingsStillWorkTests(unittest.TestCase):
    """Regression net over the label spellings real funders use."""

    def test_each_phrasing(self):
        for text, want in [
            (f"Closing date: 15 May {NEXT}", date(NEXT, 5, 15)),
            (f"Date Closed May 21, {NEXT}", date(NEXT, 5, 21)),          # no separator
            (f"Apply by 15 March {NEXT}", date(NEXT, 3, 15)),
            (f"Submission deadline: Tuesday, 16 December {NEXT} 1700HRS",
             date(NEXT, 12, 16)),                       # unparseable prefix, real date after
            (f"Applications must be submitted by {NEXT}-05-15", date(NEXT, 5, 15)),
        ]:
            with self.subTest(text=text):
                self.assertEqual(extract(text), want)

    def test_no_date_yields_none(self):
        self.assertIsNone(extract("Applications are accepted on a rolling basis."))

    def test_empty_text_yields_none(self):
        self.assertIsNone(extract(""))


class AnExplicitYearIsPreferredTests(unittest.TestCase):
    """A year-less phrase is defaulted to the current year by the parser, which can turn a
    PAST deadline into a spurious future one. It is only a fallback."""

    def test_a_dated_label_beats_a_year_less_one(self):
        self.assertEqual(extract(f"Deadline: 16 December. Closing date: 15 May {NEXT}"),
                         date(NEXT, 5, 15))

    def test_an_absurd_far_future_year_is_dropped(self):
        # A stray year in a strategy document, or a "36 months" -> 2036 misparse.
        self.assertIsNone(extract("Deadline: 15 May 2099"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
