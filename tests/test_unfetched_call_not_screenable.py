"""A search snippet is not a call page.

Web-search discovery builds a candidate from a provider's ~160 characters, guesses
a funder from the domain, and the pipeline then tries to fetch the real page. When
that fetch failed - silently, on every host that refused our missing User-Agent -
the snippet was all we ever held, and nothing downstream said so.

What made it invisible: the synthesis expanded the 160-char snippet into a
1000-char brief, so the row arrived with prose that reads like a real opportunity.
It even satisfied the blank-stub branch, whose text length is measured on the
brief. Generated content was answering "did we manage to read this call?".

Measured on the live store, search-discovered rows whose fetched text stayed
snippet-sized: 11, EVERY one with no deadline, and of the 8 that reached a tenant
pipeline ALL 8 were auto-scored Decline. Six sat in one review week, including
four the owner reported by hand. Their funders read "Mesamalaria", "Errin",
"International", "Ahpsr" - domain brand words, because no page was read to find
the real one.

Keyed on PROVENANCE plus fetched length, never length alone: a structured feed
legitimately has no page text at all (EU TED: 50 of 50 rows with zero raw_text,
deadlines and values straight from the API) and must not be touched.
"""
from __future__ import annotations

import unittest

import core.auto_scorer as A

SEARCH = "\U0001f50e Web search — discovery"
# A real provider snippet, ellipsis and all, from the live store.
SNIPPET = ("ARNTD Call for Proposals: African Researchers' Small Grants Program "
           "(SGP IV) ... NIHR Call for Applications: NIHR Biomedical Research "
           "Centres (BRCs). Grants.")
# What the synthesis wrote from it.
INVENTED_BRIEF = (
    "The African Researchers' Small Grants Program (SGP IV) is a funding "
    "opportunity announced by ARNTD and supported by the Mesamalaria funder. It "
    "seeks to enable African-based researchers to develop and implement "
    "small-scale research projects addressing neglected tropical diseases, with "
    "mentorship and capacity strengthening built into the award." * 3)


def _cand(**kw):
    base = {"opportunity_title": "ARNTD Call for Proposals",
            "funding_agency": "Mesamalaria", "brief_description": "",
            "call_geographic_scope": [], "call_domain_areas": [],
            "call_submission_deadline": None, "_source_origin": SEARCH}
    base.update(kw)
    return base


class TheReportedRows(unittest.TestCase):
    def test_a_snippet_only_search_hit_is_not_screenable(self):
        bad, why = A.insufficient_data_reject(_cand(_page_text=SNIPPET))
        self.assertTrue(bad)
        self.assertIn("never read", why)

    def test_the_invented_brief_does_not_rescue_it(self):
        """The exact bug: generated prose satisfying a completeness gate."""
        bad, why = A.insufficient_data_reject(
            _cand(_page_text=SNIPPET, brief_description=INVENTED_BRIEF))
        self.assertTrue(bad, "a 1000-char synthesis must not count as having read the page")
        self.assertIn("never read", why)

    def test_the_brief_alone_is_not_evidence_the_page_was_read(self):
        self.assertFalse(A._page_was_fetched(
            {"brief_description": INVENTED_BRIEF, "_page_text": SNIPPET}))


