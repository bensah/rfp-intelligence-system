"""One opportunity resolved from either store and restated in the RFPIS standard format.

Two things these lock down:

  * a uid may name a SCREENED row (`rfp_submissions`, structured for matching) or a RAW
    EXTRACTION (`extracted_solicitations`, the regex + LLM + deep-read output). Both must
    render through ONE layout, because primary sources publish the same facts under
    different names and a reviewer can only compare two calls if they read the same way.
  * money is ONE value. "Value 244000000.0" above "Currency USD $" made the reader do the
    formatting and the joining.
"""
from __future__ import annotations

import unittest
from datetime import date

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
    "applicant_role": "Sub",
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
    "focus_themes": ["Immunisation"],
    "brief_description": "Short version.",
    "full_description": "The much longer extracted description of the call.",
    "instrument_type": "Grant",
    "project_duration": 24,
    "what_is_funded": "Delivery innovations.",
    "what_is_not_funded": "Construction.",
    "eligibility_applicant_types": ["NGO"],
    "extraction_confidence": "high",
    "solicitation_type": "RFP",
    "resource_links": ["https://another.example/guidelines.pdf"],
    "raw_text": "The full page text as published by the funder.",
    "eligibility_other": None,        # blank → dropped
    "funding_tiers": "[]",            # SERIALISED empty → dropped
}


def _reader(row):
    return lambda uid: row if row and row.get("uid") == uid else None


def _flat(view):
    return {lb: v for _t, fields in od.sections(view) for lb, v in fields}


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
        # For a catalogue uid the row IS the raw extraction.
        self.assertIs(res["extraction"], res["row"])

    def test_a_screened_row_is_joined_back_to_its_raw_extraction(self):
        # This is the point: a pipeline row keeps only the matching fields, so the full
        # call has to come from the extraction behind it.
        seen = {}

        def by_link(link):
            seen["link"] = link
            return CATALOG_ROW

        res = od.load("BN-260808-1155", pipeline_reader=_reader(PIPELINE_ROW),
                      catalog_by_link_reader=by_link)
        self.assertEqual(res["kind"], od.KIND_PIPELINE)
        self.assertEqual(seen["link"], "https://funder.example/call/1")
        self.assertIs(res["extraction"], CATALOG_ROW)

    def test_a_row_with_no_extraction_still_resolves(self):
        res = od.load("BN-260808-1155", pipeline_reader=_reader(PIPELINE_ROW),
                      catalog_by_link_reader=lambda link: None)
        self.assertEqual(res["kind"], od.KIND_PIPELINE)
        self.assertIsNone(res["extraction"])

    def test_the_pipeline_wins_when_both_stores_have_the_uid(self):
        res = od.load("SHARED",
                      pipeline_reader=_reader(dict(PIPELINE_ROW, uid="SHARED")),
                      catalog_reader=_reader(dict(CATALOG_ROW, uid="SHARED")))
        self.assertEqual(res["kind"], od.KIND_PIPELINE)

    def test_an_unknown_or_blank_uid_resolves_to_nothing(self):
        for uid in ("NOPE", "", "   ", None):
            with self.subTest(uid=uid):
                res = od.load(uid, pipeline_reader=_reader(PIPELINE_ROW),
                              catalog_reader=_reader(CATALOG_ROW))
                self.assertIsNone(res["kind"])

    def test_a_reader_that_raises_does_not_break_the_page(self):
        def boom(uid):
            raise RuntimeError("db down")
        res = od.load("ES-99", pipeline_reader=boom, catalog_reader=_reader(CATALOG_ROW))
        self.assertEqual(res["kind"], od.KIND_CATALOG)
        # ...and a failing extraction lookup must not lose the pipeline row either.
        res2 = od.load("BN-260808-1155", pipeline_reader=_reader(PIPELINE_ROW),
                       catalog_by_link_reader=boom)
        self.assertEqual(res2["kind"], od.KIND_PIPELINE)
        self.assertIsNone(res2["extraction"])


