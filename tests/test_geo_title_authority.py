"""A country in the title is the call's scope. An applicant-country list is a different rule.

THE REPORTED CASE. Every row in one review week named a country the tenant does not work
in, and all of them passed the geography gate:

    EOI - Sierra Leone - Feasibility Study ...
    Modern Slavery Fund Viet Nam Programme 2026 to 2029
    IFB - Nigeria - Asphalt Overlay ...            (and Ethiopia, Tanzania, Cabo Verde x2)

Owner's rule: "if title has a specified country or body explicitly scopes the rfp to a
specific geo region or country, prioritize that for match making and ignore the rest."

WHY THEY PASSED. `geographic_exclusion_reject` ran its development-tier keeper (step 3)
BEFORE its named-country test (step 4). The Viet Nam call was stored with a scope of
['Low- and middle-income countries (LMICs)'], the tier keeper matched 'lmics' and returned
keep - for any tenant on earth, since a tier is a property of the CALL and never of the
tenant. Step 4 was unreachable. The countries had already been found; nothing consulted
them. Separately `_scope_specific_countries` - the input to the owner's
"specific-country-governs" rule, which was already implemented - read only the structured
scope field, so the title was invisible to it too.

THE SECOND RULE. A Finnish government scheme reached the same tenant. No geography gate
could stop it and none should have: it funds projects in ~130 developing markets and the
tenant's country is genuinely one of them. What excludes the tenant is who may APPLY -
Finland-registered operators - and that was extracted, stored in
`eligibility_countries = ['Finland']`, then dropped before any gate could read it. Pooling
the two lists does not work: the 130-country work geography drowns the single country that
decides eligibility. They are separate questions and are now separate checks.

Run:  python -m unittest tests.test_geo_title_authority
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import auto_scorer as A                                     # noqa: E402

POLICIES = {"countries": {"eligible": ["Kenya"]},
            "geographies": {"broad_terms": []}}
LMIC = ["Low- and middle-income countries (LMICs)"]


def _cand(title="", scope=None, **kw):
    c = {"opportunity_title": title, "call_geographic_scope": scope or [],
         "brief_description": "", "focus_theme": "", "funding_agency": ""}
    c.update(kw)
    return c


class TheTitleIsTheScopeTests(unittest.TestCase):
    def test_the_reported_row_is_now_rejected(self):
        cand = _cand("Modern Slavery Fund Viet Nam Programme 2026 to 2029: call for "
                     "proposals", LMIC)
        bad, why = A.geographic_exclusion_reject(cand, POLICIES)
        self.assertTrue(bad)
        self.assertIn("vietnam", why)

    def test_a_tier_no_longer_outranks_a_country_in_the_title(self):
        # The exact inversion: 'LMICs' beside 'Viet Nam' means this call funds an LMIC,
        # not that it is open to all of them.
        self.assertTrue(A.geographic_exclusion_reject(
            _cand("Health programme for Viet Nam", LMIC), POLICIES)[0])

    def test_the_procurement_notice_shape_is_rejected(self):
        for title in ("EOI - Sierra Leone - Feasibility Study of a Tech City",
                      "IFB - Nigeria - Asphalt Overlay and Ancillary Road Works",
                      "SPN - Ethiopia - Procurement of Furniture for Job Centres"):
            self.assertTrue(A.geographic_exclusion_reject(_cand(title), POLICIES)[0], title)

    def test_our_own_country_in_the_title_is_kept(self):
        self.assertFalse(A.geographic_exclusion_reject(
            _cand("EOI - Kenya - Health systems strengthening"), POLICIES)[0])


class WhatMustStillBeKeptTests(unittest.TestCase):
    def test_a_genuinely_tier_open_call_with_no_country_in_the_title(self):
        self.assertFalse(A.geographic_exclusion_reject(
            _cand("Global health innovation fund", LMIC), POLICIES)[0])

    def test_an_incidental_funder_country_stamp_does_not_reject(self):
        # The behaviour step 3's comment was written to protect: a funder-country stamp in
        # the metadata must not override a tier-open call. Reading the TITLE only - rather
        # than all of _geo_text - is what keeps this working.
        cand = _cand("Operating grant: implementation science", LMIC,
                     funding_agency="Canadian Institutes of Health Research Canada")
        self.assertFalse(A.geographic_exclusion_reject(cand, POLICIES)[0])

    def test_a_country_named_only_in_the_body_does_not_reject(self):
        cand = _cand("Implementation research awards", LMIC,
                     brief_description="Building on lessons from work in Bangladesh.")
        self.assertFalse(A.geographic_exclusion_reject(cand, POLICIES)[0])

    def test_a_region_containing_our_country_is_kept(self):
        self.assertFalse(A.geographic_exclusion_reject(
            _cand("Call for proposals: Eastern Africa", ["Eastern Africa"]), POLICIES)[0])

    def test_an_unconfigured_org_geography_still_defers(self):
        self.assertFalse(A.geographic_exclusion_reject(
            _cand("EOI - Nigeria - Roads"), {"countries": {"eligible": []}})[0])


class TheApplicantCountryRuleTests(unittest.TestCase):
    def test_the_finnish_scheme_is_rejected_on_who_may_apply(self):
        # Work geography includes our country and SHOULD keep passing the geo gate; the
        # applicant rule is what closes the call to us.
        cand = _cand("Business Partnership Support",
                     ["Kenya", "Uganda", "Mali"],
                     eligibility_countries=["Finland"])
        self.assertFalse(A.geographic_exclusion_reject(cand, POLICIES)[0])
        bad, why = A.applicant_country_mismatch_reject(cand, POLICIES)
        self.assertTrue(bad)
        self.assertIn("finland", why)

    def test_a_list_that_includes_us_is_not_a_barrier(self):
        cand = _cand("Regional fund", eligibility_countries=["Kenya", "Chad"])
        self.assertFalse(A.applicant_country_mismatch_reject(cand, POLICIES)[0])

    def test_a_double_encoded_json_string_is_still_read(self):
        # Much of the store holds this column as a jsonb STRING, not an array.
        cand = _cand("Business Partnership Support",
                     eligibility_countries='["Finland"]')
        self.assertTrue(A.applicant_country_mismatch_reject(cand, POLICIES)[0])

    def test_a_long_list_is_treated_as_a_mis_filed_work_geography(self):
        # A genuine "who may apply" rule names a handful of countries. 130 is a work
        # geography under the wrong heading, and rejecting on it would lose good calls.
        many = ["Mali", "Ghana", "Benin", "Togo", "Chad", "Niger", "Senegal", "Peru"]
        self.assertFalse(A.applicant_country_mismatch_reject(
            _cand("Developing markets fund", eligibility_countries=many), POLICIES)[0])

    def test_a_region_in_the_list_stands_the_rule_down(self):
        # "applicants from the Global South" is inclusive wording, not a restriction.
        cand = _cand("Fund", eligibility_countries=["Global South", "Finland"])
        self.assertFalse(A.applicant_country_mismatch_reject(cand, POLICIES)[0])

    def test_no_published_list_says_nothing(self):
        self.assertFalse(A.applicant_country_mismatch_reject(
            _cand("Fund", eligibility_countries=None), POLICIES)[0])

    def test_an_unconfigured_org_geography_defers(self):
        cand = _cand("Fund", eligibility_countries=["Finland"])
        self.assertFalse(A.applicant_country_mismatch_reject(
            cand, {"countries": {"eligible": []}})[0])


class TheTwoQuestionsStaySeparateTests(unittest.TestCase):
    def test_scope_countries_include_the_title_but_not_the_applicant_list(self):
        cand = _cand("Health call for Viet Nam", ["Kenya"],
                     eligibility_countries=["Finland"])
        scope = A._scope_specific_countries(cand)
        self.assertIn("vietnam", scope)
        self.assertIn("kenya", scope)
        self.assertNotIn("finland", scope)
        self.assertEqual(A.applicant_countries(cand), {"finland"})

    def test_the_specific_country_rule_now_receives_the_title(self):
        # `_specific_country_mismatch` -> the geo-authority block was already implemented
        # and was only starved of input.
        cand = _cand("Modern Slavery Fund Viet Nam Programme", LMIC)
        self.assertTrue(A._specific_country_mismatch(cand, POLICIES))

    def test_title_countries_reads_the_title_only(self):
        cand = _cand("Implementation awards", LMIC,
                     brief_description="Work in Kenya and Uganda.")
        self.assertEqual(A.title_countries(cand), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
