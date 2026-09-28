"""Listing / index pages must not be screened as single calls.

`https://www.grandchallenges.org/grant-opportunities` was stored as an
opportunity with a 2027-01-09 deadline. Both listing detectors were blind to it:

  * `_LISTING_TITLE_RE` is anchored ^...$ and matched "Grant Opportunities"
    exactly - but the page's <title> is "Grant Opportunities | Grand
    Challenges", and the site-name suffix defeated the anchor.
  * `_LISTING_URL_RE` only knew markers a site chooses to add (/list, /all,
    ?page=2). A donor whose index simply lives at /grant-opportunities has none.

Across all 246 auto-scanned rows in the pipeline, is_index_page() fired on
ZERO. The row looked plausible because the synthesis wrote a brief from the
FIRST item on the index and took that item's deadline.

The machinery behind the detector was already right: an index is crawled for
its child calls and the index itself is then rejected. Only the detector was
blind, so these tests are about what it recognises.
"""
from __future__ import annotations

import unittest

from core.auto_scorer import is_index_page, is_listing_title, strip_site_suffix


class TheReportedRow(unittest.TestCase):
    def test_the_grand_challenges_index_is_detected(self):
        self.assertTrue(is_index_page({
            "opportunity_link": "https://www.grandchallenges.org/grant-opportunities",
            "opportunity_title": "Grant Opportunities | Grand Challenges",
        }))

    def test_it_is_caught_by_the_url_alone(self):
        # Belt and braces: the URL rule must stand on its own, because a scraper
        # that captured no title would otherwise still let the index through.
        self.assertTrue(is_index_page({
            "opportunity_link": "https://www.grandchallenges.org/grant-opportunities",
            "opportunity_title": "",
        }))

    def test_it_is_caught_by_the_title_alone(self):
        self.assertTrue(is_listing_title("Grant Opportunities | Grand Challenges"))


class SiteSuffixStripping(unittest.TestCase):
    """The tail is removed only when it looks like a site name."""

    def test_a_site_name_is_removed(self):
        for title, want in (
            ("Grant Opportunities | Grand Challenges", "Grant Opportunities"),
            ("Funding Opportunities | Wellcome", "Funding Opportunities"),
            ("Open calls - Some Funder", "Open calls"),
            ("Current funding opportunities » Research Council",
             "Current funding opportunities"),
            ("Apply | The Audacious Project", "Apply"),
        ):
            with self.subTest(title=title):
                self.assertEqual(strip_site_suffix(title), want)

    def test_the_last_separator_wins_not_the_first(self):
        # The scraper's old local rule split on the FIRST separator and lost the
        # subject of the call. The site name is at the END.
        self.assertEqual(
            strip_site_suffix("Malaria | Drug Discovery | Medicines Venture"),
            "Malaria | Drug Discovery")

    def test_a_colon_is_never_a_site_separator(self):
        # "Call for Proposals: Daylight Research Grant Program" would reduce to
        # "Call for Proposals", which the listing regex matches - and a genuine
        # call would be rejected as an index.
        t = "Call for Proposals: Daylight Research Grant Program"
        self.assertEqual(strip_site_suffix(t), t)
        self.assertFalse(is_listing_title(t))

    def test_a_tail_with_a_digit_is_kept(self):
        # A year or a notice number means the tail is still the call.
        t = "Grand Challenges India 2026 | Call for Proposals"
        self.assertEqual(strip_site_suffix(t), t)
        self.assertFalse(is_listing_title(t))

    def test_a_tail_carrying_call_vocabulary_is_kept(self):
        for t in ("Malaria drug discovery - 9th African call for proposals",
                  "Funding opportunities - Malaria vaccine 2026 call",
                  "Health research – request for applications"):
            with self.subTest(t=t):
                self.assertEqual(strip_site_suffix(t), t)

    def test_a_long_tail_is_not_a_site_name(self):
        t = ("Water access | a programme supporting community managed rural "
             "supply schemes across the region")
        self.assertEqual(strip_site_suffix(t), t)

    def test_hyphenated_words_survive(self):
        # The dash separators are SPACED, so a hyphenated word is not a cut point.
        for t in ("Cost-Disruptive Tools for Diagnosis and Screening",
                  "Innovations in Cost-Disruptive Tools"):
            with self.subTest(t=t):
                self.assertEqual(strip_site_suffix(t), t)

    def test_blank_and_separator_only_input(self):
        for t in (None, "", "   ", "|", " - ", "| Grand Challenges"):
            with self.subTest(t=t):
                strip_site_suffix(t)          # must not raise
        self.assertEqual(strip_site_suffix(None), "")


