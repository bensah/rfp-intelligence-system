"""Regression tests for the geo eligibility gate (BUG 2).

Owner rule (2026-08-01): at screening ingest a geo mismatch is HARD-REJECTED (dropped,
never inserted as a Decline). A SPECIFIC named country governs over a broad region; a
geo-SILENT call falls back to the donor's declared geography; permissive only when both
the call AND the donor are geo-silent. LLM context-reasoning is the authority; the regex
gates are a weak pre-pass.

Pure unit tests — no network. The LLM path is exercised by monkeypatching
core.llm_judge.is_enabled / judge; the deterministic path runs with the LLM disabled.

Run:  python -m unittest tests.test_geo_eligibility
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Keep the deterministic path deterministic: no live LLM endpoint by default.
os.environ.pop("LLM_JUDGE_BASE_URL", None)

from core import geographies as geo          # noqa: E402
from core import auto_scorer as A            # noqa: E402
from core import llm_judge                   # noqa: E402

_POLICY = {
    "countries": {"eligible": ["Cameroon"],
                  "broad_terms": ["Sub-Saharan Africa"],
                  "permissive_when_silent": True},
    "themes": {"required_any": []},
}


def _screen(scope, *, donor_geo=None, brief="Supply of medical equipment.",
            llm_adjudicate=True):
    cand = {"opportunity_title": "RFQ", "opportunity_link": "https://x.org/rfq",
            "brief_description": brief, "call_geographic_scope": scope, "_page_text": brief}
    if donor_geo is not None:
        cand["_donor_geo"] = donor_geo
    return A.is_eligible(cand, _POLICY, geo_org_gates=True, theme_gate=False,
                         llm_adjudicate=llm_adjudicate)


class CanonicalGeoTests(unittest.TestCase):
    def test_iso_inverted_names_deinvert(self):
        self.assertEqual(geo.canonical_geo("Congo, The Democratic Republic of the"),
                         "Congo (DRC)")
        self.assertEqual(geo.canonical_geo("Congo, Republic of the"),
                         "Congo (Brazzaville)")          # DRC vs Rep. of Congo kept distinct
        self.assertEqual(geo.canonical_geo("Korea, Republic of"), "South Korea")
        self.assertEqual(geo.canonical_geo("Tanzania, United Republic of"), "Tanzania")
        self.assertEqual(geo.canonical_geo("Iran (Islamic Republic of)"), "Iran")

    def test_non_inverted_terms_unchanged(self):
        self.assertEqual(geo.canonical_geo("Cameroon"), "Cameroon")
        self.assertEqual(geo.canonical_geo("Sub-Saharan Africa"), "Sub-Saharan Africa")
        self.assertEqual(geo.canonical_geo("Kenya, Uganda"), "Kenya, Uganda")  # a list, left as-is


class DeterministicGeoGateTests(unittest.TestCase):
    """LLM disabled → the deterministic pre-pass + owner-rule fallbacks."""

    def setUp(self):
        os.environ.pop("LLM_JUDGE_BASE_URL", None)

    def test_named_foreign_country_rejected(self):
        self.assertFalse(_screen(["Congo (DRC)"])[0])
        self.assertFalse(_screen(["Samoa"])[0])

    def test_specific_country_beats_broad_region(self):
        ok, reason = _screen(["Congo (DRC)", "Sub-Saharan Africa"])
        self.assertFalse(ok, reason)               # DRC governs over SSA → reject

    def test_org_country_with_region_kept(self):
        self.assertTrue(_screen(["Cameroon", "Sub-Saharan Africa"])[0])
        self.assertTrue(_screen(["Cameroon"])[0])

    def test_region_only_kept(self):
        self.assertTrue(_screen(["Sub-Saharan Africa"])[0])   # no specific country named

    def test_silent_call_gated_on_donor_geo(self):
        # Donor funds a geography that excludes the org → reject.
        self.assertFalse(_screen([], donor_geo={"terms": {"Kenya"}, "global": False})[0])
        # Donor funds a containing region → keep.
        self.assertTrue(_screen([], donor_geo={"terms": {"Sub-Saharan Africa"},
                                               "global": False})[0])
        # Donor funds worldwide → keep.
        self.assertTrue(_screen([], donor_geo={"terms": set(), "global": True})[0])
        # Both call AND donor silent → permissive keep.
        self.assertTrue(_screen([], donor_geo=None)[0])


class LlmAuthorityTests(unittest.TestCase):
    """LLM verdict is authoritative on the ambiguous cases."""

    def setUp(self):
        self._orig_enabled = llm_judge.is_enabled
        self._orig_judge = llm_judge.judge
        llm_judge.is_enabled = lambda: True

    def tearDown(self):
        llm_judge.is_enabled = self._orig_enabled
        llm_judge.judge = self._orig_judge

    def test_llm_can_rescue_incidental_country(self):
        # LLM judges the named country incidental → country_eligible True → keep, even
        # though the deterministic precedence rule would have rejected DRC+SSA.
        llm_judge.judge = lambda cand, pol, model=None: {"country_eligible": True}
        ok, reason = _screen(["Congo (DRC)", "Sub-Saharan Africa"])
        self.assertTrue(ok, reason)

    def test_llm_rejects_silent_call(self):
        # LLM judges the org not eligible for a silent call → hard reject.
        llm_judge.judge = lambda cand, pol, model=None: {"country_eligible": False}
        ok, reason = _screen([])
        self.assertFalse(ok, reason)
        self.assertIn("LLM", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
