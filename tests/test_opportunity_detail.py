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

    def test_the_glance_metric_shows_a_formatted_award_value(self):
        # Award value is a headline metric now, not a card row — a table row could not
        # state it as plainly, and printing it in both places asked the reader to compare.
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        flat = _flat(v)
        self.assertEqual(od.format_money(v.get("grant_amount"), v.get("currency")),
                         "US $250,000")
        self.assertNotIn("Award value", flat)
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

    def test_the_glance_metric_shows_the_unit(self):
        # Duration moved out of the cards and into the three headline metrics, which is
        # where the page states it now; the formatter is the part that matters.
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(od.format_duration(v.get("project_duration")), "24 months")
        self.assertNotIn("Project duration", _flat(v))


class TestTheStandardLayout(unittest.TestCase):
    def test_a_blank_field_shows_a_DASH_rather_than_vanishing(self):
        # Reversed deliberately (owner, 2026-08-11). Dropping empty rows read better on one
        # call but changed the page's SHAPE between calls, so a reader could not tell "this
        # funder said nothing about it" from "this app does not track it", and could not
        # compare two calls by eye. A dash is a statement; a missing row is ambiguous.
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        flat = _flat(v)
        self.assertEqual(flat["Expected award date"], od.MISSING)   # not on the row
        self.assertEqual(flat["Institution types accepted"], od.MISSING)
        self.assertNotIn("Key risks", flat)      # tenant-specific, in the decision aid

    def test_a_serialised_empty_collection_still_counts_as_blank(self):
        # The jsonb columns arrive as the literal "[]" — which must read as a dash, never as
        # a value. That detection is what this test has always been about.
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(_flat(v)["Funding tiers"], od.MISSING)

    def test_placeholder_strings_count_as_blank(self):
        v = {"funder_name": "Not stated", "instrument_type": "n/a",
             "opportunity_type": "unknown", "agency_code": "  ", "deadline": "TBD"}
        values = {val for _t, rows in od.sections(v) for _lb, val in rows}
        self.assertEqual(values, {od.MISSING})

    def test_the_whole_skeleton_renders_even_for_an_empty_view(self):
        # The page must show what it TRACKS, not only what this call happened to state.
        titles = [t for t, _rows in od.sections({})]
        self.assertIn("Funding & awards", titles)
        self.assertIn("Who can apply", titles)
        self.assertTrue(all(val == od.MISSING
                            for _t, rows in od.sections({}) for _lb, val in rows))

    def test_a_row_suppressed_as_a_DUPLICATE_is_not_shown_as_a_gap(self):
        # The layout pseudo-fields are the exception: an award "range" that merely repeats the
        # single award value, or a second reference identical to the header one, were
        # suppressed because they are redundant — printing a dash would invent a gap.
        v = {"opportunity_name": "A Call", "grant_amount": 500000, "currency": "USD",
             "call_award_floor": 500000, "call_award_ceiling": 500000,
             "opportunity_id": "X-1", "funding_opportunity_number": "X-1"}
        flat = _flat(v)
        self.assertNotIn("Award range (per award)", flat)
        self.assertNotIn("Opportunity number", flat)

    def test_sections_follow_THE_REVIEWERS_QUESTIONS(self):
        # Not the schema's storage order: how much, by when, can we apply, is it our kind
        # of work, how do we submit. Provenance is no longer among them at all — it is
        # super_user detail.
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        titles = [t for t, _ in od.sections(v)]
        expected = ["Funding & awards", "Timeline", "Who can apply", "Scope & focus",
                    "Type of opportunity", "How to apply"]
        expected.insert(2, "Eligibility requirements")
        self.assertEqual(titles, [t for t in expected if t in titles])
        self.assertNotIn("Provenance", titles)
        self.assertNotIn("Identity", titles)

    def test_a_list_field_renders_as_text_not_a_python_repr(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(_flat(v)["Geographic scope"], "Countryland")
        self.assertEqual(_flat(v)["Institution types accepted"], "NGO")


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

    def test_a_missing_apply_url_falls_back_to_the_call_page(self):
        # This assertion is the reverse of what it once was. Holding out for a real apply
        # link meant the page offered NO way to act, on every row — the field is extracted
        # on none. The call page is one click from the real button, and the caller can tell
        # the fallback apart (it equals call_url) so no duplicate link is rendered.
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        self.assertEqual(od.apply_url(v), od.call_url(v))


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

    def test_the_uid_still_has_its_row_in_the_technical_view(self):
        v = od.standard_view(od.KIND_PIPELINE, PIPELINE_ROW, None)
        tech = {lb: val for _t, rows in od.technical_sections(v) for lb, val in rows}
        self.assertEqual(tech["RFPIS uid"], PIPELINE_ROW["uid"])


def _tech(view):
    return {lb: val for _t, rows in od.technical_sections(view) for lb, val in rows}


class TestNothingIsShownTwice(unittest.TestCase):
    """The header and the three glance metrics own six facts outright. Repeating them in a
    card below made the reader check whether the two agreed — the funder was printed twice
    on the page for exactly this reason."""

    FULL = {
        "opportunity_name": "A Call", "funder_name": "A Funder",
        "grantmaking_entity": "An Administrator", "opportunity_id": "TOPIC-01",
        "solicitation_type": "RFP", "grant_amount": 500000, "currency": "USD",
        "deadline": "2026-09-01", "project_duration": 24,
        "date_posted": "2026-05-01", "instrument_type": "Grant",
    }

    def test_the_header_and_metric_fields_are_absent_from_the_cards(self):
        flat = _flat(self.FULL)
        for label in ("Funder", "Grantmaking entity", "Opportunity ID", "Award value",
                      "Deadline", "Project duration", "Solicitation type"):
            with self.subTest(label=label):
                self.assertNotIn(label, flat)

    def test_the_funder_is_not_a_card_at_all(self):
        self.assertNotIn("Funder", [t for t, _rows in od.sections(self.FULL)])

    def test_an_award_range_equal_to_the_single_amount_is_suppressed(self):
        v = dict(self.FULL, call_award_floor=500000, call_award_ceiling=500000)
        self.assertNotIn("Award range (per award)", _flat(v))

    def test_a_real_award_span_is_shown(self):
        v = dict(self.FULL, call_award_floor=100000, call_award_ceiling=500000)
        self.assertEqual(_flat(v)["Award range (per award)"], "US $100,000 – US $500,000")

    def test_a_repeated_reference_is_not_printed_as_a_second_one(self):
        v = dict(self.FULL, funding_opportunity_number="TOPIC-01")   # same as the header id
        self.assertNotIn("Opportunity number", _flat(v))

    def test_a_genuinely_different_reference_is_shown(self):
        v = dict(self.FULL, funding_opportunity_number="ABC-999")
        self.assertEqual(_flat(v)["Opportunity number"], "ABC-999")


class TestTheSolicitationKindIsSpelledOut(unittest.TestCase):
    """The column holds the trade abbreviation (NOFO on 135 rows, CFP on 74, CfCN on 3) and
    is blank on 244 of 686. A reviewer should not need to know that CfCN is a concept-note
    round to understand what they are looking at."""

    def test_the_abbreviations_are_expanded(self):
        for raw, want in [("RFP", "Request for Proposals"),
                          ("NOFO", "Notice of Funding Opportunity"),
                          ("CFP", "Call for Proposals"),
                          ("EOI", "Expression of Interest"),
                          ("CfCN", "Call for Concept Notes"),
                          ("LOI", "Letter of Intent"),
                          ("RFQ", "Request for Quotation")]:
            with self.subTest(raw=raw):
                self.assertEqual(od.solicitation_label({"solicitation_type": raw}), want)

    def test_a_word_that_needs_no_expansion_is_left_alone(self):
        self.assertEqual(od.solicitation_label({"solicitation_type": "Tender"}), "Tender")

    def test_a_blank_type_falls_back_to_the_broader_classifier(self):
        # solicitation_type is blank on a third of rows; opportunity_type is on 99%.
        self.assertEqual(
            od.solicitation_label({"opportunity_type": "grant"}), "Grant")
        self.assertEqual(
            od.solicitation_label({"solicitation_type": "", "opportunity_type": "Procurement"}),
            "Procurement")

    def test_nothing_known_yields_nothing(self):
        self.assertEqual(od.solicitation_label({}), "")

    def test_the_title_line_pairs_the_two(self):
        self.assertEqual(
            od.title_line({"opportunity_name": "Bio-based fibres",
                           "solicitation_type": "RFP"}),
            ("Bio-based fibres", "Request for Proposals"))

    def test_the_type_is_not_also_a_card_row(self):
        self.assertNotIn("Solicitation type",
                         _flat({"opportunity_name": "T", "solicitation_type": "RFP"}))

    def test_a_super_user_can_still_see_the_raw_value(self):
        self.assertEqual(_tech({"solicitation_type": "CfCN"})["Solicitation type (raw)"],
                         "CfCN")


class TestApplyFallsBackToTheCallPage(unittest.TestCase):
    """`apply_url` is specified as required and populated on no row: the button is a
    different shape on every portal. Returning "" left the page with no way to act at all."""

    def test_the_extracted_apply_link_wins_when_present(self):
        v = {"apply_url": "https://f.example/apply", "opportunity_url": "https://f.example/c"}
        self.assertEqual(od.apply_url(v), "https://f.example/apply")

    def test_a_missing_apply_link_falls_back_to_the_call_page(self):
        for blank in (None, "", "n/a"):
            with self.subTest(apply_url=blank):
                v = {"apply_url": blank, "opportunity_url": "https://f.example/c"}
                self.assertEqual(od.apply_url(v), "https://f.example/c")

    def test_the_fallback_is_detectable_so_no_duplicate_link_is_rendered(self):
        v = {"opportunity_url": "https://f.example/c"}
        self.assertEqual(od.apply_url(v), od.call_url(v))

    def test_no_url_at_all_stays_empty(self):
        self.assertEqual(od.apply_url({}), "")


class TestTechnicalDetailIsSuperUserOnly(unittest.TestCase):
    """A reviewer does not act on a crawl timestamp, a content hash or a confidence band.
    On the page they competed for attention with the funder's actual terms."""

    ROW = dict(CATALOG_ROW, source="auto", source_uid="src-1",
               extraction_confidence="high", deadline_confidence="med",
               scraped_at="2026-07-01", updated_at="2026-08-01", agency_code="AF")

    def test_the_internal_fields_are_not_in_the_reviewer_layout(self):
        flat = _flat(self.ROW)
        for label in ("RFPIS uid", "Source", "Source uid", "Extraction confidence",
                      "Deadline confidence", "First seen", "Last updated", "Agency code"):
            with self.subTest(label=label):
                self.assertNotIn(label, flat)

    def test_they_are_all_in_the_technical_view(self):
        tech = _tech(od.standard_view(od.KIND_CATALOG, self.ROW, self.ROW))
        for label in ("RFPIS uid", "Source", "Extraction confidence", "Last updated"):
            with self.subTest(label=label):
                self.assertIn(label, tech)

    def test_an_empty_technical_view_yields_nothing(self):
        self.assertEqual(od.technical_sections({}), [])


class TestCoverageDoesNotFollowTheLayout(unittest.TestCase):
    """Coverage measures EXTRACTION. While it was derived from the section list, moving a
    card silently changed the score — and fields that are no longer displayed as rows
    (award value, deadline, the funder) stopped being counted at all."""

    def test_fields_owned_by_the_header_are_still_counted(self):
        for f in ("funder_name", "opportunity_id", "solicitation_type", "grant_amount",
                  "deadline", "project_duration"):
            with self.subTest(field=f):
                self.assertIn(f, od._COVERAGE_FIELDS)

    def test_the_unwritten_synthesis_fields_are_counted_as_missing(self):
        _f, _t, missing = od.coverage({"opportunity_name": "T"})
        # full_description has NO writer anywhere in the codebase — it must show as a gap.
        for f in ("full_description", "what_is_funded", "eligibility_countries",
                  "applicant_fit_profile", "attachments", "apply_url"):
            self.assertIn(f, missing)

    def test_no_layout_only_pseudo_field_leaks_into_the_count(self):
        for f in od._COVERAGE_FIELDS:
            self.assertFalse(f.startswith("_"), f)

    def test_a_fully_populated_view_reports_nothing_missing(self):
        filled, total, missing = od.coverage({f: "x" for f in od._COVERAGE_FIELDS})
        self.assertEqual((filled, missing), (total, []))


class TestTheExtractionJoinIsCaseInsensitive(unittest.TestCase):
    """THE REASON A SCREENED CALL LOOKED EMPTY. The join key is lowercased by
    `normalise_link`, but the lookup compared it with `=` against a column holding the URL
    as published — and 344 of 686 of those carry uppercase (a topic code sits in the path).
    58 of 257 pipeline rows were finding their extraction; 192 were reachable."""

    LINK = "https://funder.example/portal/topic-details/prog-ju-xyz-2026-ria-03"

    def test_a_pattern_is_produced_with_and_without_a_trailing_slash(self):
        self.assertEqual(od.link_query_patterns(self.LINK),
                         (self.LINK, self.LINK + "/"))

    def test_like_wildcards_in_a_url_are_escaped(self):
        # An unescaped "_" matches ANY character, so it could join the wrong call.
        pats = od.link_query_patterns("https://f.example/a_b?x=1%20y")
        self.assertEqual(pats[0], "https://f.example/a\\_b?x=1\\%20y")

    def test_a_backslash_is_escaped_first(self):
        self.assertEqual(od.link_query_patterns("a\\b")[0], "a\\\\b")

    def test_an_empty_link_produces_no_patterns(self):
        self.assertEqual(od.link_query_patterns(""), ())
        self.assertEqual(od.link_query_patterns(None), ())

    def test_the_verification_step_accepts_a_differently_cased_stored_url(self):
        stored = "https://Funder.example/portal/topic-details/PROG-JU-XYZ-2026-RIA-03"
        self.assertEqual(od.normalise_link(stored), self.LINK)

    def test_the_join_recovers_the_extraction_end_to_end(self):
        # Mirrors the page reader: patterns are case-insensitive, the hit is then verified.
        stored = dict(CATALOG_ROW,
                      opportunity_url="https://F.example/Call/ABC-1",
                      full_description="The long extracted description.")

        def by_link(link):
            for _p in od.link_query_patterns(link):
                if od.normalise_link(stored["opportunity_url"]) == link:
                    return stored
            return None

        res = od.load("BN-1",
                      pipeline_reader=lambda _u: dict(PIPELINE_ROW,
                                                      opportunity_link="https://f.example/call/abc-1"),
                      catalog_by_link_reader=by_link)
        self.assertIsNotNone(res["extraction"])
        v = od.standard_view(res["kind"], res["row"], res["extraction"])
        self.assertIn("Project overview", [h for h, _l in od.narrative_blocks(v)])


if __name__ == "__main__":
    unittest.main()


class TestTheFullSchemaIsAlwaysOnScreen(unittest.TestCase):
    """Owner's call, 2026-08-11: show every section a user can see, even where the data is
    not extracted yet.

    The reason is comparability. A page that hides what it has nothing for changes shape from
    call to call, so a reader cannot tell "this funder said nothing about project duration"
    from "this app does not track project duration", and cannot scan two calls for the same
    row because it sits in a different place in each. The cost is a card with dashes in it;
    the benefit is that the dash carries information."""

    THIN = {"opportunity_name": "A Sparse Call", "funder_name": "A Funder",
            "deadline": "2026-09-01"}

    def test_every_section_appears_however_little_the_call_stated(self):
        titles = [t for t, _rows in od.sections(self.THIN)]
        for expected in ("Funding & awards", "Timeline", "Who can apply",
                         "Scope & focus", "Type of opportunity", "How to apply"):
            with self.subTest(section=expected):
                self.assertIn(expected, titles)

    def test_every_tracked_field_appears(self):
        flat = _flat(self.THIN)
        for label in ("Total programme funding", "Expected number of awards",
                      "Funding tiers", "Posted", "Status", "Window",
                      "Expected award date", "Time to award",
                      # the call's OWN eligibility, in its own section
                      "Institution types accepted", "Eligible countries (applicants)",
                      "Other conditions",
                      # "Compliance requirements" is deliberately NOT a card row: it is long
                      # prose and renders as its own block, so carrying it in both places
                      # printed the same text twice.
                      "Ideal applicant",
                      "Geographic scope", "Sector", "Programme areas", "Project stages",
                      "Submission format", "Application language", "Application steps"):
            with self.subTest(label=label):
                self.assertIn(label, flat)

    def test_the_placeholder_is_a_single_consistent_mark(self):
        self.assertEqual(od.MISSING, "—")
        vals = {v for _t, rows in od.sections(self.THIN) for _lb, v in rows}
        self.assertIn(od.MISSING, vals)

    def test_a_populated_field_still_shows_its_value(self):
        # The skeleton must not flatten real data into dashes.
        v = dict(self.THIN, funding_status="Open", solicitation_language="English")
        flat = _flat(v)
        self.assertEqual(flat["Status"], "Open")
        self.assertEqual(flat["Application language"], "English")

    def test_every_narrative_section_appears_too(self):
        got = od.narrative_sections(self.THIN)
        self.assertEqual([h for h, _l, _m in got],
                         [h for h, _f in od.NARRATIVE_FIELDS])
        self.assertTrue(all(m for _h, _l, m in got))          # all missing here
        self.assertEqual(got[0][1], [od.MISSING])

    def test_a_narrative_section_with_content_is_not_marked_missing(self):
        v = dict(self.THIN, what_is_funded="Equipment\nTraining")
        by_heading = {h: (lines, missing) for h, lines, missing
                      in od.narrative_sections(v)}
        lines, missing = by_heading["What is funded"]
        self.assertFalse(missing)
        self.assertEqual(lines, ["Equipment", "Training"])

    def test_the_placeholder_is_the_SAME_mark_everywhere(self):
        # Reversed on 2026-08-11: a sentence about our pipeline ("Not extracted for this call
        # yet") put our internal state in front of a reviewer who only wants to know whether
        # the funder said anything. Cards and prose now use one mark.
        self.assertEqual(od.NOT_EXTRACTED, od.MISSING)
        self.assertEqual(od.MISSING, "—")


class TestTheSourcesOwnTextIsNotInTheUserView(unittest.TestCase):
    """A different publisher's structure on screen beside ours undid the one thing this page
    is for — that every call reads the same way. It stays available for audit."""

    def test_the_raw_extract_is_still_reachable_for_audit(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        self.assertEqual(od.as_published(v),
                         "The full page text as published by the funder.")

    def test_it_is_not_one_of_the_user_sections(self):
        v = od.standard_view(od.KIND_CATALOG, CATALOG_ROW, CATALOG_ROW)
        labels = {lb for _t, rows in od.sections(v) for lb, _v in rows}
        self.assertNotIn("As published", labels)
        self.assertNotIn("raw_text", labels)

    def test_a_row_without_it_is_not_an_error(self):
        self.assertEqual(od.as_published({}), "")

    def test_the_award_type_row_shows_a_gap_when_neither_axis_is_known(self):
        # It is a layout pseudo-field, but it stands for two REAL schema columns, so its
        # emptiness is a gap — unlike the two rows suppressed for redundancy.
        self.assertEqual(_flat({"opportunity_name": "A Call"})["Award type"], od.MISSING)

    def test_only_the_redundancy_rows_are_allowed_to_vanish(self):
        self.assertEqual(od._SUPPRESSED_WHEN_REDUNDANT,
                         frozenset({"_award_range", "_second_reference"}))


class TestGeographyIsWhatTheCALLPublished(unittest.TestCase):
    """A migrated or hand-entered row carries the countries the SUBMITTER had in mind, not the
    scope the funder published. Measured live: 34 of 63 migrated rows and both manual rows
    name one of the tenant's own countries in this column, against 3 of 192 auto-scanned ones.
    Shown as "Geographic scope" that told a reviewer the funder had restricted the call to
    their countries when it may well have said "Global"."""

    HAND = {"uid": "YA-1", "source": "migration",
            "opportunity_title": "A Migrated Call",
            "call_geographic_scope": ["Countryland", "Otherland", "Global"]}

    def test_a_hand_entered_scope_is_not_published_as_the_calls(self):
        v = od.standard_view(od.KIND_PIPELINE, self.HAND, None)
        self.assertEqual(_flat(v)["Geographic scope"], od.MISSING)

    def test_it_is_kept_as_the_submitters_note_rather_than_discarded(self):
        v = od.standard_view(od.KIND_PIPELINE, self.HAND, None)
        self.assertEqual(v["_submitter_geographic_scope"],
                         ["Countryland", "Otherland", "Global"])

    def test_an_extraction_is_always_preferred_over_the_typed_value(self):
        ext = {"uid": "es_1", "call_geographic_scope": ["Global / worldwide"]}
        v = od.standard_view(od.KIND_PIPELINE, self.HAND, ext)
        self.assertEqual(_flat(v)["Geographic scope"], "Global / worldwide")
        self.assertNotIn("_submitter_geographic_scope", v)

    def test_an_auto_scanned_row_is_trusted(self):
        # The scan reads geography off the call, so its value IS the call's.
        auto = dict(self.HAND, uid="AS-1", source="auto")
        v = od.standard_view(od.KIND_PIPELINE, auto, None)
        self.assertIn("Countryland", _flat(v)["Geographic scope"])

    def test_the_hand_entered_sources_are_named_explicitly(self):
        self.assertEqual(od._HAND_ENTERED, frozenset({"migration", "manual", "form"}))


class TestTheAwardCardAlwaysCarriesAUsdFigure(unittest.TestCase):
    """The card was a line shorter than its neighbours whenever the call was already in USD,
    so the row of three sat unevenly — and a reader comparing calls wants the dollar figure in
    the same place on both, not only on the foreign-currency ones."""

    def test_a_usd_award_shows_the_usd_figure(self):
        self.assertEqual(od.usd_reference(1500000, "USD"), "=US $1,500,000")

    def test_a_foreign_award_shows_the_conversion(self):
        got = od.usd_reference(33000000, "EUR")
        self.assertTrue(got.startswith("≈US $") or got == "", got)

    def test_no_amount_yields_nothing(self):
        for v in (None, "", 0, "n/a"):
            with self.subTest(value=v):
                self.assertEqual(od.usd_reference(v, "USD"), "")

    def test_a_messy_currency_string_still_counts_as_usd(self):
        self.assertEqual(od.usd_reference(500, "USD $"), "=US $500")


class TestNothingIsPrintedTwice(unittest.TestCase):
    """Text was appearing both inside a card and again as a prose block below it — the same
    sentences squeezed into a table cell and then set properly."""

    def test_no_field_is_both_a_card_row_and_a_prose_block(self):
        card_fields = {f for _t, rows in od._SECTIONS for _lb, f, _k in rows}
        prose_fields = {f for fields in od.INLINE_PROSE.values() for f in fields}
        prose_fields |= {f for row in od.PROSE_ROWS for _h, f in row}
        prose_fields |= {f for _h, f in od.OVERVIEW_FIELDS}
        self.assertEqual(card_fields & prose_fields, set())

    def test_the_brief_and_the_overview_are_different_fields(self):
        # The lede is brief_description; the overview is the publisher's fuller account. If
        # the overview ever fell back to the brief they would print twice under two headings.
        self.assertEqual([f for _h, f in od.OVERVIEW_FIELDS], ["full_description"])


class TestOnlyTightFactColumnsGetACard(unittest.TestCase):
    """Seven cards turned the page into a wall of boxes. A box earns its chrome for a column
    of numbers and dates a reader scans; round a sentence it is furniture."""

    def test_the_card_sections_are_the_scannable_ones(self):
        self.assertEqual(od.AS_CARDS,
                         frozenset({"Funding & awards", "Timeline", "Type of opportunity"}))

    def test_the_rest_are_marked_as_open_text(self):
        v = {"opportunity_name": "A Call", "deadline": "2026-09-01"}
        kinds = {b["title"]: b["kind"] for r in od.page_rows(v) for b in r}
        self.assertEqual(kinds["Funding & awards"], "cards")
        self.assertEqual(kinds["Timeline"], "cards")
        self.assertEqual(kinds["Eligibility requirements"], "facts")
        self.assertEqual(kinds["Who can apply"], "facts")
        self.assertEqual(kinds["How to apply"], "facts")

    def test_every_section_still_appears_in_one_form_or_the_other(self):
        v = {"opportunity_name": "A Call"}
        rendered = {b["title"] for r in od.page_rows(v) for b in r
                    if b["kind"] in ("cards", "facts")}
        self.assertEqual(rendered, {t for t, _rows in od.sections(v)})


class TestTheTitleDoesNotRepeatItself(unittest.TestCase):
    """"DIV Fund – Request for Proposals: Request for Proposals" — the funder usually names
    the kind in their own title, so appending our label repeated it."""

    def test_a_title_that_already_names_the_kind_gets_no_suffix(self):
        self.assertEqual(
            od.title_line({"opportunity_name": "A Fund – Request for Proposals",
                           "solicitation_type": "RFP"}),
            ("A Fund – Request for Proposals", ""))

    def test_an_acronym_in_the_title_counts_as_naming_it(self):
        self.assertEqual(
            od.title_line({"opportunity_name": "RFP: Cold chain equipment",
                           "solicitation_type": "RFP"})[1], "")

    def test_a_title_that_does_not_say_it_keeps_the_label(self):
        self.assertEqual(
            od.title_line({"opportunity_name": "Full-scale demonstration of heat upgrades",
                           "solicitation_type": "Tender"})[1], "Tender")

    def test_singular_and_plural_are_the_same_kind(self):
        self.assertEqual(
            od.title_line({"opportunity_name": "A Request for Proposal for services",
                           "solicitation_type": "RFP"})[1], "")


class TestThePublishersOwnOverview(unittest.TestCase):
    """The overview is the publisher's account of what they aim to fund, kept close to their
    wording — not our rewrite — and capped so a long one does not swallow the page."""

    def test_a_short_overview_is_returned_whole(self):
        v = {"full_description": "Purpose and objectives, stated briefly."}
        self.assertEqual(od.overview_text(v), "Purpose and objectives, stated briefly.")
        self.assertFalse(od.overview_is_truncated(v))

    def test_a_long_overview_is_clipped_at_a_sentence(self):
        body = ("The programme funds work on health systems. " * 200)
        v = {"full_description": body}
        got = od.overview_text(v)
        self.assertLess(len(got), od.OVERVIEW_MAX_CHARS + 40)
        self.assertTrue(got.endswith("…"))
        self.assertTrue(od.overview_is_truncated(v))
        self.assertIn("systems.", got)          # cut on a sentence, not mid-word

    def test_the_cap_is_about_five_hundred_words(self):
        self.assertGreaterEqual(od.OVERVIEW_MAX_CHARS, 3000)
        self.assertLessEqual(od.OVERVIEW_MAX_CHARS, 4200)

    def test_nothing_yields_nothing(self):
        self.assertEqual(od.overview_text({}), "")
        self.assertFalse(od.overview_is_truncated({}))


class TestTheRowsLeaveNoHoles(unittest.TestCase):
    """Streaming sections into a two-column run and resetting it whenever prose appeared left
    holes all down the page: a card on the left with nothing beside it, then a heading, then
    another lone card. Rows are now explicit."""

    V = {"opportunity_name": "A Call", "deadline": "2026-09-01"}

    def test_no_row_holds_more_than_two_blocks(self):
        for row in od.page_rows(self.V):
            self.assertLessEqual(len(row), 2)
            self.assertGreaterEqual(len(row), 1)

    def test_the_paired_rows_are_actually_paired(self):
        rows = od.page_rows(self.V)
        pairs = {tuple(b["title"] for b in r) for r in rows if len(r) == 2}
        self.assertIn(("Funding & awards", "Timeline"), pairs)
        # The owner asked for these two beside each other.
        self.assertIn(("Who can apply", "How to apply"), pairs)

    def test_what_is_and_is_not_funded_share_a_row(self):
        pairs = {tuple(b["title"] for b in r) for r in od.page_rows(self.V) if len(r) == 2}
        self.assertIn(("What is funded", "What is NOT funded"), pairs)

    def test_an_unplanned_section_is_paired_rather_than_orphaned(self):
        # A section added to _SECTIONS but not to LAYOUT_ROWS must still reach the page.
        planned = {t for row in od.LAYOUT_ROWS for t in row}
        titles = {t for t, _r in od.sections(self.V)}
        rendered = {b["title"] for r in od.page_rows(self.V) for b in r
                    if b["kind"] != "prose"}
        self.assertEqual(rendered, titles)
        self.assertTrue(titles <= planned | (titles - planned))

    def test_how_to_apply_carries_its_detail_without_a_second_heading(self):
        v = dict(self.V, how_to_apply="Register, then upload the budget.")
        block = next(b for r in od.page_rows(v) for b in r
                     if b["title"] == "How to apply")
        self.assertEqual(block["prose"], ["Register, then upload the budget."])
        # ...and there is no separate block for it.
        headings = [b["title"] for r in od.page_rows(v) for b in r]
        self.assertNotIn("How to apply in detail", headings)

    def test_compliance_sits_under_eligibility_not_on_its_own(self):
        v = dict(self.V, compliance_requirements="Audited accounts required.")
        block = next(b for r in od.page_rows(v) for b in r
                     if b["title"] == "Eligibility requirements")
        self.assertEqual(block["prose"], ["Audited accounts required."])
        self.assertNotIn("Compliance & hard gates",
                         [b["title"] for r in od.page_rows(v) for b in r])
