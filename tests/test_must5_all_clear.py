"""MUST-5 must not certify compliance nobody checked.

THE REPORTED CASE. On a non-US call that imposed no compliance or co-financing
requirement at all, MUST-5 read:

    🟢 MUST 5 · Cofinancing & compliance — Yes, fully met · 1/1 · 100%

Twelve components were built; exactly ONE was active — SAM.gov/UEI — and it was itself a
permissive default pass. SAM/UEI applies only to US-federal funding, so on that call it
was scoring a rule the funder never made. Because it was the only always-active
component, MUST-5's active set was never empty, `derive_cofinancing` never returned "Not
sure", and the criterion contributed FULL MUST-5 weight toward Proceed on the strength of
a placeholder.

THE MODEL (owner 2026-08-06). MUST-5 components are strict eligibility rules that exist
only when the call or donor intel states them.
  · SAM/UEI is EXCLUDED entirely unless the call is US-federal or the donor demands it —
    out of the denominator, not a free pass inside it.
  · Nothing stated at all → ONE explicit component, "All compliance & co-financing
    requirements met" = 1/1, shown alone. A full pass, because we must not eliminate a
    strong-fit RFP over data the funder never published — but a pass that is visible as
    one thing rather than implied by a default hiding among greyed rows.
  · One or more stated → those alone form the denominator. Hard gates, no middle ground:
    org holds it → 1, doesn't → 0.

Run:  python -m unittest tests.test_must5_all_clear
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                          # noqa: E402
from core.scorer import criterion_score                         # noqa: E402

ALL_CLEAR = "compliance_all_clear"
# A non-US call from a funder whose record imposes nothing — the common case.
PLAIN_CALL = {"opportunity_title": "Global collaboration action on climate and health",
              "opportunity_link": "https://donor.org/call", "brief_description": "A grant."}
US_FEDERAL = {"opportunity_title": "Notice of funding opportunity",
              "opportunity_link": "https://www.grants.gov/web/grants/view-opportunity.html",
              "brief_description": "US federal award."}
ORG = {"org_tax_exempt": True, "org_has_audited_financials": True,
       "org_cofinancing_capacity": "strong"}


def _by_key(items):
    return {i["key"]: i for i in items}


def _active(items):
    return [i for i in items if i["active"] and i["score"] is not None]


def _ratio(org, rfp, donor):
    act = _active(CD.compliance_factors(org, rfp, donor, {}))
    num = sum(i["score"] for i in act)
    return num, len(act)


class TheReportedCaseTests(unittest.TestCase):
    def test_a_call_that_imposes_nothing_no_longer_leans_on_sam_uei(self):
        items = CD.compliance_factors(ORG, PLAIN_CALL, {}, {})
        self.assertFalse(_by_key(items)["sam_uei"]["active"],
                         "SAM/UEI is US-federal only — it must be out of the denominator")

    def test_the_pass_now_comes_from_one_explicit_component(self):
        act = _active(CD.compliance_factors(ORG, PLAIN_CALL, {}, {}))
        self.assertEqual([i["key"] for i in act], [ALL_CLEAR])
        self.assertEqual(act[0]["score"], 1.0)
        self.assertEqual(CD.component_mark(act[0])[0], "✓")

    def test_it_still_reads_as_a_full_pass(self):
        # A strong-fit RFP must not be eliminated over data the funder never published.
        self.assertEqual(CD.derive_cofinancing(ORG, PLAIN_CALL, {}), "Yes, fully met")
        self.assertEqual(_ratio(ORG, PLAIN_CALL, {}), (1.0, 1))

    def test_the_all_clear_row_says_why(self):
        ac = _by_key(CD.compliance_factors(ORG, PLAIN_CALL, {}, {}))[ALL_CLEAR]
        self.assertIn("no compliance or co-financing requirement stated", ac["_detail"])

    def test_the_all_clear_is_never_a_fatal_gate(self):
        ac = _by_key(CD.compliance_factors(ORG, PLAIN_CALL, {}, {}))[ALL_CLEAR]
        self.assertFalse(ac["fatal"])


class SamUeiScopeTests(unittest.TestCase):
    def test_a_us_federal_call_still_activates_it(self):
        sam = _by_key(CD.compliance_factors(ORG, US_FEDERAL, {}, {}))["sam_uei"]
        self.assertTrue(sam["active"])
        self.assertEqual(sam["score"], 0.0)          # this org holds no SAM/UEI

    def test_an_explicit_donor_demand_activates_it_on_any_call(self):
        donor = {"donor_sam_uei_registration_required": True}
        sam = _by_key(CD.compliance_factors(ORG, PLAIN_CALL, donor, {}))["sam_uei"]
        self.assertTrue(sam["active"])

    def test_an_org_holding_sam_uei_passes_it(self):
        org = {**ORG, "org_has_sam_uei": True}
        sam = _by_key(CD.compliance_factors(org, US_FEDERAL, {}, {}))["sam_uei"]
        self.assertEqual(sam["score"], 1.0)

    def test_a_us_federal_call_retires_the_all_clear(self):
        act = _active(CD.compliance_factors(ORG, US_FEDERAL, {}, {}))
        self.assertNotIn(ALL_CLEAR, [i["key"] for i in act])
        self.assertEqual([i["key"] for i in act], ["sam_uei"])

    def test_the_us_federal_call_now_fails_instead_of_passing_on_a_default(self):
        self.assertEqual(CD.derive_cofinancing(ORG, US_FEDERAL, {}), "Not met")
        self.assertEqual(_ratio(ORG, US_FEDERAL, {}), (0.0, 1))


class OneStatedRequirementTests(unittest.TestCase):
    """"If only 1 component appears from call/donor we use that alone to compute the
    denominator: no match → 0 on 1, match → 1 on 1." """

    TAX = {"donor_tax_exempt_status_required": True}

    def test_the_stated_requirement_alone_forms_the_denominator(self):
        act = _active(CD.compliance_factors(ORG, PLAIN_CALL, self.TAX, {}))
        self.assertEqual([i["key"] for i in act], ["tax_exempt"])

    def test_a_match_is_one_on_one_and_passes(self):
        self.assertEqual(_ratio(ORG, PLAIN_CALL, self.TAX), (1.0, 1))
        self.assertEqual(CD.derive_cofinancing(ORG, PLAIN_CALL, self.TAX), "Yes, fully met")

    def test_a_mismatch_is_zero_on_one_and_fails(self):
        org = {**ORG, "org_tax_exempt": False}
        self.assertEqual(_ratio(org, PLAIN_CALL, self.TAX), (0.0, 1))
        self.assertEqual(CD.derive_cofinancing(org, PLAIN_CALL, self.TAX), "Not met")
        self.assertEqual(criterion_score(CD.derive_cofinancing(org, PLAIN_CALL, self.TAX)), 0)

    def test_any_stated_requirement_retires_the_all_clear(self):
        for donor in ({"donor_tax_exempt_status_required": True},
                      {"donor_audited_financials_required": True},
                      {"donor_safeguarding_policy_required": True},
                      {"donor_partnership_mandatory": True},
                      {"donor_govt_mou_required": True},
                      {"donor_cost_sharing_match_required": True}):
            ac = _by_key(CD.compliance_factors(ORG, PLAIN_CALL, donor, {}))[ALL_CLEAR]
            self.assertFalse(ac["active"], donor)
            self.assertIsNone(ac["score"], donor)


