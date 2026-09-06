"""The opportunity/catalogue page runs the SAME screening gate and reports the reason.

Two halves of the fix:
  1. to_candidate carries the fields the reject gates read (notes / opportunity_type /
     eligibility_applicant_types / source) so the catalogue candidate is a faithful input to
     `auto_scorer.is_eligible` — the same function the scan uses.
  2. analyse() runs is_eligible and returns `screened_out` + `screen_reason`, so the page can
     explain WHY a call is (or would be) kept out of the pipeline instead of only scoring it.

(The true-positive behaviour — a UK-only call → screened_out=True with a geography reason — is
verified against the live catalogue row; this suite covers the plumbing, which is what
regressed before.)

Run:  python -m unittest tests.test_opportunity_screening_reason
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import opportunity_detail as OD                    # noqa: E402
from core import opportunity_scoring as OSC                  # noqa: E402
from core import auto_scorer as A                            # noqa: E402


class ToCandidateCarriesGateFieldsTests(unittest.TestCase):
    def test_reject_gate_fields_are_carried(self):
        row = {"opportunity_name": "X", "funder_name": "Wellcome",
               "eligibility_other": "Applicants must be UK-based.",
               "opportunity_type": "Grant/funding call",
               "eligibility_applicant_types": ["Individuals"], "source": "auto"}
        cand = OD.to_candidate(row)
        self.assertEqual(cand.get("notes"), "Applicants must be UK-based.")
        self.assertEqual(cand.get("opportunity_type"), "Grant/funding call")
        self.assertEqual(cand.get("eligibility_applicant_types"), ["Individuals"])
        self.assertEqual(cand.get("source"), "auto")


class IsEligibleReturnsAReasonTests(unittest.TestCase):
    def test_a_uk_scoped_call_is_rejected_on_geography(self):
        # The scan engine itself, with an eligible-countries policy that excludes the UK.
        cand = {"opportunity_title": "Wellcome Accelerator Awards", "funding_agency": "Wellcome",
                "opportunity_link": "https://wellcome.org/x",
                "eligibility_countries": ["United Kingdom"],
                "call_geographic_scope": ["United Kingdom"]}
        policies = {"countries": {"eligible": ["Cameroon", "Mali"]}}
        ok, reason = A.is_eligible(cand, policies, geo_org_gates=True, theme_gate=False)
        self.assertFalse(ok)
        self.assertTrue(reason)


class AnalyseExposesScreeningResultTests(unittest.TestCase):
    def test_result_carries_screened_out_and_reason_keys(self):
        an = OSC.analyse({"opportunity_title": "X", "funding_agency": "Wellcome"},
                         {"org_registered_countries": ["Cameroon"]}, None, {})
        self.assertIn("screened_out", an)
        self.assertIn("screen_reason", an)
        self.assertIsInstance(an["screened_out"], bool)


if __name__ == "__main__":
    unittest.main()
