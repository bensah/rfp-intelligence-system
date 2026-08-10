"""The extraction must STORE what the synthesis already gave it.

`llm_synthesis.synthesize_store()` returns the whole org-neutral read of an RFP —
programme areas, eligibility specifics, compliance requirements, how to apply, and a
structured award value / duration recovered from ranged text. `extract.build_record` read
exactly ONE key off it (`brief_description`) and dropped the rest on the floor.

Measured over 500 catalogue rows before this change: `call_domain_areas` 0, `eligibility_other`
0, `submission_format` 0, `project_duration` 0 — every one of them computed, paid for in
tokens, and discarded. That, not the page layout, is why the opportunity page read empty.

Synthesis is STUBBED here: these tests are about the plumbing, and they must run with no
LLM endpoint configured (there is none in CI).
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

from core import extract as EX                              # noqa: E402

# What the model really returns (core.llm_synthesis.synthesize's `out` dict).
SYNTH = {
    "brief_description": "A clean two-sentence summary of the call.",
    "call_domain_areas": ["Vaccines", "Health systems"],
    "eligibility_specifics": "Applicants must be registered in an eligible country.",
    "compliance_requirements": "Audited financials for the last two years.",
    "how_to_apply": "Submit a concept note through the online portal, then a full proposal.",
    "application_checklist": "Concept note\nBudget\nCVs",
    "call_award_value": 2000000.0,
    "project_duration": 30,
}

CANDIDATE = {
    "opportunity_title": "Strengthening immunisation delivery",
    "opportunity_link": "https://funder.example/calls/immunisation-2027",
    "funding_agency": "A Health Funder",
    "brief_description": "Immunisation delivery grants for eligible organisations.",
    "opportunity_type": "Grant/funding call",
    "call_submission_deadline": "2027-03-31",
    "_page_text": ("The Funder invites proposals to strengthen immunisation delivery. "
                   "Awards of up to $2 million are available over 24-36 months. "
                   "Applicants must be registered in an eligible country and provide "
                   "audited financials. Submit a concept note via the online portal. "
                   * 6),
}


def _record(synth=SYNTH, candidate=None, use_llm=True, side_effect=None):
    """build_record with synthesis stubbed and the eligibility gate satisfied.

    The FUNCTION is patched, not the module. `build_record` does a function-local
    `from core import llm_synthesis`, which resolves through the already-imported `core`
    package attribute — so swapping sys.modules only worked when this file ran alone and
    silently fell through to the real (disabled) module under the full suite.
    """
    cand = dict(candidate or CANDIDATE)
    kw = ({"side_effect": side_effect} if side_effect
          else {"return_value": synth})
    with mock.patch("core.llm_synthesis.synthesize_store", **kw):
        with mock.patch.object(EX, "is_eligible", return_value=(True, "")):
            rec, reason = EX.build_record(cand, {}, use_llm=use_llm)
    return rec, reason


class TheSynthesisFieldsAreStoredTests(unittest.TestCase):
    def test_a_record_is_built_at_all(self):
        rec, reason = _record()
        self.assertIsNotNone(rec, reason)

    def test_programme_areas_are_kept(self):
        rec, _ = _record()
        self.assertEqual(rec["call_domain_areas"], ["Vaccines", "Health systems"])

    def test_eligibility_other_carries_the_specifics_and_the_compliance(self):
        rec, _ = _record()
        self.assertIn("registered in an eligible country", rec["eligibility_other"])
        self.assertIn("Audited financials", rec["eligibility_other"])

    def test_submission_format_carries_how_to_apply(self):
        rec, _ = _record()
        self.assertIn("concept note", rec["submission_format"])

    def test_the_brief_is_still_the_synthesised_one(self):
        rec, _ = _record()
        self.assertEqual(rec["brief_description"],
                         "A clean two-sentence summary of the call.")

    def test_the_duration_the_model_read_is_kept(self):
        rec, _ = _record()
        self.assertEqual(rec["project_duration"], 30)


class TheStructuralExtractorStillWinsTests(unittest.TestCase):
    """The synthesis FILLS blanks; it must never override a figure read off the page."""

    def test_a_regex_award_value_is_not_replaced_by_the_model(self):
        cand = dict(CANDIDATE,
                    _page_text="Awards of exactly $750,000 are available. " * 20)
        rec, _ = _record(candidate=cand)
        # Whatever the structural extractor found, the model's 2,000,000 must not win.
        if rec.get("grant_amount"):
            self.assertNotEqual(rec["grant_amount"], 2000000.0)

    def test_the_model_amount_fills_a_blank(self):
        rec, _ = _record()
        # The prose says "up to $2 million" as a RANGE the regex can miss.
        self.assertTrue(rec["grant_amount"])


class NoSynthesisIsStillSafeTests(unittest.TestCase):
    """No LLM endpoint, a disabled pass, or a failed call must degrade quietly — the
    record still stores everything the structural extraction found."""

    def test_synthesis_returning_none_leaves_the_fields_blank_not_broken(self):
        rec, reason = _record(synth=None)
        self.assertIsNotNone(rec, reason)
        self.assertIsNone(rec["brief_description"])
        self.assertIsNone(rec["call_domain_areas"])
        self.assertIsNone(rec["eligibility_other"])
        self.assertIsNone(rec["submission_format"])

    def test_use_llm_false_skips_synthesis_entirely(self):
        rec, reason = _record(use_llm=False)
        self.assertIsNotNone(rec, reason)
        self.assertIsNone(rec["call_domain_areas"])

    def test_a_synthesis_that_raises_does_not_lose_the_record(self):
        rec, reason = _record(side_effect=RuntimeError("LLM timeout"))
        self.assertIsNotNone(rec, reason)
        self.assertIsNone(rec["brief_description"])

    def test_placeholder_answers_are_treated_as_blank(self):
        rec, _ = _record(synth={"brief_description": "A summary.",
                                "eligibility_specifics": "None stated",
                                "compliance_requirements": "n/a",
                                "how_to_apply": "  "})
        self.assertIsNone(rec["eligibility_other"])
        self.assertIsNone(rec["submission_format"])

    def test_empty_programme_areas_store_null_not_an_empty_list(self):
        # "[]" in the column is what made the page render a literal empty list.
        rec, _ = _record(synth={"brief_description": "x", "call_domain_areas": []})
        self.assertIsNone(rec["call_domain_areas"])


class EveryWrittenKeyIsAStoreColumnTests(unittest.TestCase):
    def test_the_record_writes_nothing_the_table_would_reject(self):
        from core import extracted_store
        rec, _ = _record()
        self.assertTrue(set(rec) <= extracted_store._COLS,
                        set(rec) - extracted_store._COLS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
