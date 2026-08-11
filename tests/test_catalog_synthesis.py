"""Writing the §4 catalogue fields that never had a writer.

Nine columns were blank on all 686 rows — not failed extractions, but fields nothing ever
populated. `full_description` appeared in exactly two places in the codebase: the column
allow-list and the read on the opportunity page.

The design constraint is that this must work on the FREE TIER (gpt-oss:120b via Ollama
Cloud), which shapes what is tested here:

  * ONE model call per row for all the reading fields, never one call per field
  * a partial answer is kept field by field — demanding all of them or nothing is what
    makes a weaker model useless
  * JSON dug out of a reply that wraps it in prose or a fence, which a free-tier model does
    far more often than a paid one
  * the exact strings (an email address, a portal name) come from REGEX, never from the
    model, because a paraphrased submission address sends a proposal nowhere
  * a blank stays blank rather than becoming a plausible sentence

No network: the client is stubbed everywhere.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")
os.environ.setdefault("LLM_JUDGE_BASE_URL", "http://localhost:11434/v1")

from core import catalog_synthesis as CS       # noqa: E402


class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})()


class _Stub:
    """A stubbed chat completion returning `content`.

    `is_enabled()` is forced on as part of the stub: it reads LLM_JUDGE_BASE_URL from the
    environment, and another test module in the suite clears that — which made every
    model-path test here pass alone and fail in a full run. The stub asserts what it needs
    rather than depending on import order.
    """

    def __init__(self, content):
        self.client = mock.MagicMock()
        self.client.chat.completions.create.return_value = type(
            "R", (), {"choices": [_Msg(content)]})()
        self._patches = ()

    def __enter__(self):
        self._patches = (mock.patch.object(CS, "_client", return_value=self.client),
                         mock.patch.object(CS, "is_enabled", return_value=True))
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False

    @property
    def calls(self) -> int:
        return self.client.chat.completions.create.call_count


def _reply(content):
    return _Stub(content)


# Long enough to clear _MIN_TEXT, because a row with less text than this does not get a
# model call at all (see AThinRowNeverCostsACallTests). The fixture used to be two
# sentences, which made every model-path test here silently exercise the skip path instead.
ROW = {"uid": "es_1", "opportunity_name": "A Health Delivery Call",
       "funder_name": "A Funder", "opportunity_url": "https://funder.example/call/1",
       "raw_text": (
           "Applications are invited from established organisations for the delivery of "
           "cold-chain equipment and associated training in participating districts. "
           "Proposals must be submitted through UNGM before the closing date. "
           "The programme will fund the procurement, installation and commissioning of "
           "equipment, together with the training of health workers in its operation and "
           "routine maintenance. Applicants are expected to demonstrate prior experience "
           "of comparable delivery, and to describe how the equipment will be maintained "
           "beyond the period of the award. Construction of new facilities is excluded.")}

GOOD = ('{"full_description": "A long original description of the call and what it '
        'expects an applicant to deliver.", "what_is_funded": ["Cold-chain equipment", '
        '"Training"], "what_is_not_funded": ["Construction"], '
        '"eligibility_countries": ["Kenya", "Uganda"], '
        '"eligibility_other": ["Local registration required"], '
        '"applicant_fit_profile": "An established national NGO.", '
        '"project_stages": ["Implementation", "Capacity building"]}')


class OneCallPerRowTests(unittest.TestCase):
    def setUp(self):
        CS.reset_calls()

    def test_all_the_reading_fields_come_from_a_single_call(self):
        with _reply(GOOD) as c:
            got = CS.synthesize_row(dict(ROW))
        self.assertEqual(c.calls, 1)
        for f in ("full_description", "what_is_funded", "what_is_not_funded",
                  "eligibility_countries", "eligibility_other",
                  "applicant_fit_profile", "project_stages"):
            self.assertIn(f, got, f)

    def test_bullets_arrive_as_lines_the_page_can_render(self):
        with _reply(GOOD):
            got = CS.synthesize_row(dict(ROW))
        self.assertEqual(got["what_is_funded"], "Cold-chain equipment\nTraining")

    def test_no_call_is_made_when_there_is_nothing_to_read(self):
        with _reply(GOOD) as c:
            CS.synthesize_row(dict(ROW, raw_text=""))
        self.assertEqual(c.calls, 0)

    def test_no_call_is_made_when_every_field_is_already_populated(self):
        full = dict(ROW, **{f: "already there" for f in CS.LLM_FIELDS})
        with _reply(GOOD) as c:
            got = CS.synthesize_row(full)
        self.assertEqual(c.calls, 0)
        self.assertEqual(got, {})

    def test_the_call_ceiling_stops_a_runaway_backfill(self):
        with mock.patch.object(CS, "_MAX_CALLS", 1), _reply(GOOD) as c:
            CS.synthesize_row(dict(ROW))
            CS.synthesize_row(dict(ROW))
        self.assertEqual(c.calls, 1)


class ABlankStaysBlankTests(unittest.TestCase):
    """A missing column is a true statement about the call. A plausible sentence about
    something the call never said is one a reviewer would act on."""

    def setUp(self):
        CS.reset_calls()

    def test_a_populated_field_is_never_overwritten(self):
        row = dict(ROW, full_description="A human wrote this.")
        with _reply(GOOD):
            got = CS.synthesize_row(row)
        self.assertNotIn("full_description", got)

    def test_placeholder_answers_are_dropped(self):
        with _reply('{"full_description": "Not specified", '
                    '"what_is_funded": ["N/A", "None stated"], '
                    '"eligibility_other": ["not applicable"]}'):
            got = CS.synthesize_row(dict(ROW))
        for f in ("full_description", "what_is_funded", "eligibility_other"):
            self.assertNotIn(f, got, f)

    def test_an_omitted_key_yields_no_column(self):
        with _reply('{"full_description": "Real prose about this call."}'):
            got = CS.synthesize_row(dict(ROW))
        self.assertIn("full_description", got)
        self.assertNotIn("what_is_not_funded", got)

    def test_an_invented_project_stage_is_discarded(self):
        # Stored as TEXT, not a list — see ProjectStagesAreStoredAsTextTests.
        with _reply('{"project_stages": ["Implementation", "Interpretive Dance"]}'):
            got = CS.synthesize_row(dict(ROW))
        self.assertEqual(got["project_stages"], "Implementation")

    def test_a_model_failure_still_returns_the_regex_fields(self):
        with mock.patch.object(CS, "_client", side_effect=RuntimeError("rate limited")):
            got = CS.synthesize_row(dict(ROW))
        self.assertEqual(got.get("submission_format"), "Online portal: UNGM")


class FreeTierJsonTests(unittest.TestCase):
    """A partial or badly wrapped answer must still be worth something — this is what
    makes a free-tier model usable rather than a coin toss."""

    def setUp(self):
        CS.reset_calls()

    def test_a_fenced_reply_is_parsed(self):
        with _reply("```json\n" + GOOD + "\n```"):
            self.assertIn("full_description", CS.synthesize_row(dict(ROW)))

    def test_a_reply_with_a_preamble_is_parsed(self):
        with _reply("Sure! Here is the JSON you asked for:\n" + GOOD + "\nHope that helps."):
            self.assertIn("full_description", CS.synthesize_row(dict(ROW)))

    def test_a_brace_inside_a_string_does_not_break_the_scan(self):
        with _reply('Here you go: {"full_description": "Funds work on {health} systems.'
                    ' Nothing more."}'):
            got = CS.synthesize_row(dict(ROW))
        self.assertIn("{health}", got["full_description"])

    def test_a_partial_answer_keeps_the_fields_it_got_right(self):
        with _reply('{"full_description": "Good prose.", "what_is_funded": []}'):
            got = CS.synthesize_row(dict(ROW))
        self.assertIn("full_description", got)
        self.assertNotIn("what_is_funded", got)

    def test_an_unparseable_reply_loses_nothing_already_earned(self):
        with _reply("I'm sorry, I can't help with that."):
            got = CS.synthesize_row(dict(ROW))
        self.assertNotIn("full_description", got)
        self.assertEqual(got.get("submission_format"), "Online portal: UNGM")


class SubmissionFormatIsRegexFirstTests(unittest.TestCase):
    """Measured over 621 live rows the phrase rules alone returned 3%: the wording varies
    endlessly. The PLATFORM NAME is exact text and is what a reviewer needs — it says where
    the submission happens and what account they need. That took it to 26%, and the model
    (already reading the document) covers the rest."""

    def test_a_named_platform_wins(self):
        for text, want in [
                ("Bidders must submit bids through UNGM.", "Online portal: UNGM"),
                ("Apply via the EU Funding and Tenders Portal.",
                 "Online portal: EU Funding & Tenders Portal"),
                ("Suppliers registered on the MultiQuote platform may quote.",
                 "Online portal: MultiQuote"),
                ("Submit your application in grants.gov Workspace.",
                 "Online portal: grants.gov Workspace")]:
            with self.subTest(text=text):
                self.assertEqual(CS.extract_submission_format(text), want)

    def test_an_email_address_is_returned_verbatim(self):
        got = CS.extract_submission_format(
            "Proposals should be sent to tenders@funder.example before the deadline.")
        self.assertIn("tenders@funder.example", got)

    def test_a_platform_plus_an_email_keeps_both(self):
        got = CS.extract_submission_format(
            "Submit through UNGM. Queries to help@funder.example.")
        self.assertIn("UNGM", got)
        self.assertIn("help@funder.example", got)

    def test_a_two_stage_process_is_named(self):
        got = CS.extract_submission_format(
            "Applicants must submit a concept note first, before a full proposal.")
        self.assertIn("concept note", got.lower())

    def test_silence_yields_nothing(self):
        self.assertIsNone(CS.extract_submission_format(
            "This programme supports health systems in several countries."))
        self.assertIsNone(CS.extract_submission_format(""))

    def test_the_regex_answer_is_not_replaced_by_the_model(self):
        # The address/portal must be exact; a paraphrase sends a proposal nowhere.
        CS.reset_calls()
        with _reply('{"submission_format": "probably by email somewhere"}'):
            got = CS.synthesize_row(dict(ROW))
        self.assertEqual(got["submission_format"], "Online portal: UNGM")


HTML = """
<html><body>
  <a href="/files/full-rfp.pdf">Full Request for Proposals</a>
  <a href="/files/budget-template.xlsx">Budget Template</a>
  <a href="/files/annex-2.docx">Annex 2 — Reporting</a>
  <a href="/guidance/how-to-apply">Applicant Guidelines</a>
  <a href="/faq">Frequently Asked Questions</a>
  <a href="/about-us">About us</a>
  <a href="/files/full-rfp.pdf">Full Request for Proposals</a>
  <a href="#top">Top</a><a href="mailto:x@y.example">Mail</a>