class SectionPathsAreIndexes(unittest.TestCase):
    """A listing noun as the LAST path segment, with no call slug after it."""

    INDEX = (
        "https://www.grandchallenges.org/grant-opportunities",
        "https://example.org/funding-opportunities/",
        "https://example.org/opportunities?page=1",
        "https://example.org/grants",
        "https://example.org/tenders/",
        "https://idrc-crdi.ca/en/funding",
        "https://example.org/calls",
        "https://www.enabel.be/grants/?in_country=all&is_status=0",
    )
    NOT_INDEX = (
        # A real call BENEATH a section: the noun is not the last segment.
        "https://idrc-crdi.ca/en/funding/supporting-stisa-2034-sgci-multilateral",
        "https://wellcome.org/grant-funding/schemes/some-scheme",
        "https://gcgh.grandchallenges.org/challenge/estimating-global-burden",
        "https://example.org/grants/my-specific-call-2026",
        "https://example.org/opportunities/water-access-innovation-fund",
        "https://grants.gov/search-results-detail/358921",
    )

    def test_section_paths_are_flagged(self):
        for u in self.INDEX:
            with self.subTest(url=u):
                self.assertTrue(is_index_page({"opportunity_link": u,
                                               "opportunity_title": ""}), u)

    def test_a_call_beneath_a_section_is_not_flagged(self):
        for u in self.NOT_INDEX:
            with self.subTest(url=u):
                self.assertFalse(is_index_page({"opportunity_link": u,
                                                "opportunity_title": ""}), u)


class RealCallsAreNotSweptUp(unittest.TestCase):
    """Titles of calls that are actually in the pipeline must stay screenable.

    This is the direction that costs us money: a false positive here silently
    drops a genuine opportunity.
    """

    TITLES = (
        "Innovations in Cost-Disruptive Tools for Diagnosis and Screening",
        "Innovations in Cost-Disruptive Tools for Diagnosis and Screening (South Africa)",
        "Grand challenge - Estimating the Global Burden of Diarrheal Diseases",
        "Supporting STISA 2034: SGCI Multilateral Research Call advancing "
        "Africa's Science, Technology and Innovation priorities",
        "Addressing Neglected Areas of SRHR in Sub-Saharan Africa (Letters of Interest)",
        "AI-Enabled Consumer Engagement to Advance Family Planning",
        "Wellcome Snakebite Innovation Prize",
        "The Pandemic Fund Call for Proposals (2027)",
    )

    def test_none_of_them_reads_as_a_listing_heading(self):
        for t in self.TITLES:
            with self.subTest(title=t):
                self.assertFalse(is_listing_title(t), t)


class GenericHeadingsAreCaught(unittest.TestCase):
    HEADINGS = (
        "Grant Opportunities | Grand Challenges",
        "Funding opportunities",
        "Current funding opportunities » Research Council",
        "Open calls",
        "Calls for proposals",
        "Call for proposals - ahpsr",
        "Find a funding opportunity | Health Agency",
        "All grants",
        "Browse opportunities | A Funder",
    )

    def test_each_is_a_listing_heading(self):
        for t in self.HEADINGS:
            with self.subTest(title=t):
                self.assertTrue(is_listing_title(t), t)


if __name__ == "__main__":
    unittest.main()
