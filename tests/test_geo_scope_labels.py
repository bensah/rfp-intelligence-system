"""A label that names nowhere is not a place we are absent from.

THE LIVE FAULT. A call whose extraction reads ["Sub-Saharan Africa"] carried ["Regional"] on
its pipeline row. "Regional" expands to nothing, so MUST-4 found no overlap, scored 0, and the
fatal gate DECLINED the call — for an organisation registered inside that very region.

The containment logic was never the problem: `geographies.expand("Sub-Saharan Africa")` already
yields all 50 member countries, and the org's own are among them. The problem is that a
form-filler's placeholder was being read as a geography.

An unstated scope is what MUST-4 already knows how to handle — "Not sure", excluded from the
count, Park for review. That is very different from "we are not there", which is a fatal gate.
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as cd          # noqa: E402
from core import geographies as geo             # noqa: E402

ORG = {"org_registered_countries": ["Countryland"], "org_operating_countries": ["Countryland"]}


class ParentRegionsAlreadyContainTheirCountriesTests(unittest.TestCase):
    """Asserted first, because it is what proves the fault was the label and not the geography
    model — a broad region has always resolved to its members."""

    def test_a_region_expands_to_its_member_countries(self):
        ssa = geo.expand(["Sub-Saharan Africa"])
        self.assertGreater(len(ssa), 40)
        self.assertIn("kenya", ssa)
        self.assertIn("ghana", ssa)

    def test_an_income_tier_expands_too(self):
        self.assertIn("kenya", geo.expand(["Low- and middle-income countries (LMICs)"]))

    def test_a_country_inside_the_region_counts_as_covered(self):
        self.assertTrue(cd._covers_scope(["Kenya"], ["Sub-Saharan Africa"]))
        self.assertTrue(cd._covers_scope(["Ghana"], ["Low- and middle-income countries (LMICs)"]))

    def test_a_country_outside_it_does_not(self):
        self.assertFalse(cd._covers_scope(["Norway"], ["Sub-Saharan Africa"]))


class APlaceholderIsNotAPlaceTests(unittest.TestCase):
    def _label(self, scope):
        return cd._geo_presence(ORG, {"call_geographic_scope": scope}, None, {})

    def test_the_reported_value_no_longer_declines_a_call(self):
        g = self._label(["Regional"])
        self.assertEqual(g["label"], "Not sure")
        self.assertFalse(g["active"])          # excluded from the count, not scored 0

    def test_the_other_form_filler_placeholders_behave_the_same(self):
        for label in ("Multiple countries", "Various", "Country-specific", "Multi-country",
                      "Not specified", "N/A", "Other"):
            with self.subTest(label=label):
                self.assertEqual(self._label([label])["label"], "Not sure")

    def test_A_REAL_SCOPE_BESIDE_A_PLACEHOLDER_STILL_COUNTS(self):
        # Dropping the label must not drop the row's real geography with it.
        g = self._label(["Regional", "Countryland"])
        self.assertTrue(g["active"])
        self.assertEqual(g["score"], 1.0)

    def test_somewhere_we_are_genuinely_absent_still_scores_zero(self):
        # The guard must not turn a real miss into "Not sure" — that would hide true
        # ineligibility, which is worse than the bug it fixes.
        g = self._label(["Norway"])
        self.assertTrue(g["active"])
        self.assertEqual(g["score"], 0.0)
        self.assertEqual(g["label"], "No presence there")

    def test_a_placeholder_only_scope_is_not_a_fatal_gate(self):
        self.assertFalse(cd.fatal_decline(ORG, {"call_geographic_scope": ["Regional"]},
                                          None, {})[0])

    def test_a_genuine_absence_still_is_one(self):
        self.assertTrue(cd.fatal_decline(ORG, {"call_geographic_scope": ["Norway"]},
                                         None, {})[0])


class TheGroupingPrefixIsDisplayOnlyTests(unittest.TestCase):
    """The classifier files a programme area under a group — "Cross-cutting - Digital Health".
    The group is how the area is FILED and is what the matcher reads; on a page it is noise."""

    def test_the_prefix_is_dropped_for_display(self):
        from core import opportunity_detail as od
        self.assertEqual(
            od.format_programme_areas(["Cross-cutting - Digital Health (+AI)",
                                       "Cross-cutting - Diagnostics"]),
            "Digital Health (+AI), Diagnostics")

    def test_duplicates_created_by_stripping_collapse(self):
        from core import opportunity_detail as od
        self.assertEqual(
            od.format_programme_areas(["Cross-cutting - Research", "Research"]), "Research")

    def test_other_labels_are_untouched(self):
        from core import opportunity_detail as od
        self.assertEqual(od.format_programme_areas(["Unspecified Program Area"]),
                         "Unspecified Program Area")

    def test_nothing_yields_nothing(self):
        from core import opportunity_detail as od
        self.assertEqual(od.format_programme_areas([]), "")