</body></html>
"""


class DocumentsComeFromTheMarkupTests(unittest.TestCase):
    """`raw_text` is the page's TEXT, so its links are already gone — recovering documents
    means re-fetching the page, which is why the backfill makes that opt-in."""

    def setUp(self):
        self.att, self.res = CS.extract_documents(HTML, "https://funder.example/call/1")

    def test_files_become_attachments_with_absolute_urls(self):
        urls = [a["url"] for a in self.att]
        self.assertIn("https://funder.example/files/full-rfp.pdf", urls)
        self.assertIn("https://funder.example/files/budget-template.xlsx", urls)

    def test_each_document_is_typed_from_its_label(self):
        by_url = {a["url"].rsplit("/", 1)[-1]: a["doc_type"] for a in self.att}
        self.assertEqual(by_url["full-rfp.pdf"], "full_rfp")
        self.assertEqual(by_url["budget-template.xlsx"], "budget_template")
        self.assertEqual(by_url["annex-2.docx"], "annex")

    def test_labelled_pages_become_resource_links(self):
        kinds = {r["doc_type"] for r in self.res}
        self.assertIn("guidance", kinds)
        self.assertIn("faq", kinds)

    def test_unlabelled_navigation_is_dropped(self):
        every = [d["url"] for d in self.att + self.res]
        self.assertNotIn("https://funder.example/about-us", every)

    def test_a_repeated_link_appears_once(self):
        urls = [a["url"] for a in self.att]
        self.assertEqual(len(urls), len(set(urls)))

    def test_anchors_and_mailto_are_ignored(self):
        every = " ".join(d["url"] for d in self.att + self.res)
        self.assertNotIn("mailto:", every)
        self.assertNotIn("#top", every)

    def test_no_html_yields_nothing_rather_than_raising(self):
        self.assertEqual(CS.extract_documents(None), ([], []))
        self.assertEqual(CS.extract_documents(""), ([], []))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AThinRowNeverCostsACallTests(unittest.TestCase):
    """Measured on a 20-row batch, 4 rows produced no field at all — and every one had a
    raw_text that was boilerplate rather than a call: 20 characters ("fundsforNGOs Premium",
    an aggregator paywall stub), 74, 96 and 108 characters of procurement-portal furniture.
    That is 20% of a free-tier batch spent on rows that could not have answered. The model was
    right to return nothing; the fix is not to ask it."""

    def setUp(self):
        CS.reset_calls()

    def test_a_paywall_stub_costs_no_call(self):
        with _reply(GOOD) as c:
            got = CS.synthesize_row(dict(ROW, raw_text="fundsforNGOs Premium"))
        self.assertEqual(c.calls, 0)
        self.assertEqual(CS.skipped_thin(), 1)
        self.assertNotIn("full_description", got)

    def test_portal_boilerplate_costs_no_call(self):
        boiler = ("Welcome to the procurement platform of the organisation. "
                  "Terms and Conditions Site Map Glossary")
        with _reply(GOOD) as c:
            CS.synthesize_row(dict(ROW, raw_text=boiler))
        self.assertEqual(c.calls, 0)

    def test_the_free_regex_fields_are_still_returned(self):
        # Skipping the model must not skip the work that costs nothing.
        thin = "Submit bids through UNGM. Terms and Conditions."
        with _reply(GOOD):
            got = CS.synthesize_row(dict(ROW, raw_text=thin))
        self.assertEqual(got.get("submission_format"), "Online portal: UNGM")

    def test_a_real_call_is_still_read(self):
        body = ("The programme invites proposals from established organisations. " * 8)
        self.assertGreaterEqual(len(body), CS._MIN_TEXT)
        with _reply(GOOD) as c:
            got = CS.synthesize_row(dict(ROW, raw_text=body))
        self.assertEqual(c.calls, 1)
        self.assertIn("full_description", got)

    def test_the_threshold_is_generous_rather_than_strict(self):
        # A false skip loses a row permanently; a wasted call costs seconds. So the bar sits
        # at roughly two sentences of substance, not at "long".
        self.assertLessEqual(CS._MIN_TEXT, 600)

    def test_skips_are_counted_separately_from_failures(self):
        # A thin batch must read as a SOURCE problem, not as the model failing.
        with _reply(GOOD):
            CS.synthesize_row(dict(ROW, raw_text="too short"))
            CS.synthesize_row(dict(ROW, raw_text="also short"))
        self.assertEqual((CS.calls_made(), CS.skipped_thin()), (0, 2))

class TheThingIsNamedByWhatItIsTests(unittest.TestCase):
    """"The call" is our internal shorthand. A reader outside the system does not refer to a
    tender as a call, so generated prose uses the funder's own register — "the request for
    proposals", "the tender" — which is also what makes the text read as though a person wrote
    it about that specific opportunity."""

    def test_the_solicitation_type_decides_the_phrase(self):
        for raw, want in [("RFP", "request for proposals"),
                          ("RFA", "request for applications"),
                          ("CFP", "call for proposals"),
                          ("CfCN", "call for concept notes"),
                          ("EOI", "expression of interest"),
                          ("Tender", "tender"),
                          ("ITB", "invitation to bid")]:
            with self.subTest(raw=raw):
                self.assertEqual(CS._kind_phrase({"solicitation_type": raw}), want)

    def test_the_pursuit_class_is_the_fallback(self):
        self.assertEqual(CS._kind_phrase({"opportunity_type": "Procurement"}), "tender")
        self.assertEqual(CS._kind_phrase({"opportunity_type": "Prize/Challenge"}),
                         "prize competition")
        self.assertEqual(CS._kind_phrase({"opportunity_type": "Consultancy"}),
                         "consultancy assignment")

    def test_nothing_known_falls_back_to_a_neutral_phrase(self):
        # Never "the call": that is the shorthand being removed.
        self.assertEqual(CS._kind_phrase({}), "funding call")
        self.assertNotEqual(CS._kind_phrase({}), "call")

    def test_the_phrase_reaches_the_prompt(self):
        CS.reset_calls()
        with _reply(GOOD) as c:
            CS.synthesize_row(dict(ROW, solicitation_type="Tender"))
        sent = c.client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn("Read this tender", sent)
        self.assertIn('refer to it as "the tender"', sent)


class AnOverviewCannotOutgrowItsSourceTests(unittest.TestCase):
    """The first applied batch wrote 50 overviews and 26 were LONGER than the raw page text
    they came from — worst case 1,346 characters produced from 432, a ratio of 3.1. The
    catalogue explains it: 453 of 686 rows carry under 1,500 characters of source, median 802.
    Asked for "up to 500 words in the publisher's own words" from 400 characters of page text,
    the model fills the gap by elaborating — and the page then presents that elaboration as
    the funder's own account of what they fund.

    A dash is true. Invented prose about somebody's funding programme is not."""

    def setUp(self):
        CS.reset_calls()

    def _long_source(self):
        return ("The programme supports work on health systems in eligible countries. " * 12)

    def test_an_overview_longer_than_its_source_is_discarded(self):
        body = self._long_source()
        padded = "x" * int(len(body) * 3)          # the worst real case was 3.1x
        with _reply('{"full_description": "' + padded + '"}'):
            got = CS.synthesize_row(dict(ROW, raw_text=body))
        # the row still needs enough text to earn a call, but the overview must not survive
        self.assertNotIn("full_description", got)
        self.assertEqual(CS.padded_overviews(), 1)

    def test_a_properly_condensed_overview_survives(self):
        body = self._long_source()
        with _reply('{"full_description": "A short faithful summary of the programme."}'):
            got = CS.synthesize_row(dict(ROW, raw_text=body))
        self.assertIn("full_description", got)
        self.assertEqual(CS.padded_overviews(), 0)

    def test_a_small_expansion_is_tolerated(self):
        # Condensation artefacts — expanding an abbreviation, joining fragments — can push
        # slightly past 1.0, so the allowance is a ratio rather than a hard equality.
        body = "a" * 1000
        with _reply('{"full_description": "' + "b" * 1100 + '"}'):
            got = CS.synthesize_row(dict(ROW, raw_text=body))
        self.assertIn("full_description", got)

    def test_the_other_fields_are_unaffected_by_a_padded_overview(self):
        body = self._long_source()
        with _reply('{"full_description": "' + "z" * 9000 + '", '
                    '"what_is_funded": ["Equipment"]}'):
            got = CS.synthesize_row(dict(ROW, raw_text=body))
        self.assertNotIn("full_description", got)
        self.assertEqual(got["what_is_funded"], "Equipment")

    def test_the_ratio_is_configurable_and_close_to_one(self):
        self.assertGreater(CS._MAX_OVERVIEW_RATIO, 1.0)
        self.assertLessEqual(CS._MAX_OVERVIEW_RATIO, 1.5)


