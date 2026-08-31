"""PREFER-8 portal familiarity must credit the federal pair on a US-federal call.

THE REPORTED CASE (uid BE-260831-1210). A US Department of State APS, sourced from a
grants.gov listing, scored:

    ✗ Familiar with the donor's portal

for an org whose profile lists `sam.gov` among its portal registrations. The host-only
match compared the org's `sam.gov` against the call's hosts — the `opportunity_link`
is a grants.gov URL and `donor_website` is state.gov — and `sam.gov != grants.gov`, so
nothing matched, even though grants.gov + sam.gov ARE the federal submission pair.

US-federal submission runs through grants.gov + sam.gov together. A registration on
EITHER is genuine familiarity with how the call is filed. `_registered_on_portal` now
trusts `_is_us_federal` for that credit — the same signal registration-region and
SAM/UEI already lean on — so a program/bureau link host (state.gov) no longer hides it.

Run:  python -m unittest tests.test_prefer8_portal_us_federal
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                      # noqa: E402

# A US Department of State call, listed on grants.gov, funder site state.gov. No
# donor_submission_portal_url — exactly the shape that defeated the host-only match.
US_FEDERAL_RFP = {
    "funding_agency": "USDoS - US Department of State",
    "opportunity_link": "https://www.grants.gov/search-results-detail/363649",
    "source": "migration",
    "brief_description": "US-Cameroon Health MOU implementation.",
}
USDOS_DONOR = {"donor": "US Department of State", "donor_website": "https://www.state.gov"}

# Org holds sam.gov (not grants.gov), and has no relationship with this funder.
ORG_SAM = {"org_donor_registrations": ["gatesfoundation.org", "gavi.org", "sam.gov"]}
ORG_GRANTS = {"org_donor_registrations": ["grants.gov"]}
ORG_NEITHER = {"org_donor_registrations": ["ec.europa.eu", "wellcome.org"]}

# A non-federal call — the new path must NOT fire here.
EU_RFP = {"funding_agency": "European Commission",
          "opportunity_link": "https://ec.europa.eu/info/funding-tenders/opportunities/x"}


class PortalUsFederalTests(unittest.TestCase):
    def test_sam_registration_credits_a_us_federal_call(self):
        self.assertTrue(CD._is_us_federal(US_FEDERAL_RFP))
        self.assertTrue(CD._registered_on_portal(ORG_SAM, US_FEDERAL_RFP, USDOS_DONOR))

    def test_grants_registration_also_credits_it(self):
        self.assertTrue(CD._registered_on_portal(ORG_GRANTS, US_FEDERAL_RFP, USDOS_DONOR))

    def test_the_component_reads_met(self):
        f = {i["key"]: i for i in
             CD._competitiveness_factors(ORG_SAM, US_FEDERAL_RFP, USDOS_DONOR)}["comp_portal"]
        self.assertTrue(f["met"])

    def test_no_federal_registration_still_fails(self):
        # A federal call, but the org is on neither federal portal → honest ✗.
        self.assertFalse(CD._registered_on_portal(ORG_NEITHER, US_FEDERAL_RFP, USDOS_DONOR))

    def test_non_federal_call_is_unaffected(self):
        # sam.gov must not spuriously credit an EU call — the new path is gated on
        # _is_us_federal, and the host match still requires a real host overlap.
        self.assertFalse(CD._is_us_federal(EU_RFP))
        self.assertFalse(CD._registered_on_portal(ORG_SAM, EU_RFP,
                                                  {"donor_website": "https://ec.europa.eu"}))


if __name__ == "__main__":
    unittest.main()
