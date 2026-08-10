"""One opportunity resolved from either store, and turned into a trackable candidate.

The Live Opportunity Feed linked every title to the bare `/pipelines` page, so the click
told you nothing. And the Featured card ranks the SHARED catalogue, whose calls live in
`extracted_solicitations` and are not in `rfp_submissions` at all — no page in the app
could show one, let alone offer to score it.
"""
from __future__ import annotations

import unittest

from core import opportunity_detail as od

PIPELINE_ROW = {
    "uid": "BN-260808-1155",
    "opportunity_title": "Cold Chain Equipment Subgrant",
    "funding_agency": "A Health Funder",
    "call_award_value": 574676.0,
    "currency": "USD",
    "call_submission_deadline": "2026-08-19",
    "call_geographic_scope": ["Countryland", "Otherland"],
    "call_domain_areas": ["Vaccines"],
    "brief_description": "Support for cold chain equipment.",
    "opportunity_link": "https://funder.example/call/1",
    "alignment_score": 95.5,
    "auto_recommendation": "Proceed",
    "decision": "Proceed",
    "instrument_type": None,          # blank → must be dropped from the layout
    "key_risks": "",                  # blank
}

CATALOG_ROW = {
    "uid": "ES-99",
    "opportunity_name": "Vaccine Delivery Innovation Fund",
    "funder_name": "Another Funder",
    "opportunity_url": "https://another.example/opportunity/7",
    "apply_url": "https://another.example/apply/7",
    "deadline": "2026-10-01T00:00:00",
    "date_posted": "2026-07-01",
    "grant_amount": 250000,
    "currency": "USD",
    "call_geographic_scope": ["Countryland"],
    "call_domain_areas": ["Vaccines", "Health systems"],
    "focus_themes": ["Immunisation"],
    "brief_description": "Short version.",
    "full_description": "The much longer extracted description of the call.",
    "instrument_type": "Grant",
    "project_duration": 24,
    "what_is_funded": "Delivery innovations.",
    "what_is_not_funded": "Construction.",
    "eligibility_applicant_types": ["NGO"],
    "extraction_confidence": 0.82,
    "solicitation_type": "RFP",
    "resource_links": ["https://another.example/guidelines.pdf"],
    "eligibility_other": None,        # blank → dropped
}


def _reader(row):
    return lambda uid: row if row and row.get("uid") == uid else None


class TestResolvingAUid(unittest.TestCase):
    def test_a_pipeline_uid_resolves_to_the_pipeline_row(self):
        res = od.load("BN-260808-1155", pipeline_reader=_reader(PIPELINE_ROW),
                      catalog_reader=_reader(CATALOG_ROW))
        self.assertEqual(res["kind"], od.KIND_PIPELINE)
        self.assertEqual(res["row"]["uid"], "BN-260808-1155")

    def test_a_catalogue_uid_resolves_to_the_catalogue_row(self):
        res = od.load("ES-99", pipeline_reader=_reader(PIPELINE_ROW),
                      catalog_reader=_reader(CATALOG_ROW))
        self.assertEqual(res["kind"], od.KIND_CATALOG)
        self.assertEqual(res["row"]["uid"], "ES-99")

    def test_the_pipeline_wins_when_both_stores_have_the_uid(self):
        # Once a catalogue call is tracked, the tenant's own screened row is the more
        # informative answer.
        both = dict(CATALOG_ROW, uid="SHARED")
        res = od.load("SHARED", pipeline_reader=_reader(dict(PIPELINE_ROW, uid="SHARED")),
                      catalog_reader=_reader(both))
        self.assertEqual(res["kind"], od.KIND_PIPELINE)

    def test_an_unknown_uid_resolves_to_nothing(self):
        res = od.load("NOPE", pipeline_reader=_reader(PIPELINE_ROW),
                      catalog_reader=_reader(CATALOG_ROW))
        self.assertEqual(res, {"kind": None, "row": None})

    def test_a_blank_uid_resolves_to_nothing(self):
        for uid in ("", "   ", None):
            self.assertEqual(od.load(uid, pipeline_reader=_reader(PIPELINE_ROW)),
                             {"kind": None, "row": None})

    def test_a_reader_that_raises_does_not_break_the_page(self):
        def boom(uid):
            raise RuntimeError("db down")
        res = od.load("ES-99", pipeline_reader=boom, catalog_reader=_reader(CATALOG_ROW))
        self.assertEqual(res["kind"], od.KIND_CATALOG)


