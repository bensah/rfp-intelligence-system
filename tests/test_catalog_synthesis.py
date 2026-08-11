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
        with _reply('{"project_stages": ["Implementation", "Interpretive Dance"]}'):
            got = CS.synthesize_row(dict(ROW))
        self.assertEqual(got["project_stages"], ["Implementation"])

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