class SeveralStatedRequirementsTests(unittest.TestCase):
    DONOR = {"donor_tax_exempt_status_required": True,
             "donor_audited_financials_required": True,
             "donor_safeguarding_policy_required": True}

    def test_only_the_stated_ones_count(self):
        act = _active(CD.compliance_factors(ORG, PLAIN_CALL, self.DONOR, {}))
        self.assertEqual(sorted(i["key"] for i in act),
                         ["audited_financials", "safeguarding", "tax_exempt"])

    def test_one_unmet_gate_fails_the_criterion(self):
        # ORG holds tax-exempt + audited financials but no safeguarding policy.
        self.assertEqual(_ratio(ORG, PLAIN_CALL, self.DONOR), (2.0, 3))
        self.assertEqual(CD.derive_cofinancing(ORG, PLAIN_CALL, self.DONOR), "Not met")

    def test_holding_all_of_them_passes(self):
        org = {**ORG, "org_has_safeguarding_policy": True}
        self.assertEqual(_ratio(org, PLAIN_CALL, self.DONOR), (3.0, 3))
        self.assertEqual(CD.derive_cofinancing(org, PLAIN_CALL, self.DONOR), "Yes, fully met")


class OverrideTests(unittest.TestCase):
    """A reviewer can assert a requirement the derivation never saw. The all-clear
    default must then RETIRE rather than sit beside it inflating the denominator."""

    def test_an_override_retires_the_all_clear(self):
        bd = CD.factor_breakdown(PLAIN_CALL, ORG, {}, {},
                                 overrides={"cofinancing": {"tax_exempt": 0.0}})
        by = _by_key(bd["cofinancing"])
        self.assertTrue(by["tax_exempt"]["active"])
        self.assertFalse(by[ALL_CLEAR]["active"])
        act = _active(bd["cofinancing"])
        self.assertEqual([i["key"] for i in act], ["tax_exempt"])

    def test_without_overrides_the_all_clear_survives_the_breakdown(self):
        bd = CD.factor_breakdown(PLAIN_CALL, ORG, {}, {})
        self.assertTrue(_by_key(bd["cofinancing"])[ALL_CLEAR]["active"])

    def test_a_reviewer_may_override_the_all_clear_itself(self):
        bd = CD.factor_breakdown(PLAIN_CALL, ORG, {}, {},
                                 overrides={"cofinancing": {ALL_CLEAR: 0.0}})
        ac = _by_key(bd["cofinancing"])[ALL_CLEAR]
        self.assertTrue(ac["_override"])
        self.assertEqual(ac["score"], 0.0)           # the human verdict is not overwritten


