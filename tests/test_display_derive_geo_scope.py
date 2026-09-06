"""DISPLAY-ONLY: surface eligibility_countries under "Geographic scope" when work-scope is blank.

When a call publishes no work-geography but restricts applicants by country
(eligibility_countries), the opportunity page's "Geographic scope" field was blank next to an
explicit "Eligible countries: United Kingdom" — confusing. standard_view now fills it (tagged
"(applicants)") so the two agree. This is display-only: the scoring path (to_candidate →
analyse) is untouched, so it never double-counts into MUST-4.

Run:  python -m unittest tests.test_display_derive_geo_scope
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


class DisplayDeriveGeoScopeTests(unittest.TestCase):
    def _view(self, row):
        return OD.standard_view(OD.KIND_CATALOG, row, extraction=row)

    def test_blank_scope_derives_from_eligibility_countries(self):
        v = self._view({"opportunity_name": "Wellcome Accelerator Awards",
                        "call_geographic_scope": [], "eligibility_countries": ["United Kingdom"]})
        self.assertEqual(v.get("call_geographic_scope"), ["United Kingdom (applicants)"])

    def test_it_is_display_only_scoring_is_untouched(self):
        # to_candidate (what analyse scores) must NOT gain a work-scope from this.
        cand = OD.to_candidate({"opportunity_name": "X", "call_geographic_scope": [],
                                "eligibility_countries": ["United Kingdom"]})
        self.assertNotIn("call_geographic_scope", cand)     # blank stayed blank for scoring

    def test_a_real_work_scope_is_left_alone(self):
        v = self._view({"opportunity_name": "X",
                        "call_geographic_scope": ["Cameroon"],
                        "eligibility_countries": ["United Kingdom"]})
        self.assertEqual(v.get("call_geographic_scope"), ["Cameroon"])   # not overwritten

    def test_no_eligibility_countries_leaves_scope_blank(self):
        v = self._view({"opportunity_name": "X", "call_geographic_scope": []})
        self.assertIn(OD._blank(v.get("call_geographic_scope")), (True,))


if __name__ == "__main__":
    unittest.main()
