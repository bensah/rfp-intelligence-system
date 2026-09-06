"""US STATE government funders are domestic → rejected for a non-US deployment.

The reported leak: an "AIDS Clinical Guidelines Program RFA" funded by the New York State
Department of Health / AIDS Institute (a state-level, NY-only program) landed in a Cameroon
tenant's pipeline. Its landing page stated no country and no "domestic" phrase — the
restriction was only IMPLIED by the funder identity — so it defaulted to "Global" and passed.

The fix detects a US STATE government funder and rejects it, WITHOUT touching US FEDERAL
agencies (USDoS / USAID / NIH / CDC), which fund international work, and WITHOUT tripping on
the country Georgia.

Run:  python -m unittest tests.test_us_state_agency_reject
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import auto_scorer as A                           # noqa: E402


class UsStateAgencyFunderTests(unittest.TestCase):
    def test_matches_us_state_agencies(self):
        for t in [
            "New York State Department of Health / AIDS Institute",
            "New York State AIDS Institute clinical guidelines program",
            "State of California Department of Public Health",
            "Commonwealth of Massachusetts Department of Public Health",
            "Texas State Health and Human Services Commission",
        ]:
            self.assertTrue(A.us_state_agency_funder(t), t)

    def test_does_not_match_federal_or_international(self):
        for t in [
            "US Department of State (GHSD) Annual Program Statement — Cameroon health MOU",
            "United States Agency for International Development global health RFA",
            "National Institutes of Health international research award",
            "Centers for Disease Control and Prevention global HIV program",
            "European Commission Horizon call open to all countries",
        ]:
            self.assertFalse(A.us_state_agency_funder(t), t)

    def test_country_georgia_is_not_a_us_state_hit(self):
        # The COUNTRY Georgia must not trip it — only the US STATE agency form does.
        self.assertFalse(A.us_state_agency_funder(
            "A maternal-health project in the Republic of Georgia and Armenia"))
        self.assertTrue(A.us_state_agency_funder("Georgia State Department of Public Health"))
        self.assertTrue(A.us_state_agency_funder("State of Georgia Department of Health"))


class UsDomesticRejectIntegrationTests(unittest.TestCase):
    def test_ny_state_call_is_rejected(self):
        cand = {"funding_agency": "Health Research, Inc.",
                "notes": "New York State Department of Health / AIDS Institute. Develop and "
                         "maintain clinical guidelines for HIV/AIDS care in New York State.",
                "opportunity_link": "https://www.healthresearch.org/aids-clinical-guidelines-rfa/"}
        rejected, reason = A.us_domestic_only_reject(cand, {})
        self.assertTrue(rejected)
        self.assertIn("STATE", reason)

    def test_inclusive_foreign_eligibility_is_not_rejected(self):
        # If a (hypothetical) state-worded call explicitly welcomed foreign orgs, keep it.
        cand = {"funding_agency": "New York State Department of Health",
                "notes": "Foreign and domestic organizations are eligible to apply."}
        rejected, _ = A.us_domestic_only_reject(cand, {})
        self.assertFalse(rejected)


if __name__ == "__main__":
    unittest.main()