class TestTheDetailLayout(unittest.TestCase):
    def test_blank_fields_are_dropped(self):
        secs = dict((t, dict(f)) for t, f in od.sections(od.KIND_PIPELINE, PIPELINE_ROW))
        self.assertNotIn("Instrument", secs.get("The call", {}))     # None
        self.assertNotIn("Key risks", secs.get("Eligibility & compliance", {}))  # ""

    def test_empty_sections_are_dropped_entirely(self):
        titles = [t for t, _ in od.sections(od.KIND_CATALOG, {"uid": "x"})]
        self.assertEqual(titles, [])

    def test_the_catalogue_layout_surfaces_the_extraction_detail(self):
        secs = dict((t, dict(f)) for t, f in od.sections(od.KIND_CATALOG, CATALOG_ROW))
        self.assertEqual(secs["Scope"]["What is funded"], "Delivery innovations.")
        self.assertEqual(secs["Scope"]["What is NOT funded"], "Construction.")
        self.assertEqual(secs["Eligibility"]["Applicant types"], ["NGO"])
        self.assertEqual(secs["The award"]["Value"], 250000)
        self.assertNotIn("Other requirements", secs["Eligibility"])   # None

    def test_the_pipeline_layout_surfaces_the_screening_state(self):
        secs = dict((t, dict(f)) for t, f in od.sections(od.KIND_PIPELINE, PIPELINE_ROW))
        self.assertEqual(secs["Screening"]["Bid Strength"], 95.5)
        self.assertEqual(secs["Screening"]["Team decision"], "Proceed")

    def test_placeholder_strings_count_as_blank(self):
        row = {"uid": "x", "funder_name": "Not stated", "instrument_type": "n/a",
               "opportunity_type": "unknown", "agency_code": "  "}
        self.assertEqual(od.sections(od.KIND_CATALOG, row), [])


class TestTitleLinkAndNarrative(unittest.TestCase):
    def test_title_reads_the_right_column_per_store(self):
        self.assertEqual(od.title_of(od.KIND_CATALOG, CATALOG_ROW),
                         "Vaccine Delivery Innovation Fund")
        self.assertEqual(od.title_of(od.KIND_PIPELINE, PIPELINE_ROW),
                         "Cold Chain Equipment Subgrant")

    def test_an_untitled_opportunity_still_renders(self):
        self.assertEqual(od.title_of(od.KIND_CATALOG, {}), "(untitled opportunity)")

    def test_link_falls_back_to_the_apply_url(self):
        self.assertEqual(od.link_of(od.KIND_CATALOG, CATALOG_ROW),
                         "https://another.example/opportunity/7")
        self.assertEqual(
            od.link_of(od.KIND_CATALOG, {"apply_url": "https://x.example/a"}),
            "https://x.example/a")

    def test_the_narrative_prefers_the_fuller_text(self):
        self.assertEqual(od.narrative_of(od.KIND_CATALOG, CATALOG_ROW),
                         "The much longer extracted description of the call.")
        self.assertEqual(
            od.narrative_of(od.KIND_CATALOG, {"brief_description": "Only this."}),
            "Only this.")


class TestTurningACatalogueRowIntoATrackableCandidate(unittest.TestCase):
    def test_the_catalogue_column_names_are_mapped_to_the_pipeline_ones(self):
        cand = od.to_candidate(CATALOG_ROW)
        self.assertEqual(cand["opportunity_title"], "Vaccine Delivery Innovation Fund")
        self.assertEqual(cand["funding_agency"], "Another Funder")
        self.assertEqual(cand["opportunity_link"],
                         "https://another.example/opportunity/7")
        self.assertEqual(cand["call_submission_deadline"], "2026-10-01")
        self.assertEqual(cand["call_award_value"], 250000)

    def test_the_extraction_is_carried_not_thrown_away(self):
        # The crawl already paid for geography, domains and duration; re-deriving them
        # from a title would lose them.
        cand = od.to_candidate(CATALOG_ROW)
        self.assertEqual(cand["call_geographic_scope"], ["Countryland"])
        self.assertEqual(cand["call_domain_areas"], ["Vaccines", "Health systems"])
        self.assertEqual(cand["project_duration"], 24)
        self.assertEqual(cand["instrument_type"], "Grant")

    def test_domain_areas_fall_back_to_focus_themes(self):
        row = dict(CATALOG_ROW)
        row.pop("call_domain_areas")
        self.assertEqual(od.to_candidate(row)["call_domain_areas"], ["Immunisation"])

    def test_blank_values_are_omitted_so_they_never_overwrite_with_nothing(self):
        cand = od.to_candidate({"opportunity_name": "T", "funder_name": "",
                                "grant_amount": None, "deadline": None})
        self.assertEqual(set(cand), {"opportunity_title"})

    def test_a_title_is_capped_for_the_column(self):
        cand = od.to_candidate({"opportunity_name": "x" * 400})
        self.assertEqual(len(cand["opportunity_title"]), 300)


