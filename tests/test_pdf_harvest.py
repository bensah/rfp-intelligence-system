"""Application/RFI-document PDF discovery for the thin-page full-PDF harvest.

A teaser landing page (e.g. the MMV market-intel RFI: a table of contents plus a
"Request for information instructions" PDF link) must resolve to that document PDF so
the enricher can harvest its full body. The full-body harvest uses require_keyword=True
so it follows a genuine RFP-document link and never a stray logo / annual-report PDF.
"""
import unittest

from bs4 import BeautifulSoup

from core.scraper import _find_application_pdf

BASE = "https://www.mmv.org/partnering-opportunities/request-information-market-intelligence"


class TestFindApplicationPdf(unittest.TestCase):
    def test_finds_keyworded_rfi_instructions_pdf(self):
        html = (
            '<a href="/logo.pdf">Logo</a>'
            '<a href="/sites/default/files/content/document/MMV_2026_RFI_Market_Intel_vF.pdf">'
            'Request for information instructions</a>'
        )
        soup = BeautifulSoup(html, "html.parser")
        got = _find_application_pdf(soup, BASE, require_keyword=True)
        self.assertIsNotNone(got)
        self.assertTrue(got.endswith("MMV_2026_RFI_Market_Intel_vF.pdf"))

    def test_require_keyword_skips_non_relevant_pdf(self):
        # Only a bare, non-guide PDF on the page → the harvest must NOT follow it.
        soup = BeautifulSoup('<a href="/annual-report-2025.pdf">Annual report</a>',
                             "html.parser")
        self.assertIsNone(_find_application_pdf(soup, BASE, require_keyword=True))

    def test_default_still_falls_back_to_first_pdf(self):
        # The deadline-follow path (require_keyword=False) keeps its first-PDF fallback.
        soup = BeautifulSoup('<a href="/annual-report-2025.pdf">Annual report</a>',
                             "html.parser")
        got = _find_application_pdf(soup, BASE)
        self.assertIsNotNone(got)
        self.assertTrue(got.endswith("annual-report-2025.pdf"))

    def test_no_pdf_returns_none(self):
        soup = BeautifulSoup('<a href="/about">About us</a>', "html.parser")
        self.assertIsNone(_find_application_pdf(soup, BASE, require_keyword=True))


if __name__ == "__main__":
    unittest.main()
