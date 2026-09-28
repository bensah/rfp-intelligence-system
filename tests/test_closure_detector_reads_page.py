"""The closure detector had never read the page.

`closed_call_hard_reject` searched `_full_text` - title + brief + scope + funder -
which is roughly 1000 characters of synthesised summary. Donors put the closure
notice in the page body.

Proven on the MalariaGEN procurement grant, whose page says BOTH "now closed" and
"no longer accepting applications": with `_page_text` populated, the function
still returned `(False, '')`, because it was not looking there.

Reading the page is safe HERE in a way it was not for the theme gate, whose
page-text rescue had to route through the LLM judge. The required-theme list
contains bare words like "health", so a long page matches incidentally; these
closure phrases are whole clauses that only appear when someone means them.

The one real risk is a page advertising an OPEN round while recounting a closed
one, so a match found only in the page body stands down to a still-future
deadline whatever its strength, while a match in the narrow fields keeps its old
behaviour exactly.
"""
from __future__ import annotations

import unittest

import core.auto_scorer as A


def _cand(**kw):
    base = {"opportunity_title": "Procurement grants call", "brief_description": "",
            "funding_agency": "A Funder", "call_geographic_scope": [],
            "call_submission_deadline": None}
    base.update(kw)
    return base


# The sentence that was sitting unread.
CLOSED_PAGE = (
    "MalariaGEN and BMGF Call for Applications: Procurement grants. This funding "
    "opportunity supports laboratory equipment specialists in malaria-endemic "
    "settings. Update: The procurement grant is now closed and we are no longer "
    "accepting applications. For future opportunities please see our resources page."
)


class ThePageIsRead(unittest.TestCase):
    def test_a_closure_notice_in_the_page_body_rejects(self):
        rejects, why = A.closed_call_hard_reject(_cand(_page_text=CLOSED_PAGE))
        self.assertTrue(rejects)
        self.assertIn("on the page", why)

    def test_raw_text_is_read_too(self):
        # The store column is raw_text; the pipeline candidate uses _page_text.
        rejects, _ = A.closed_call_hard_reject(_cand(raw_text=CLOSED_PAGE))
        self.assertTrue(rejects)

    def test_the_same_candidate_without_the_page_is_kept(self):
        # Precondition, and the exact bug: nothing in the narrow fields says closed.
        self.assertEqual(A.closed_call_hard_reject(_cand()), (False, ""))

    def test_an_open_page_is_untouched(self):
        page = ("Applications are invited for the 2027 round. The deadline for "
                "submission is 30 June 2027. Grants of up to USD 200,000.")
        self.assertEqual(A.closed_call_hard_reject(_cand(_page_text=page)), (False, ""))


class AFutureDeadlineOverridesAPageOnlyMatch(unittest.TestCase):
    """A long page can recount a past round while a new one is open."""

    def test_a_future_deadline_keeps_the_call(self):
        rejects, _ = A.closed_call_hard_reject(
            _cand(_page_text=CLOSED_PAGE, call_submission_deadline="2027-06-30"))
        self.assertFalse(rejects)

    def test_a_past_deadline_does_not_rescue_it(self):
        rejects, _ = A.closed_call_hard_reject(
            _cand(_page_text=CLOSED_PAGE, call_submission_deadline="2020-06-30"))
        self.assertTrue(rejects)

    def test_no_deadline_at_all_is_the_leak_case_and_rejects(self):
        # Undated rows are exactly what was reaching the review week.
        rejects, _ = A.closed_call_hard_reject(_cand(_page_text=CLOSED_PAGE))
        self.assertTrue(rejects)


class NarrowFieldBehaviourIsUnchanged(unittest.TestCase):
    """Strong prose in the summary must still reject regardless of any date.

    This is the pre-existing contract (see test_euft_twostage) and the change must
    not relax it: only page-only matches get the new override.
    """

    def test_strong_prose_in_the_brief_rejects_despite_a_future_deadline(self):
        rejects, why = A.closed_call_hard_reject(_cand(
            brief_description="This fund is no longer accepting applications.",
            call_submission_deadline="2027-06-30"))
        self.assertTrue(rejects)
        self.assertIn("in the summary", why)

    def test_a_soft_badge_in_the_brief_still_stands_down_to_a_future_deadline(self):
        rejects, _ = A.closed_call_hard_reject(_cand(
            brief_description="Status: closed", call_submission_deadline="2027-06-30"))
        self.assertFalse(rejects)

    def test_a_soft_badge_with_no_future_deadline_still_rejects(self):
        rejects, _ = A.closed_call_hard_reject(_cand(brief_description="now closed"))
        self.assertTrue(rejects)

    def test_the_narrow_fields_win_the_reason_line(self):
        # When both places match, the reason should name the summary, because that
        # match is the one that keeps the stricter old rule.
        rejects, why = A.closed_call_hard_reject(_cand(
            brief_description="applications are now closed", _page_text=CLOSED_PAGE))
        self.assertTrue(rejects)
        self.assertIn("in the summary", why)


class PortalStatusIsUnchanged(unittest.TestCase):
    def test_portal_closed_still_rejects(self):
        rejects, why = A.closed_call_hard_reject(_cand(_closed=True))
        self.assertTrue(rejects)
        self.assertIn("portal status", why)

    def test_portal_closed_with_a_future_stage_two_deadline_survives(self):
        rejects, _ = A.closed_call_hard_reject(
            _cand(_closed=True, call_submission_deadline="2027-06-30"))
        self.assertFalse(rejects)


class NoFalsePositivesOnOrdinaryProse(unittest.TestCase):
    """A long page must not reject on incidental words."""

    PAGES = (
        "We fund closed-loop water systems and closed-circuit diagnostics.",
        "The clinic operates a closed formulary. Applications open 1 March 2027.",
        "Deadline: 30 June 2027. Late submissions will not be accepted.",
        "This is a closed-ended fund with a fixed corpus.",
        "Our offices are closed on public holidays.",
    )

    def test_none_of_them_reads_as_a_closed_call(self):
        for page in self.PAGES:
            with self.subTest(page=page[:44]):
                rejects, why = A.closed_call_hard_reject(_cand(_page_text=page))
                self.assertFalse(rejects, f"{page!r} -> {why}")

    def test_blank_and_missing_page_text(self):
        for val in (None, "", "   "):
            with self.subTest(val=val):
                self.assertEqual(
                    A.closed_call_hard_reject(_cand(_page_text=val)), (False, ""))


if __name__ == "__main__":
    unittest.main()
