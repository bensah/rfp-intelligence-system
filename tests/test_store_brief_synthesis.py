"""Regression tests for store brief synthesis (BUG 3).

extract.build_record must store a CLEAN synthesised brief_description (never the raw
attachment text), and leave it NULL when synthesis is unavailable — the raw text stays in
raw_text for grounding/backfill.

Pure unit test — llm_synthesis.synthesize_store is monkeypatched; no network, no LLM.

Run:  python -m unittest tests.test_store_brief_synthesis
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# No live LLM judge in build_record's gap path (keeps the test hermetic + fast).
os.environ.pop("LLM_JUDGE_BASE_URL", None)
os.environ.pop("LLM_SYNTH_BASE_URL", None)

from core import extract               # noqa: E402
from core import llm_synthesis         # noqa: E402
from core.policies import DEFAULT_POLICIES  # noqa: E402

_RAW = ("[General_conditions_of_contract.pdf] GENERAL CONDITIONS OF CONTRACT. "
        "THE UNITED NATIONS OFFICE FOR PROJECT SERVICES HEREBY INVITES QUALIFIED "
        "SUPPLIERS TO SUBMIT A BID FOR THE SUPPLY AND DELIVERY OF OPERATING TABLES "
        "AND DELIVERY BEDS. " * 6)

_CANDIDATE = {
    "opportunity_title": "Supply and delivery of operating tables and delivery beds",
    "opportunity_link": "https://www.ungm.org/Public/Notice/123456",
    "funding_agency": "UNOPS",
    "brief_description": _RAW,
    "_page_text": _RAW,
    "call_submission_deadline": "2099-08-17",
    "call_award_value": 500000,
    "currency": "USD",
    "call_geographic_scope": ["Kenya"],
}

_CLEAN = ("UNOPS is inviting suppliers to bid for the supply and delivery of operating "
          "tables and delivery beds in Kenya. Bids are due by 17 August 2099.")


class StoreBriefTests(unittest.TestCase):
    def setUp(self):
        self._orig = llm_synthesis.synthesize_store

    def tearDown(self):
        llm_synthesis.synthesize_store = self._orig

    def test_store_brief_is_synthesised_not_raw(self):
        llm_synthesis.synthesize_store = lambda cand: {"brief_description": _CLEAN}
        rec, reason = extract.build_record(dict(_CANDIDATE), DEFAULT_POLICIES, use_llm=True)
        self.assertIsNotNone(rec, reason)
        self.assertEqual(rec["brief_description"], _CLEAN)
        self.assertNotIn("[General_conditions", rec["brief_description"])
        # Raw text is preserved separately for grounding / backfill.
        self.assertIn("GENERAL CONDITIONS", rec["raw_text"])

    def test_brief_is_null_when_synthesis_unavailable(self):
        llm_synthesis.synthesize_store = lambda cand: None      # disabled / capped / failed
        rec, reason = extract.build_record(dict(_CANDIDATE), DEFAULT_POLICIES, use_llm=True)
        self.assertIsNotNone(rec, reason)
        self.assertIsNone(rec["brief_description"])             # NULL, never the raw text
        self.assertIn("GENERAL CONDITIONS", rec["raw_text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
