"""to_candidate must carry the call's applicant-country restriction (eligibility_countries).

The opportunity/catalogue page scores a row via opportunity_detail.to_candidate → analyse.
to_candidate previously DROPPED eligibility_countries, so a country-restricted call (e.g. the
Wellcome Accelerator Awards, applicants must be UK-based) lost that restriction on the
catalogue page and scored a rosy 100% off the DONOR's general scope — while the screening
engine (which keeps eligibility_countries) had correctly kept it out of the pipeline. The two
engines disagreed. Carrying eligibility_countries lets MUST-1's applicant_countries gate read
the same fact, so the catalogue view and the screened pipeline agree.

Run:  python -m unittest tests.test_to_candidate_eligibility_countries
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import opportunity_detail as OD                   # noqa: E402
from core import criteria_derive as CD                      # noqa: E402


class ToCandidateEligibilityCountriesTests(unittest.TestCase):
    def test_eligibility_countries_is_carried(self):
        row = {"opportunity_name": "Wellcome Accelerator Awards", "funder_name": "Wellcome",
               "call_geographic_scope": [], "eligibility_countries": ["United Kingdom"]}
        cand = OD.to_candidate(row)
        self.assertEqual(cand.get("eligibility_countries"), ["United Kingdom"])

    def test_blank_eligibility_countries_is_dropped(self):
        cand = OD.to_candidate({"opportunity_name": "X", "eligibility_countries": []})
        self.assertNotIn("eligibility_countries", cand)     # blanks still filtered

    def test_uk_only_call_reads_not_eligible_for_a_non_uk_org(self):
        # The end-to-end point: a UK-applicant restriction now reaches MUST-1 on the
        # catalogue page, so a Cameroon org reads "No, not eligible" instead of a false pass.
        org = {"org_registered_countries": ["Cameroon", "Mali"],
               "org_operating_countries": ["Cameroon", "Mali"]}
        cand = OD.to_candidate({"opportunity_name": "Wellcome Accelerator Awards",
                                "funder_name": "Wellcome",
                                "eligibility_countries": ["United Kingdom"]})
        # A globally-scoped donor must NOT rescue a call whose applicants must be UK-based.
        donor = {"donor_geographic_scope": ["Sub-Saharan Africa", "Global"]}
        q = {i["key"]: i for i in CD.qualification_factors(org, cand, donor, {})}
        ac = q["applicant_countries"]
        self.assertTrue(ac["active"])
        self.assertEqual(ac["score"], 0.0)                  # Cameroon org not UK-eligible
        self.assertEqual(CD.derive_qualification(org, cand, donor, {}), "No, not eligible")

    def test_org_in_the_eligible_country_passes(self):
        org = {"org_registered_countries": ["United Kingdom"],
               "org_operating_countries": ["United Kingdom"]}
        cand = OD.to_candidate({"opportunity_name": "X", "funder_name": "Wellcome",
                                "eligibility_countries": ["United Kingdom"]})
        q = {i["key"]: i for i in CD.qualification_factors(org, cand, {}, {})}
        self.assertEqual(q["applicant_countries"]["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