class NoRegressionTests(unittest.TestCase):
    def test_the_bid_strength_helper_agrees_with_the_factor_list(self):
        for donor in ({}, {"donor_tax_exempt_status_required": True}):
            num, den = CD.cofinancing_bid_strength(ORG, PLAIN_CALL, donor, {})
            self.assertEqual((num, den), _ratio(ORG, PLAIN_CALL, donor), donor)

    def test_must5_never_auto_declines(self):
        # Every MUST-5 gate is acquirable before the deadline — none is fatal.
        org = {"org_tax_exempt": False}
        donor = {"donor_tax_exempt_status_required": True}
        self.assertFalse(CD.fatal_decline(org, PLAIN_CALL, donor)[0])
        for f in CD.compliance_factors(org, PLAIN_CALL, donor, {}):
            self.assertFalse(f["fatal"], f["key"])

    def test_cofinancing_keeps_its_partial_band(self):
        # The ONE soft component: 'limited' capacity is a real middle, not a hard gate.
        org = {"org_cofinancing_capacity": "limited"}
        donor = {"donor_cost_sharing_match_required": True}
        self.assertEqual(_by_key(CD.compliance_factors(org, PLAIN_CALL, donor, {}))
                         ["cofinance"]["score"], 0.5)
        self.assertEqual(CD.derive_cofinancing(org, PLAIN_CALL, donor),
                         "Partial, with effort")

    def test_the_feature_key_is_appended_last(self):
        from core.features import COMPONENT_FEATURE_NAMES as N
        self.assertEqual(N[-1], "cmp_compliance_all_clear")
        self.assertIn("cmp_sam_uei", N)              # retired-from-default, not removed


if __name__ == "__main__":
    unittest.main(verbosity=2)