class ProjectStagesAreStoredAsTextTests(unittest.TestCase):
    """`project_stages` is a TEXT column, so returning a Python list made the client
    JSON-encode it — all 42 rows in the first applied batch stored the literal
    `["Implementation"]`. The page survived it because `display_value` untangles a
    JSON-looking string, but a SQL filter, an export or the ML feature builder would each
    read the brackets and quotes as content."""

    def test_stages_come_back_as_joined_text(self):
        self.assertEqual(CS._stages(["Research", "Pilot"]), "Research\nPilot")

    def test_it_is_not_json(self):
        got = CS._stages(["Implementation"])
        self.assertFalse(str(got).startswith("["))
        self.assertEqual(got, "Implementation")

    def test_an_invented_stage_is_still_dropped(self):
        self.assertIsNone(CS._stages(["Interpretive Dance"]))

    def test_nothing_yields_none_not_an_empty_string(self):
        self.assertIsNone(CS._stages([]))
        self.assertIsNone(CS._stages(None))

    def test_the_page_renders_it_as_a_list_either_way(self):
        from core import opportunity_detail as od
        self.assertEqual(od.display_value(CS._stages(["Research", "Pilot"])),
                         "Research\nPilot")
        self.assertEqual(od.as_bullets(CS._stages(["Research", "Pilot"])),
                         ["Research", "Pilot"])


