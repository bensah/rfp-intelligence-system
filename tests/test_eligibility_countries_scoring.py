"""`eligibility_countries` is WHO MAY APPLY — which is MUST-1's question, not MUST-4's.

Schema §4.4 defines the field as "who may apply (may differ from work geography)". Across the
82 live catalogue rows carrying both it and a work geography, 37 disagree — one call takes
applications only from India for work in Africa, several only from EU member states for work in
sub-Saharan Africa. So the two must not be collapsed:

  * MUST-1 gets a component of its own for the apply-list.
  * MUST-4 keeps scoring the WORK geography, and only falls back to the apply-list when the
    call and the donor both state none (14 live rows).

The MUST-1 component is deliberately NON-FATAL. The list is model-written prose — "England",
"EU Member States", "African malaria-endemic countries" — and scored as a hard gate it
auto-Declined all 33 live rows carrying one, including a call open to Sub-Saharan Africa for an
org present in two Sub-Saharan countries. Telling a precise place-list from prose reliably
enough to justify an invisible auto-Decline is not something this can do, so it lowers the score
and stays visible instead.
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

from core import criteria_derive as cd                      # noqa: E402

ORG = {"org_registered_countries": ["Kenya", "Zambia"],
       "org_operating_countries": ["Kenya", "Zambia"],
       "org_entity_type": "nonprofit / ngo"}


def _component(rfp: dict, org: dict = None, key: str = "applicant_countries") -> dict | None:
    items = cd.qualification_factors(org or ORG, rfp, {}, {})
    return next((i for i in items if i["key"] == key), None)


class TheApplyListLandsOnMust1Tests(unittest.TestCase):
    def test_a_named_country_the_org_is_not_in_fails_the_component(self):
        c = _component({"eligibility_countries": ["Finland"]})
        self.assertTrue(c["active"])
        self.assertEqual(c["score"], 0.0)

    def test_a_region_covering_the_org_passes(self):
        c = _component({"eligibility_countries": ["Sub-Saharan Africa"]})
        self.assertEqual(c["score"], 1.0)

    def test_no_apply_list_leaves_the_component_inactive(self):
        self.assertFalse(_component({})["active"])
        self.assertFalse(_component({"eligibility_countries": []})["active"])

    def test_it_uses_the_shared_coverage_helper_so_operating_presence_counts(self):
        # Same registered/operating/inclusive-tier matching as item D — not a second
        # implementation of geography that could drift from it.
        org = {"org_registered_countries": [], "org_operating_countries": ["Zambia"]}
        self.assertEqual(_component({"eligibility_countries": ["Zambia"]}, org)["score"], 1.0)


class ItNeverAutoDeclinesTests(unittest.TestCase):
    """The whole reason the component is shaped this way."""

    def test_an_unmatched_apply_list_is_not_a_fatal_gate(self):
        rfp = {"eligibility_countries": ["Finland"]}
        self.assertIn("applicant_countries", cd._NON_FATAL_QUALIFICATION)
        self.assertFalse(cd.fatal_decline(ORG, rfp, None, {})[0])

    def test_model_prose_that_resolves_to_nothing_still_does_not_decline(self):
        for phrase in ("African malaria-endemic countries", "Eureka countries",
                       "All countries except mainland China", "EU Member States"):
            with self.subTest(phrase=phrase):
                rfp = {"eligibility_countries": [phrase]}
                self.assertFalse(cd.fatal_decline(ORG, rfp, None, {})[0])

    def test_it_still_costs_score_so_the_finding_is_not_silently_dropped(self):
        self.assertEqual(_component({"eligibility_countries": ["Finland"]})["score"], 0.0)


class ItemDStaysDonorDrivenTests(unittest.TestCase):
    """Registration region is a DONOR requirement. Feeding the apply-list into it would put a
    prose list behind a fatal gate by the back door, and double-count it against the new
    component."""

    def test_the_apply_list_does_not_become_a_registration_requirement(self):
        d = _component({"eligibility_countries": ["Finland"]}, key="local_registration")
        self.assertFalse(d["active"])

    def test_a_real_donor_requirement_still_gates(self):
        items = cd.qualification_factors(ORG, {}, {"donor_registration_region": ["Finland"]}, {})
        d = next(i for i in items if i["key"] == "local_registration")
        self.assertTrue(d["active"])
        self.assertEqual(d["score"], 0.0)


class Must4UsesTheWorkGeographyTests(unittest.TestCase):
    def test_a_stated_work_geography_always_wins(self):
        # apply=India, work=Kenya -> MUST-4 must read Kenya. This is the 37-row disagreement.
        self.assertEqual([s.lower() for s in cd._geo_scope(
            {"call_geographic_scope": ["Kenya"], "eligibility_countries": ["India"]}, None)],
            ["kenya"])

    def test_the_donor_fallback_still_precedes_the_apply_list(self):
        self.assertEqual([s.lower() for s in cd._geo_scope(
            {"eligibility_countries": ["India"]}, {"donor_geographic_scope": ["Zambia"]})],
            ["zambia"])

    def test_the_apply_list_is_used_only_when_both_are_silent(self):
        self.assertEqual([s.lower() for s in cd._geo_scope(
            {"eligibility_countries": ["Kenya"]}, {})], ["kenya"])
        self.assertEqual(cd._geo_scope({}, {}), [])


class TheComponentNamesTheCountriesTests(unittest.TestCase):
    """A verdict on model-written prose has to show its basis, or an arguable one can't be
    spotted and overridden."""

    def test_the_countries_appear_in_the_component_name(self):
        self.assertEqual(_component({"eligibility_countries": ["United Kingdom", "Ireland"]})["name"],
                         "Eligible to apply (United Kingdom, Ireland)")

    def test_a_long_list_is_summarised(self):
        self.assertEqual(cd._applicant_countries_label(["Norway", "Spain", "Ukraine", "France"]),
                         "Eligible to apply (Norway, Spain, Ukraine +1 more)")

    def test_an_empty_list_keeps_a_readable_name(self):
        self.assertEqual(cd._applicant_countries_label([]), "Eligible to apply")


class NonAsciiHyphensDoNotDefeatGeographyTests(unittest.TestCase):
    """The model writes "Sub‑Saharan Africa" with U+2011. Every geography lookup is a string
    match, so the odd hyphen alone turned a covered region into a miss — which under a fatal
    reading was an auto-Decline for a call the org IS eligible for."""

    def test_a_non_breaking_hyphen_still_matches_the_region(self):
        self.assertEqual(_component({"eligibility_countries": ["Sub‑Saharan Africa"]})["score"], 1.0)

    def test_every_dash_variant_normalises(self):
        for dash in ("‐", "‑", "‒", "–", "—", "−"):
            with self.subTest(dash=dash):
                self.assertEqual(cd._ascii_dashes(f"Sub{dash}Saharan Africa"), "Sub-Saharan Africa")


class TheMust4FallbackNeverAutoDeclinesTests(unittest.TestCase):
    """The fallback re-introduced the fatal problem one criterion along: with no work geography,
    the same prose reached `geo_presence`, which IS a fatal gate. A non-match on a
    fallback-derived scope has to Park, not Decline."""

    def test_prose_the_org_does_not_match_parks_instead_of_declining(self):
        rfp = {"eligibility_countries": ["African malaria-endemic countries"]}
        self.assertEqual(cd.derive_geographic_fit(ORG, rfp, {}, None), "Not sure")
        self.assertFalse(cd.fatal_decline(ORG, rfp, None, {})[0])

    def test_a_real_country_the_org_is_not_in_also_parks_when_it_is_only_the_apply_list(self):
        # "Applications from Finland" is not a statement that the WORK is in Finland.
        rfp = {"eligibility_countries": ["Finland"]}
        self.assertEqual(cd.derive_geographic_fit(ORG, rfp, {}, None), "Not sure")

    def test_it_says_why_rather_than_claiming_no_scope_was_stated(self):
        g = cd._geo_presence(ORG, {"eligibility_countries": ["Finland"]}, None, {})
        self.assertIn("not where the work happens", g["via"])
        self.assertEqual(g["scope"], ["Finland"])       # the scope is still reported

    def test_a_MATCHING_fallback_scope_still_scores(self):
        # Declining to guess applies to the negative only — a positive match is informative.
        self.assertEqual(cd.derive_geographic_fit(
            ORG, {"eligibility_countries": ["Kenya"]}, {}, None), "Yes, our own presence")

    def test_a_STATED_work_geography_still_auto_declines_as_before(self):
        # The safeguard must not weaken MUST-4 where the call really does say where the work is.
        rfp = {"call_geographic_scope": ["Finland"], "eligibility_countries": ["Kenya"]}
        self.assertEqual(cd.derive_geographic_fit(ORG, rfp, {}, None), "No presence there")
        self.assertTrue(cd.fatal_decline(ORG, rfp, None, {})[0])

    def test_the_scope_source_is_reported(self):
        self.assertEqual(cd._geo_scope_with_source({"call_geographic_scope": ["Kenya"]}, None)[1],
                         "call")
        self.assertEqual(cd._geo_scope_with_source({}, {"donor_geographic_scope": ["Kenya"]})[1],
                         "donor")
        self.assertEqual(cd._geo_scope_with_source({"eligibility_countries": ["Kenya"]}, None)[1],
                         "apply_list")
        self.assertEqual(cd._geo_scope_with_source({}, None)[1], "none")


class OneFactIsNotCountedTwiceTests(unittest.TestCase):
    def test_the_apply_list_scores_on_must1_only(self):
        # Removing the geographic-scope registration proxy on 2026-08-07 was about exactly this:
        # one fact behind two criteria inflates its weight and misnames the blocker.
        rfp = {"eligibility_countries": ["Finland"]}
        self.assertEqual(_component(rfp)["score"], 0.0)                     # MUST-1: counted
        self.assertFalse(cd._geo_presence(ORG, rfp, None, {})["active"])    # MUST-4: excluded


if __name__ == "__main__":
    unittest.main(verbosity=2)