class RealPageTextIsKept(unittest.TestCase):
    """The direction that costs a genuine call.

    Lengths taken from the live store rows that must survive: 478, 892, 1110,
    1499 and 15291 characters of genuinely fetched text.
    """

    def test_a_fetched_page_survives_even_with_no_deadline(self):
        for n in (478, 892, 1110, 1499, 15291):
            with self.subTest(chars=n):
                bad, why = A.insufficient_data_reject(_cand(_page_text="x " * (n // 2)))
                self.assertFalse(bad, f"{n} chars of real page text -> {why}")

    def test_the_ceiling_is_the_boundary(self):
        self.assertTrue(A.insufficient_data_reject(
            _cand(_page_text="x" * (A._SNIPPET_CEILING - 1)))[0])
        self.assertFalse(A.insufficient_data_reject(
            _cand(_page_text="x" * A._SNIPPET_CEILING))[0])

    def test_raw_text_counts_as_fetched_too(self):
        self.assertTrue(A._page_was_fetched({"raw_text": "y" * 1200}))


class OnlyTheSearchChannel(unittest.TestCase):
    """A structured feed has no page text by design and must be untouched."""

    STRUCTURED = (
        "Tenders Electronic Daily (EU TED) — donor catalog",
        "United Nations Global Marketplace — donor catalog",
        "United States Federal Government — donor catalog (kw='HIV')",
        "Canadian Institutes of Health Research (ResearchNet) — donor catalog (RSS)",
        "UK Find a Tender Service — donor catalog",
    )

    def test_a_structured_source_with_no_page_text_is_not_rejected_by_this_rule(self):
        for src in self.STRUCTURED:
            with self.subTest(source=src):
                flagged, _ = A.discovered_but_never_read(
                    {"_source_origin": src, "_page_text": ""})
                self.assertFalse(flagged, src)

    def test_ted_style_row_with_zero_page_text_but_real_metadata_survives(self):
        # 50 of 50 EU TED store rows carry zero raw_text.
        bad, why = A.insufficient_data_reject({
            "_source_origin": "Tenders Electronic Daily (EU TED) — donor catalog",
            "_page_text": "", "brief_description": "",
            "call_submission_deadline": "2027-03-01",
            "call_geographic_scope": ["Romania"], "call_domain_areas": ["Health"],
            "opportunity_title": "Supply of laboratory reagents"})
        self.assertFalse(bad, why)

    def test_a_google_alert_is_the_same_channel(self):
        flagged, _ = A.discovered_but_never_read(
            {"_source_origin": "Google Alert", "_page_text": SNIPPET})
        self.assertTrue(flagged)

    def test_the_source_field_is_read_when_source_origin_is_absent(self):
        flagged, _ = A.discovered_but_never_read(
            {"source": SEARCH, "_page_text": SNIPPET})
        self.assertTrue(flagged)


class ADeadlineIsIndependentEvidence(unittest.TestCase):
    """A snippet that carried a real closing date is not a blind guess."""

    def test_a_snippet_hit_with_a_deadline_is_kept(self):
        bad, _ = A.insufficient_data_reject(
            _cand(_page_text=SNIPPET, call_submission_deadline="2027-06-30"))
        self.assertFalse(bad)

    def test_the_helper_still_reports_it_as_unread(self):
        # The fact is unchanged - it is the GATE that defers to a deadline, so the
        # synthesis skip still applies and no brief is invented.
        flagged, _ = A.discovered_but_never_read(
            _cand(_page_text=SNIPPET, call_submission_deadline="2027-06-30"))
        self.assertTrue(flagged)


class NoBriefIsInventedFromASnippet(unittest.TestCase):
    def test_extract_skips_synthesis_for_an_unread_search_hit(self):
        import inspect

        import core.extract as E
        src = inspect.getsource(E.build_record if hasattr(E, "build_record") else E)
        self.assertIn("discovered_but_never_read", src,
                      "the synthesis gate must consult the unread-page check")


class Robustness(unittest.TestCase):
    def test_missing_provenance_is_not_flagged(self):
        self.assertEqual(A.discovered_but_never_read({}), (False, ""))

    def test_none_page_text(self):
        flagged, _ = A.discovered_but_never_read(
            {"_source_origin": SEARCH, "_page_text": None, "raw_text": None})
        self.assertTrue(flagged)

    def test_whitespace_only_page_text_is_not_fetched(self):
        self.assertFalse(A._page_was_fetched({"_page_text": "   \n  " * 200}))


if __name__ == "__main__":
    unittest.main()