class TheFullestSourceTextIsUsedTests(unittest.TestCase):
    """THE CEILING ON EVERY FIELD IN THIS MODULE. `raw_text` is written by core/extract.py as
    `_page_text or brief_description`, so a row discovered from a listing stores its BRIEF as
    the source text — a couple of sentences. Median stored source is 802 characters and 247
    rows hold under 400, which is why institution types, eligible countries and project stages
    came back empty: the text never mentioned them.

    Sampled live, 6 of 8 thin rows went from under 400 characters to between 1,300 and 5,200
    once the page itself was read. The other two are aggregator paywall stubs — nothing to
    recover, and the thin-text guard still declines to spend a call on them."""

    PAGE = ("<html><head><style>x{}</style></head><body><nav>Home About</nav>"
            "<p>Eligible applicants are NGOs and academic institutions registered in "
            "eligible countries. Only invited organisations may apply.</p>"
            "<footer>Cookies</footer></body></html>")

    def test_navigation_and_scripts_are_stripped(self):
        text = CS.page_text(self.PAGE)
        self.assertIn("Eligible applicants are NGOs", text)
        for junk in ("Home About", "Cookies", "x{}"):
            self.assertNotIn(junk, text)

    def test_the_page_beats_a_short_stored_brief(self):
        body, prov = CS.best_body({"raw_text": "A short brief."}, self.PAGE)
        self.assertIn("Eligible applicants", body)
        self.assertGreater(prov["used"], prov["stored"])

    def test_A_LONGER_STORED_TEXT_IS_NEVER_REPLACED(self):
        # A flaky fetch that returns a cookie banner must not shrink a good row.
        stored = "y" * 6000
        body, _prov = CS.best_body({"raw_text": stored}, self.PAGE)
        self.assertEqual(body, stored)

    def test_no_html_changes_nothing(self):
        body, _p = CS.best_body({"raw_text": "Stored."}, None)
        self.assertEqual(body, "Stored.")

    def test_the_body_is_capped(self):
        body, _p = CS.best_body({"raw_text": ""}, "<p>" + "z" * 40000 + "</p>")
        self.assertLessEqual(len(body), CS._MAX_BODY)

    def test_the_recovered_text_is_written_back(self):
        # So the next pass, and every gate downstream, reads the fuller call instead of each
        # one re-fetching the same page.
        CS.reset_calls()
        row = dict(ROW, raw_text="A short brief.", full_description=None)
        with _reply(GOOD):
            got = CS.synthesize_row(row, html="<p>" + ("Real call detail. " * 60) + "</p>")
        self.assertIn("raw_text", got)
        self.assertGreater(len(got["raw_text"]), len("A short brief.") + CS._BODY_GAIN)

    def test_a_trivial_gain_is_not_written_back(self):
        CS.reset_calls()
        row = dict(ROW, raw_text="x" * 3000)
        with _reply(GOOD):
            got = CS.synthesize_row(row, html="<p>tiny</p>")
        self.assertNotIn("raw_text", got)

    def test_a_paywall_stub_still_costs_no_call(self):
        # The page is as empty as the stored text, so the thin guard still applies AFTER the
        # fetch rather than before it.
        CS.reset_calls()
        with _reply(GOOD) as c:
            CS.synthesize_row(dict(ROW, raw_text="Premium"), html="<p>Premium</p>")
        self.assertEqual(c.calls, 0)
