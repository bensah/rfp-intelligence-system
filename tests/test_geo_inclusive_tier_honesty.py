"""MUST-4 must not claim we are "based in scope" when the country overlap is empty.

THE REPORTED CASE. A UNICEF tender for Bangladesh's Sylhet Division stored
`call_geographic_scope = ['Bangladesh', 'Global / worldwide']`. Against an org
registered in Cameroon and Mali the country overlap is EMPTY, yet MUST-4 read:

    🟢 MUST 4 · Geographic fit — Yes, our own presence · 1/1 · 100%
       via: "registered / based in scope"

`_covers_scope` passes on EITHER a real country overlap OR an inclusive tier
(Global / LMIC / …) that is open to everyone. Both routes returned the same
explanation, so a tier-only match asserted a footprint the org does not have.

Two things are separated here:
  * the SCORE — an org really is eligible for a genuinely worldwide call, so a
    tier-only match still scores 1.0 (changing that auto-Declined real global calls, per
    the standing note in `_covers_scope`); and
  * the EXPLANATION — which must say which route matched.

The stray tag itself is a DATA problem, not a scoring one: `worldwide_ok` already
refuses to tag "United Nations Global Marketplace", and re-extracting that row's own
text today yields ['Bangladesh']. Rows scanned before that guard keep the stale value
because the re-scan merge preserves existing fields — see
scripts/backfill_stale_geo_scope.py.

Run:  python -m unittest tests.test_geo_inclusive_tier_honesty
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                    # noqa: E402
from core import geographies as geo                       # noqa: E402
from core import auto_scorer as AS                        # noqa: E402

ORG = {"org_registered_countries": ["Cameroon", "Mali"],
       "org_operating_countries": ["Cameroon", "Mali"]}
POL = {"countries": {"eligible": ["Cameroon", "Mali"], "broad_terms": []}}

# The reported row, verbatim.
STALE = {"call_geographic_scope": ["Bangladesh", "Global / worldwide"]}
REPAIRED = {"call_geographic_scope": ["Bangladesh"]}


class ExplanationTests(unittest.TestCase):
    def test_a_tier_only_match_does_not_claim_we_are_based_in_scope(self):
        via = CD._geo_presence(ORG, STALE, {}, {})["via"]
        self.assertNotIn("registered / based in scope", via)
        self.assertIn("open to any country", via)

    def test_a_real_country_overlap_still_says_based_in_scope(self):
        rfp = {"call_geographic_scope": ["Cameroon", "Nigeria"]}
        g = CD._geo_presence(ORG, rfp, {}, {})
        self.assertEqual(g["score"], 1.0)
        self.assertEqual(g["via"], "registered / based in scope")

    def test_a_genuinely_worldwide_call_is_still_a_pass(self):
        # Changing the SCORE here auto-Declined real global calls before; only the
        # explanation moved.
        g = CD._geo_presence(ORG, {"call_geographic_scope": ["Global / worldwide"]}, {}, {})
        self.assertEqual(g["score"], 1.0)
        self.assertEqual(g["label"], "Yes, our own presence")
        self.assertIn("open to any country", g["via"])

    def test_an_lmic_tier_is_also_explained_as_a_tier(self):
        g = CD._geo_presence(ORG, {"call_geographic_scope": ["LMICs"]}, {}, {})
        self.assertIn("open to any country", g["via"])

    def test_a_partner_route_keeps_its_own_explanation(self):
        org = {"org_registered_countries": ["Mali"],
               "org_operating_countries": ["Cameroon"]}
        g = CD._geo_presence(org, {"call_geographic_scope": ["Cameroon"]}, {}, {})
        self.assertEqual(g["score"], 0.5)
        self.assertIn("operating country", g["via"])


class CountryOverlapTests(unittest.TestCase):
    def test_an_inclusive_tier_is_not_a_country_overlap(self):
        self.assertFalse(CD._country_overlap(["Cameroon"], ["Global / worldwide"]))
        self.assertFalse(CD._country_overlap(["Cameroon"], ["Bangladesh",
                                                            "Global / worldwide"]))

    def test_a_real_overlap_is_detected(self):
        self.assertTrue(CD._country_overlap(["Cameroon"], ["Cameroon", "Nigeria"]))

    def test_region_expansion_still_counts(self):
        self.assertTrue(CD._country_overlap(["Cameroon"], ["Sub-Saharan Africa"]))

    def test_empty_inputs_are_safe(self):
        self.assertFalse(CD._country_overlap([], ["Cameroon"]))
        self.assertFalse(CD._country_overlap(["Cameroon"], []))
        self.assertFalse(CD._country_overlap(None, None))


class TheStaleTagIsADataProblemTests(unittest.TestCase):
    """The extractor is already correct — these rows predate the guard."""

    UNICEF_TEXT = (
        "To provide comprehensive technical and coordination support to the Integrated "
        "Immunization & Surveillance Systems Strengthening, Digital Optimization & "
        "Evidence Generation Program in Bangladesh, Sylhet Division (Sunamganj, "
        "Habiganj, Moulvibazar and Sylhet City). Eligible applicants are any legal "
        "entities that can register as a UNICEF vendor on the United Nations Global "
        "Marketplace and provide the required corporate documentation.")

    def test_a_platform_name_does_not_open_a_call_worldwide(self):
        self.assertFalse(geo.worldwide_ok("United Nations Global Marketplace"))
        self.assertFalse(geo.worldwide_ok("Global Fund to Fight AIDS"))
        self.assertFalse(geo.worldwide_ok("Global Health EDCTP3 Joint Undertaking"))

    def test_a_genuine_worldwide_phrase_still_does(self):
        self.assertTrue(geo.worldwide_ok("open to applicants worldwide"))
        self.assertTrue(geo.worldwide_ok("organisations from any country may apply"))

    def test_re_extracting_the_reported_row_drops_the_stray_tier(self):
        fresh = AS._extract_call_geographic_scope(self.UNICEF_TEXT, POL)
        self.assertIn("Bangladesh", fresh)
        self.assertNotIn("Global / worldwide", fresh)

    def test_the_repaired_scope_rejects_the_call(self):
        self.assertEqual(CD.derive_geographic_fit(ORG, STALE, {}, {}),
                         "Yes, our own presence")          # the stale, wrong verdict
        self.assertEqual(CD.derive_geographic_fit(ORG, REPAIRED, {}, {}),
                         "No presence there")              # the correct one
        self.assertTrue(CD.fatal_decline(ORG, REPAIRED, {})[0])


class BackfillRuleTests(unittest.TestCase):
    """The backfill only rewrites a scope the row's OWN text no longer supports, and
    never blanks one."""

    def _fresh(self, text):
        return AS._extract_call_geographic_scope(text, POL)

    def test_a_country_only_call_reduces_to_that_country(self):
        self.assertEqual(
            self._fresh("Proposals are invited for work in Bangladesh, Sylhet Division."),
            ["Bangladesh"])

    def test_a_genuinely_global_call_keeps_its_tier(self):
        fresh = self._fresh("This call is open to applicants worldwide, from any country.")
        self.assertIn("Global / worldwide", fresh)

    def test_a_global_call_naming_a_priority_country_keeps_both(self):
        # The regression the standing note warns about: stripping the tier whenever a
        # country is named wrongly auto-Declined these.
        fresh = self._fresh("Open to applicants worldwide; proposals from Cameroon "
                            "are particularly encouraged.")
        self.assertIn("Global / worldwide", fresh)
        self.assertIn("Cameroon", fresh)

    def test_text_with_no_geography_yields_nothing_so_the_row_is_skipped(self):
        self.assertEqual(self._fresh("A call for proposals on health systems."), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
