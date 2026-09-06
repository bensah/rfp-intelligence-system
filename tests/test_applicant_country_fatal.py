"""A CLEAN applicant-country restriction the org fails is a fatal eligibility miss.

`eligibility_countries` (who may APPLY, by country) feeds MUST-1's applicant_countries gate.
It is NON-fatal in general — the field is often noisy LLM prose ("EU Member States",
"African malaria-endemic countries", "England") and a hard gate there auto-Declined valid
calls. But when the list is a CLEAN, canonical, bounded set of recognised countries the org
is not in (e.g. the Wellcome Accelerator Awards: applicants must be UK-based), that IS a real
ineligibility → it now auto-Declines so the call ranks at the bottom instead of scoring a
false "Strong fit".

Run:  python -m unittest tests.test_applicant_country_fatal
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                       # noqa: E402

ORG = {"org_registered_countries": ["Cameroon", "Mali"],
       "org_operating_countries": ["Cameroon", "Mali"]}
# A globally-scoped funder must not rescue a country-restricted call.
DONOR = {"donor_geographic_scope": ["Sub-Saharan Africa", "Global"]}


def _ac(rfp):
    return {i["key"]: i for i in CD.qualification_factors(ORG, rfp, DONOR, {})}["applicant_countries"]


class ApplicantCountryFatalTests(unittest.TestCase):
    def test_clean_single_country_the_org_fails_is_fatal(self):
        rfp = {"eligibility_countries": ["United Kingdom"]}
        self.assertTrue(_ac(rfp)["_fatal_country"])
        self.assertTrue(CD.fatal_decline(ORG, rfp, DONOR, {})[0])

    def test_clean_bounded_list_the_org_fails_is_fatal(self):
        rfp = {"eligibility_countries": ["United States", "United Kingdom", "Canada"]}
        self.assertTrue(CD.fatal_decline(ORG, rfp, DONOR, {})[0])

    def test_org_in_the_eligible_set_is_not_fatal(self):
        rfp = {"eligibility_countries": ["Cameroon", "Nigeria"]}
        self.assertFalse(_ac(rfp)["_fatal_country"])
        self.assertFalse(CD.fatal_decline(ORG, rfp, DONOR, {})[0])

    def test_vague_prose_stays_non_fatal(self):
        for elig in (["EU Member States"], ["African malaria-endemic countries"],
                     ["England"]):
            rfp = {"eligibility_countries": elig}
            self.assertFalse(_ac(rfp)["_fatal_country"], elig)
            self.assertFalse(CD.fatal_decline(ORG, rfp, DONOR, {})[0], elig)

    def test_inclusive_tier_stays_non_fatal(self):
        for elig in (["Global"], ["LMIC"], ["Sub-Saharan Africa"]):
            rfp = {"eligibility_countries": elig}
            self.assertFalse(_ac(rfp)["_fatal_country"], elig)

    def test_no_restriction_is_inactive(self):
        it = _ac({})
        self.assertFalse(it["active"])
        self.assertFalse(it.get("_fatal_country"))


if __name__ == "__main__":
    unittest.main()
