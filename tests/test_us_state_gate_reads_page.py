"""The US-state gate could not see the page either.

`us_state_agency_funder` was added FROM a New York State AIDS Institute leak, and
the same funder leaked again: "RFP C043113 AIDS Intervention Management System",
funder "Health Research, Inc." - a pass-through research foundation whose name
names no state - with an EMPTY brief. So the text the gate searched was the title
plus the word "Global", and there was nothing for the pattern to match.

Isolated by moving the page's own text into `brief_description`, where
`_full_text` does look: the gate then returns True with the right reason. The
detector was never wrong. It was never shown the page.

`_full_text` is the shared cause across three gates now - the theme gate, the
closure detector, and this one.
"""
from __future__ import annotations

import unittest

import core.auto_scorer as A

POLICIES = {"countries": {"eligible": ["Cameroon", "Mali"], "broad_terms": []},
            "exclusions": {}}

# The permissible-contact block the owner quoted, as it appears on the page.
NY_PAGE = (
    "Calendar of Events Issuance of Request for Proposals: September 16, 2026. "
    "PERMISSIBLE SUBJECT MATTER CONTACT: Pursuant to State Finance Law 139-j(3)(a), "
    "the New York State Department of Health (hereinafter referred to as the "
    "“Department”) identifies the following allowable person to contact for "
    "communications related to the submission of written bids. New York State "
    "Department of Health, AIDS Institute, Office of Administration and Contract "
    "Management, Corning Tower, Albany, New York 12237."
)


def _cand(**kw):
    base = {"opportunity_title": "RFP C043113 AIDS Intervention Management System",
            "brief_description": "", "funding_agency": "Health Research, Inc.",
            "call_geographic_scope": ["Global"], "notes": "",
            "call_submission_deadline": "2026-11-05"}
    base.update(kw)
    return base


class TheReportedRow(unittest.TestCase):
    def test_the_page_names_the_state_agency_and_the_gate_now_fires(self):
        rejects, why = A.us_domestic_only_reject(_cand(_page_text=NY_PAGE), POLICIES)
        self.assertTrue(rejects)
        self.assertIn("US STATE government agency", why)

    def test_without_the_page_it_leaks_which_is_the_bug(self):
        # Precondition: the funder name alone names no state, and the brief is empty.
        self.assertFalse(A.us_state_agency_funder("Health Research, Inc."))
        self.assertEqual(A.us_domestic_only_reject(_cand(), POLICIES), (False, ""))

    def test_raw_text_is_read_too(self):
        rejects, _ = A.us_domestic_only_reject(_cand(raw_text=NY_PAGE), POLICIES)
        self.assertTrue(rejects)

    def test_a_truncated_fragment_cannot_fire(self):
        """Honest about the limit: the live store holds 403 chars of this page,
        a Calendar-of-Events fragment that never names the Department. The fix
        is correct and inert until the page itself is captured."""
        fragment = NY_PAGE[:60]
        self.assertNotIn("New York State", fragment)
        self.assertEqual(
            A.us_domestic_only_reject(_cand(_page_text=fragment), POLICIES), (False, ""))


class TheExistingGuardsStillStandDownFirst(unittest.TestCase):
    def test_an_explicit_international_eligibility_statement_wins(self):
        """The point of this test is that the STAND-DOWN reads the page too.

        Widening only the reject arm would be worse than not widening at all: a
        call that welcomes any country on its page would be dropped because a
        state agency is named on the same page. The wording here is one the
        existing detector recognises, so this tests the plumbing and not its
        vocabulary - which is separately narrow ("foreign entities may apply"
        does not match, and that is a pre-existing gap, not touched here).
        """
        page = NY_PAGE + " This call is open to applicants from any country."
        self.assertTrue(A._has_inclusive_eligibility(page), "precondition")
        rejects, _ = A.us_domestic_only_reject(_cand(_page_text=page), POLICIES)
        self.assertFalse(rejects,
                         "an inclusive eligibility statement must stand the gate down")

    def test_a_us_deployment_is_unaffected(self):
        """Controlled by the `org_is_us_entity` SETTING, not by policies - the gate
        reads it from core.settings, so that is what has to be stubbed."""
        from core import settings as S
        orig = S.get_setting
        S.get_setting = lambda key, default=None: (
            "true" if key == "org_is_us_entity" else orig(key, default))
        try:
            rejects, _ = A.us_domestic_only_reject(_cand(_page_text=NY_PAGE), POLICIES)
        finally:
            S.get_setting = orig
        self.assertFalse(rejects, "a US org is eligible for a US state call")


class NoFalsePositivesFromAPassingMention(unittest.TestCase):
    """The pattern needs a state name adjacent to "state" plus an agency word, so
    a page cannot trip it by naming a place."""

    PAGES = (
        "Partners include universities in New York, California and Texas.",
        "The grantee may work in any state of the United States.",
        "Our New York office coordinates the programme.",
        "Applicants from Washington are welcome, as are all others.",
        "This is a global call. Previous grantees were based in Georgia and Ohio.",
    )

    def test_none_of_them_rejects(self):
        for page in self.PAGES:
            with self.subTest(page=page[:44]):
                rejects, why = A.us_domestic_only_reject(
                    _cand(_page_text=page), POLICIES)
                self.assertFalse(rejects, f"{page!r} -> {why}")

    def test_blank_page_text(self):
        for val in (None, "", "   "):
            with self.subTest(val=val):
                self.assertEqual(
                    A.us_domestic_only_reject(_cand(_page_text=val), POLICIES),
                    (False, ""))


class TheDetectorItselfIsUnchanged(unittest.TestCase):
    def test_it_still_matches_a_state_agency(self):
        for text in ("New York State Department of Health",
                     "State of California Department of Public Health",
                     "New York State AIDS Institute"):
            with self.subTest(text=text):
                self.assertTrue(A.us_state_agency_funder(text))

    def test_it_still_ignores_us_federal_agencies(self):
        # Narrow on purpose: federal calls are handled by their own rules, and
        # some of them do admit foreign applicants.
        for text in ("National Institutes of Health", "USAID",
                     "Centers for Disease Control and Prevention",
                     "U.S. Department of State"):
            with self.subTest(text=text):
                self.assertFalse(A.us_state_agency_funder(text))


if __name__ == "__main__":
    unittest.main()