class TestOneStandardViewOverBothStores(unittest.TestCase):
    def test_the_matching_row_is_mapped_onto_the_schema_names(self):
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        self.assertEqual(v["opportunity_name"], "Cold Chain Equipment Subgrant")
        self.assertEqual(v["funder_name"], "A Health Funder")
        self.assertEqual(v["grant_amount"], 574676.0)
        self.assertEqual(v["deadline"], "2026-08-19")
        self.assertEqual(v["opportunity_url"], "https://funder.example/call/1")

    def test_the_raw_extraction_wins_field_by_field(self):
        # The extraction is the fuller read, so where both have a value it governs.
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, CATALOG_ROW)
        self.assertEqual(v["opportunity_name"], "Vaccine Delivery Innovation Fund")
        self.assertEqual(v["full_description"],
                         "The much longer extracted description of the call.")

    def test_the_matching_row_fills_what_the_extraction_lacks(self):
        ext = {"uid": "E", "opportunity_name": "From extraction"}
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, ext)
        self.assertEqual(v["opportunity_name"], "From extraction")
        self.assertEqual(v["funder_name"], "A Health Funder")     # only on the row
        self.assertEqual(v["applicant_role"], "Sub")              # RFPIS-only fact

    def test_a_blank_never_overwrites_a_populated_value(self):
        ext = {"uid": "E", "funder_name": "", "grant_amount": None}
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, ext)
        self.assertEqual(v["funder_name"], "A Health Funder")
        self.assertEqual(v["grant_amount"], 574676.0)