class TestAlreadyTrackedDetection(unittest.TestCase):
    """Tracking mints a NEW uid, so the page must not offer to track a second time."""

    def test_a_matching_call_url_finds_the_tenants_own_row(self):
        mine = [{"uid": "AA-1", "opportunity_link": "https://other.example/x"},
                {"uid": "BB-2",
                 "opportunity_link": "https://another.example/opportunity/7/"}]
        self.assertEqual(od.tracked_uid(CATALOG_ROW, mine), "BB-2")

    def test_no_match_returns_none(self):
        self.assertIsNone(od.tracked_uid(
            CATALOG_ROW, [{"uid": "AA-1", "opportunity_link": "https://nope.example"}]))

    def test_a_catalogue_row_with_no_url_cannot_be_matched(self):
        self.assertIsNone(od.tracked_uid({"opportunity_name": "T"}, [{"uid": "A"}]))

    def test_an_empty_pipeline_returns_none(self):
        self.assertIsNone(od.tracked_uid(CATALOG_ROW, []))
        self.assertIsNone(od.tracked_uid(CATALOG_ROW, None))

    def test_link_comparison_ignores_case_and_trailing_slash(self):
        self.assertEqual(od.normalise_link("HTTPS://X.example/A/"),
                         "https://x.example/a")


class TestJsonbColumnsRenderAsText(unittest.TestCase):
    """These columns arrive inconsistently — real list, JSON-encoded string, or a list
    whose single element is itself an encoded list. A raw Python repr must never reach the
    page."""

    def test_a_real_list_is_joined(self):
        self.assertEqual(od.display_value(["Ukraine", "United States"]),
                         "Ukraine, United States")

    def test_a_json_encoded_string_is_unpacked(self):
        self.assertEqual(od.display_value('["Vaccines", "MNCH"]'), "Vaccines, MNCH")

    def test_a_double_encoded_list_is_unpacked(self):
        self.assertEqual(od.display_value(['["Sub-Saharan Africa"]']),
                         "Sub-Saharan Africa")

    def test_a_plain_string_passes_through(self):
        self.assertEqual(od.display_value("  Cooperative Agreement  "),
                         "Cooperative Agreement")

    def test_a_number_passes_through(self):
        self.assertEqual(od.display_value(574676.0), "574676.0")

    def test_none_is_empty(self):
        self.assertEqual(od.display_value(None), "")

    def test_a_serialised_empty_collection_counts_as_blank(self):
        # "funding_tiers" came back as the literal string "[]" and rendered as "[]".
        row = {"uid": "x", "opportunity_name": "T", "funding_tiers": "[]",
               "attachments": "{}"}
        secs = dict((t, dict(f)) for t, f in od.sections(od.KIND_CATALOG, row))
        self.assertNotIn("Funding tiers", secs.get("The award", {}))


class TestIsScreened(unittest.TestCase):
    def test_a_catalogue_row_is_never_screened(self):
        self.assertFalse(od.is_screened(od.KIND_CATALOG, CATALOG_ROW))

    def test_a_scored_pipeline_row_is_screened(self):
        self.assertTrue(od.is_screened(od.KIND_PIPELINE, PIPELINE_ROW))

    def test_a_pipeline_row_with_no_score_is_not(self):
        self.assertFalse(od.is_screened(od.KIND_PIPELINE,
                                        dict(PIPELINE_ROW, alignment_score=None)))


if __name__ == "__main__":
    unittest.main()