class TestMoneyIsOneValue(unittest.TestCase):
    def test_the_amount_and_its_currency_render_together(self):
        self.assertEqual(od.format_money(244000000.0, "USD"), "US $244,000,000")

    def test_a_whole_amount_drops_the_decimals(self):
        self.assertEqual(od.format_money(574676.0, "USD"), "US $574,676")

    def test_a_fractional_amount_keeps_two(self):
        self.assertEqual(od.format_money(1234.5, "USD"), "US $1,234.50")

    def test_other_currencies_carry_their_own_symbol(self):
        self.assertEqual(od.format_money(50000, "EUR"), "EU €50,000")
        self.assertEqual(od.format_money(50000, "GBP"), "GB £50,000")

    def test_an_unlisted_currency_prints_its_code(self):
        self.assertEqual(od.format_money(1000000, "XAF"), "XAF 1,000,000")

    def test_a_messy_currency_value_is_normalised(self):
        for raw in ("USD $", "usd", "USD - US Dollar", " USD "):
            with self.subTest(raw=raw):
                self.assertEqual(od.format_money(100, raw), "US $100")

    def test_a_missing_or_nonsense_amount_renders_empty(self):
        for raw in (None, "", 0, -5, "abc", float("nan")):
            with self.subTest(raw=raw):
                self.assertEqual(od.format_money(raw, "USD"), "")

    def test_a_range_reads_as_a_range(self):
        self.assertEqual(od.format_money_range(15000, 50000, "USD"),
                         "US $15,000 – US $50,000")

    def test_a_range_with_one_bound_shows_that_bound(self):
        self.assertEqual(od.format_money_range(15000, None, "USD"), "US $15,000")
        self.assertEqual(od.format_money_range(None, 50000, "USD"), "US $50,000")

    def test_equal_bounds_collapse_to_one_value(self):
        self.assertEqual(od.format_money_range(15000, 15000, "USD"), "US $15,000")

    def test_the_section_shows_a_formatted_award_value(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        flat = _flat(v)
        self.assertEqual(flat["Award value"], "US $250,000")
        self.assertNotIn("Currency", flat)      # never a bare code on its own row


class TestDeadlineStatus(unittest.TestCase):
    TODAY = date(2026, 8, 10)

    def test_days_until_counts_forward(self):
        self.assertEqual(od.days_until("2026-08-19", self.TODAY), 9)

    def test_an_urgent_deadline_is_flagged(self):
        txt, tone = od.deadline_status("2026-08-19", None, self.TODAY)
        self.assertEqual((txt, tone), ("9 days left", "urgent"))

    def test_a_distant_deadline_is_open(self):
        self.assertEqual(od.deadline_status("2027-03-31", None, self.TODAY)[1], "open")

    def test_a_mid_range_deadline_is_soon(self):
        self.assertEqual(od.deadline_status("2026-09-15", None, self.TODAY)[1], "soon")

    def test_a_past_deadline_is_closed(self):
        txt, tone = od.deadline_status("2026-08-01", None, self.TODAY)
        self.assertEqual(tone, "closed")
        self.assertEqual(txt, "Closed 9 days ago")

    def test_a_single_day_is_not_pluralised(self):
        self.assertEqual(od.deadline_status("2026-08-11", None, self.TODAY)[0],
                         "1 day left")
        self.assertEqual(od.deadline_status("2026-08-09", None, self.TODAY)[0],
                         "Closed 1 day ago")

    def test_due_today(self):
        self.assertEqual(od.deadline_status("2026-08-10", None, self.TODAY),
                         ("Due today", "urgent"))

    def test_a_stated_closed_status_wins_over_the_date(self):
        # The cron flips this when a deadline passes, and a funder can close early.
        self.assertEqual(od.deadline_status("2027-03-31", "Closed", self.TODAY),
                         ("Closed", "closed"))

    def test_no_deadline_is_unknown_not_closed(self):
        self.assertEqual(od.deadline_status(None, None, self.TODAY)[1], "unknown")


class TestDurationCarriesItsUnit(unittest.TestCase):
    """`project_duration` counts MONTHS, so a bare "36" tells the reader nothing."""

    def test_a_number_becomes_months(self):
        self.assertEqual(od.format_duration(36), "36 months")
        self.assertEqual(od.format_duration("24"), "24 months")
        self.assertEqual(od.format_duration(24.0), "24 months")

    def test_one_month_is_not_pluralised(self):
        self.assertEqual(od.format_duration(1), "1 month")

    def test_free_text_that_names_its_unit_is_left_alone(self):
        self.assertEqual(od.format_duration("18-24 months"), "18-24 months")
        self.assertEqual(od.format_duration("2 years"), "2 years")

    def test_blank_is_empty(self):
        for raw in (None, "", "n/a"):
            self.assertEqual(od.format_duration(raw), "")

    def test_the_section_shows_the_unit(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(_flat(v)["Project duration"], "24 months")


class TestTheStandardLayout(unittest.TestCase):
    def test_blank_and_placeholder_fields_are_dropped(self):
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        flat = _flat(v)
        self.assertNotIn("Instrument", flat)          # None
        self.assertNotIn("Key risks", flat)           # ""

    def test_a_serialised_empty_collection_is_dropped(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertNotIn("Funding tiers", _flat(v))   # was the literal "[]"

    def test_placeholder_strings_count_as_blank(self):
        v = {"funder_name": "Not stated", "instrument_type": "n/a",
             "opportunity_type": "unknown", "agency_code": "  ", "deadline": "TBD"}
        self.assertEqual(od.sections(v), [])

    def test_empty_sections_are_dropped_whole(self):
        self.assertEqual(od.sections({}), [])

    def test_sections_follow_the_schema_order(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        titles = [t for t, _ in od.sections(v)]
        self.assertEqual(titles[:2], ["Funding", "Dates & window"])
        self.assertIn("Who can apply", titles)
        self.assertIn("Classification", titles)
        self.assertEqual(titles[-1], "Provenance")

    def test_a_list_field_renders_as_text_not_a_python_repr(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(_flat(v)["Geographic scope"], "Countryland")
        self.assertEqual(_flat(v)["Applicant types"], "NGO")


class TestNarrativeAndRawRead(unittest.TestCase):
    def test_the_summary_is_the_house_style_brief(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(od.summary_of(v), "Short version.")

    def test_the_narrative_blocks_carry_the_prose_sections(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        blocks = dict(od.narrative_blocks(v))
        self.assertEqual(blocks["Project overview"],
                         ["The much longer extracted description of the call."])
        self.assertEqual(blocks["What is funded"], ["Delivery innovations."])
        self.assertEqual(blocks["What is NOT funded"], ["Construction."])

    def test_empty_narrative_fields_produce_no_block(self):
        blocks = dict(od.narrative_blocks({"full_description": "", "what_is_funded": None}))
        self.assertEqual(blocks, {})

    def test_bullets_split_on_newlines_and_semicolons(self):
        self.assertEqual(od.as_bullets("- One\n- Two; Three"), ["One", "Two", "Three"])

    def test_bullets_accept_a_json_encoded_list(self):
        self.assertEqual(od.as_bullets('["NGO", "Academic"]'), ["NGO", "Academic"])

    def test_as_published_returns_the_raw_page_text(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(od.as_published(v),
                         "The full page text as published by the funder.")


class TestDocuments(unittest.TestCase):
    def test_structured_attachments_carry_label_and_type(self):
        v = {"attachments": [{"url": "https://x.example/a.pdf", "label": "Full RFP",
                              "doc_type": "full_rfp"}]}
        self.assertEqual(od.documents(v),
                         [("Full RFP", "https://x.example/a.pdf", "full_rfp")])

    def test_a_json_encoded_array_is_accepted(self):
        v = {"resource_links": '[{"url": "https://x.example/b.docx", '
                               '"label": "Budget template", "type": "budget_template"}]'}
        self.assertEqual(od.documents(v),
                         [("Budget template", "https://x.example/b.docx",
                           "budget_template")])

    def test_a_bare_url_string_is_labelled_by_itself(self):
        v = {"resource_links": ["https://x.example/guidelines.pdf"]}
        self.assertEqual(od.documents(v),
                         [("https://x.example/guidelines.pdf",
                           "https://x.example/guidelines.pdf", "resource")])

    def test_entries_without_a_url_are_skipped(self):
        self.assertEqual(od.documents({"attachments": [{"label": "no link"}]}), [])

    def test_no_documents_is_an_empty_list(self):
        self.assertEqual(od.documents({}), [])
        self.assertEqual(od.documents({"attachments": "[]"}), [])


class TestCoverageReportsTheDataGap(unittest.TestCase):
    """The LLM-synthesis stage of the extraction schema has never run, so most narrative
    fields are empty on every row. That has to read as a DATA gap, not as a layout that
    forgot to show something."""

    def test_coverage_counts_populated_schema_fields(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        filled, total, missing = od.coverage(v)
        self.assertGreater(total, 20)
        self.assertGreater(filled, 0)
        self.assertEqual(filled + len(missing), total)

    def test_the_unpopulated_synthesis_fields_are_named_as_missing(self):
        v = od.standard_view(od.KIND_CATALOG, {"uid": "x", "opportunity_name": "T"}, None)
        _f, _t, missing = od.coverage(v)
        for field in ("what_is_funded", "attachments", "resource_links",
                      "applicant_fit_profile"):
            self.assertIn(field, missing)

    def test_a_fully_populated_view_reports_nothing_missing(self):
        v = {f: "x" for f in od._COVERAGE_FIELDS}
        filled, total, missing = od.coverage(v)
        self.assertEqual((filled, missing), (total, []))


class TestTitleAndLinks(unittest.TestCase):
    def test_title_comes_from_the_schema_name(self):
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        self.assertEqual(od.title_of(v), "Cold Chain Equipment Subgrant")

    def test_an_untitled_opportunity_still_renders(self):
        self.assertEqual(od.title_of({}), "(untitled opportunity)")

    def test_the_apply_url_is_kept_distinct_from_the_listing_page(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(od.call_url(v), "https://another.example/opportunity/7")
        self.assertEqual(od.apply_url(v), "https://another.example/apply/7")

    def test_no_apply_url_is_empty_not_the_listing_page(self):
        # An Apply button must never quietly send someone back to the summary page.
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        self.assertEqual(od.apply_url(v), "")


class TestTurningACatalogueRowIntoATrackableCandidate(unittest.TestCase):
    def test_the_catalogue_column_names_are_mapped(self):
        cand = od.to_candidate(CATALOG_ROW)
        self.assertEqual(cand["opportunity_title"], "Vaccine Delivery Innovation Fund")
        self.assertEqual(cand["funding_agency"], "Another Funder")
        self.assertEqual(cand["call_submission_deadline"], "2026-10-01")
        self.assertEqual(cand["call_award_value"], 250000)

    def test_the_extraction_is_carried_not_thrown_away(self):
        cand = od.to_candidate(CATALOG_ROW)
        self.assertEqual(cand["call_geographic_scope"], ["Countryland"])
        self.assertEqual(cand["project_duration"], 24)
        self.assertEqual(cand["instrument_type"], "Grant")

    def test_domain_areas_fall_back_to_focus_themes(self):
        self.assertEqual(od.to_candidate(CATALOG_ROW)["call_domain_areas"],
                         ["Immunisation"])

    def test_blank_values_are_omitted(self):
        cand = od.to_candidate({"opportunity_name": "T", "funder_name": "",
                                "grant_amount": None})
        self.assertEqual(set(cand), {"opportunity_title"})

    def test_a_title_is_capped_for_the_column(self):
        self.assertEqual(len(od.to_candidate({"opportunity_name": "x" * 400})
                             ["opportunity_title"]), 300)


class TestAlreadyTrackedDetection(unittest.TestCase):
    def test_a_matching_call_url_finds_the_tenants_own_row(self):
        mine = [{"uid": "AA-1", "opportunity_link": "https://other.example/x"},
                {"uid": "BB-2",
                 "opportunity_link": "https://another.example/opportunity/7/"}]
        self.assertEqual(od.tracked_uid(CATALOG_ROW, mine), "BB-2")

    def test_no_match_returns_none(self):
        self.assertIsNone(od.tracked_uid(
            CATALOG_ROW, [{"uid": "A", "opportunity_link": "https://nope.example"}]))

    def test_an_empty_pipeline_returns_none(self):
        self.assertIsNone(od.tracked_uid(CATALOG_ROW, []))
        self.assertIsNone(od.tracked_uid(CATALOG_ROW, None))

    def test_link_comparison_ignores_case_and_trailing_slash(self):
        self.assertEqual(od.normalise_link("HTTPS://X.example/A/"), "https://x.example/a")


class TestIsScreened(unittest.TestCase):
    def test_a_catalogue_row_is_never_screened(self):
        self.assertFalse(od.is_screened(od.KIND_CATALOG, CATALOG_ROW))

    def test_a_scored_pipeline_row_is_screened(self):
        self.assertTrue(od.is_screened(od.KIND_PIPELINE, PIPELINE_ROW))

    def test_a_pipeline_row_with_no_score_is_not(self):
        self.assertFalse(od.is_screened(od.KIND_PIPELINE,
                                        dict(PIPELINE_ROW, alignment_score=None)))


class TestOnlyARealDispositionCountsAsAPipeline(unittest.TestCase):
    """A row lands in `rfp_submissions` the moment the scan touches it. 180 of 254 live rows
    carry NO decision and 160 are marked not eligible, so badging every pipeline row "In
    your pipeline" told the reviewer that three quarters of the store was live work. A
    pipeline is the three real dispositions and nothing else."""

    def test_each_real_disposition_counts(self):
        for d in ("Proceed", "Park", "Decline"):
            with self.subTest(decision=d):
                row = dict(PIPELINE_ROW, decision=d)
                self.assertEqual(od.pipeline_decision(od.KIND_PIPELINE, row), d)
                self.assertTrue(od.in_pipeline(od.KIND_PIPELINE, row))

    def test_a_scanned_row_with_no_decision_does_not(self):
        for blank in (None, "", "   "):
            with self.subTest(decision=blank):
                row = dict(PIPELINE_ROW, decision=blank)
                self.assertIsNone(od.pipeline_decision(od.KIND_PIPELINE, row))
                self.assertFalse(od.in_pipeline(od.KIND_PIPELINE, row))

    def test_a_row_rejected_at_screening_does_not(self):
        # The shape of the 160: scored, ineligible, never given a disposition.
        row = dict(PIPELINE_ROW, decision=None, qualification="No, not eligible",
                   auto_recommendation="Decline")
        self.assertFalse(od.in_pipeline(od.KIND_PIPELINE, row))

    def test_a_catalogue_call_never_does(self):
        self.assertFalse(od.in_pipeline(od.KIND_CATALOG, CATALOG_ROW))
        self.assertIsNone(od.pipeline_decision(od.KIND_CATALOG,
                                               dict(CATALOG_ROW, decision="Proceed")))

    def test_the_stored_casing_is_tolerated(self):
        self.assertEqual(
            od.pipeline_decision(od.KIND_PIPELINE, dict(PIPELINE_ROW, decision="proceed")),
            "Proceed")


class TestPartOneIsTenantVoid(unittest.TestCase):
    """Part 1 restates the CALL and must read identically for every tenant. Two fields are
    statements about the tenant instead, and both used to render there: "Our role" inside
    the "Who can apply" card, where it looked like an eligibility rule the funder had
    published, and "Key risks", which describes this entity's exposure, not the call."""

    def test_our_role_is_not_in_the_call_layout(self):
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        self.assertEqual(v["applicant_role"], "Sub")        # still resolved…
        self.assertNotIn("Our role", _flat(v))              # …but not shown in Part 1

    def test_key_risks_is_not_a_call_narrative(self):
        row = dict(PIPELINE_ROW, key_risks="Lacks presence in the eligible regions.")
        v = od.standard_view(od.KIND_PIPELINE, row, None)
        self.assertNotIn("Key risks",
                         [h for h, _lines in od.narrative_blocks(v)])

    def test_both_appear_in_the_decision_aid_instead(self):
        row = dict(PIPELINE_ROW, key_risks="Lacks presence in the eligible regions.")
        v = od.standard_view(od.KIND_PIPELINE, row, None)
        rows, prose = od.decision_aid(v)
        self.assertIn(("Our role on a bid", "Sub"), rows)
        self.assertEqual([h for h, _l in prose], ["Key risks for this entity"])

    def test_a_catalogue_call_has_no_decision_aid(self):
        # Nobody has screened it, so there is no tenant view of it to show.
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(od.decision_aid(v), ([], []))

    def test_our_role_no_longer_counts_toward_extraction_completeness(self):
        # It is not an extracted field, so counting it inflated the score.
        self.assertNotIn("applicant_role", od._COVERAGE_FIELDS)


class TestTheHeaderReference(unittest.TestCase):
    """Under the title sat the RFPIS uid, which reads like the call's own number. The
    funder's id is what gets quoted back to them or searched on their portal; the uid is an
    internal key and already has a row under Identity."""

    def test_the_funders_own_id_is_used(self):
        v = od.standard_view(od.KIND_CATALOG,
                             dict(CATALOG_ROW, opportunity_id="TOPIC-2026-03"),
                             dict(CATALOG_ROW, opportunity_id="TOPIC-2026-03"))
        self.assertEqual(od.header_reference(v), "TOPIC-2026-03")

    def test_it_is_blank_when_the_call_published_none(self):
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        self.assertEqual(od.header_reference(v), "")
        self.assertEqual(od.header_reference({"opportunity_id": "n/a"}), "")

    def test_the_uid_is_never_used_as_the_reference(self):
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        self.assertNotEqual(od.header_reference(v), PIPELINE_ROW["uid"])

    def test_the_uid_still_has_its_row_under_identity(self):
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        self.assertEqual(_flat(v)["RFPIS uid"], PIPELINE_ROW["uid"])


if __name__ == "__main__":
    unittest.main()
