"""An award size must be an AWARD size, and a portal account is not a relationship.

THE REPORTED CASE. A digital-health call from an EU Joint Undertaking scored:

    🟡 PREFER 6 · Funding quality — Moderate · 2/3 · 67%
       ✓ At/above your minimum target size
       ✗ Within your absorptive ceiling
    🟡 PREFER 7 · Donor relationship — Some contact · 1/1 · 100%
       ✓ Shared collaborator or registered

Both readings were wrong, for unrelated reasons.

1. THE AWARD SIZE WAS THE WHOLE CALL. The portal's structured budget was written
   straight into `call_award_value`, but that number is the programme ENVELOPE — the
   scraper sums every action's per-year budget. The topic funds 8 projects at about
   EUR 2.25M each; the figure stored against it was EUR 33M, the two topics of that
   call added together. PREFER-6 then measured a EUR 2.25M award against the org's
   absorptive ceiling and reported a call it can comfortably take on as eight times
   too big. The envelope has a column of its own (`total_program_funding`); the
   per-award value is only claimed when the portal states one.

   The same confusion existed in the free-text extractor, which took the LARGEST
   award-context figure. A page carrying both "total indicative budget EUR 18 000 000"
   and "EUR 2.25 million per project" always yielded the envelope, because the
   envelope is always the bigger number.

2. A PORTAL ACCOUNT IS NOT CONTACT WITH A FUNDER. `rel_contact` was
   `_shared_collaborator(...) or _registered_on_portal(...)`. The org is registered on
   the EU Funding & Tenders Portal, so EVERY EU call in the catalogue satisfied
   PREFER-7 — including funders nobody here has ever contacted or been funded by.
   Portal familiarity keeps its place in PREFER-8, where it is a real submission edge;
   PREFER-7 asks whether we know the FUNDER.

Run:  python -m unittest tests.test_award_size_and_relationship
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                              # noqa: E402
from core.scraper import _extract_amount, _per_award_value          # noqa: E402

# The real topic, from the funder's own work programme: an EUR 18.0M envelope funding
# 8 projects at EUR 2.25M each. EUR 33M is that topic plus the other one in the call.
TOPIC_TEXT = (
    "The total indicative budget for the topic is EUR 18 000 000. "
    "The Joint Undertaking estimates that an EU contribution of around EUR 2.25 million "
    "per project would allow these outcomes to be addressed appropriately. "
    "Number of projects expected to be funded: 8."
)


class ThePerAwardValueTests(unittest.TestCase):
    def test_an_envelope_on_its_own_is_not_an_award_size(self):
        # The reported row: a total, and nothing saying how it is shared out. Guessing
        # is worse than declining to answer — a missing value is excluded from scoring,
        # a present one is measured against the org's band as though it were fact.
        self.assertIsNone(_per_award_value(33_000_000, None, None, None))

    def test_an_envelope_divides_by_the_grants_the_call_expects_to_make(self):
        self.assertEqual(_per_award_value(18_000_000, None, None, 8), 2_250_000)

    def test_a_stated_per_grant_ceiling_beats_the_division(self):
        # Same HIGHEST-of-a-range rule the text extractor follows.
        self.assertEqual(_per_award_value(18_000_000, 1_000_000, 2_250_000, 8), 2_250_000)

    def test_a_floor_is_used_when_no_ceiling_is_stated(self):
        self.assertEqual(_per_award_value(18_000_000, 900_000, None, None), 900_000)

    def test_no_budget_at_all_stays_unknown(self):
        self.assertIsNone(_per_award_value(None, None, None, None))


class TheTextExtractorTests(unittest.TestCase):
    def test_a_per_project_figure_beats_a_larger_envelope(self):
        val, cur = _extract_amount("", TOPIC_TEXT)
        self.assertEqual(val, 2_250_000)
        self.assertEqual(cur, "EUR")

    def test_an_ordinary_single_award_page_is_unchanged(self):
        # No envelope language: the figure is the award, exactly as before.
        self.assertEqual(
            _extract_amount("", "Total funding available: USD 500,000 for the "
                                "successful applicant.")[0],
            500_000)

    def test_a_range_still_keeps_its_ceiling(self):
        self.assertEqual(
            _extract_amount("", "Awards of USD 50,000 to USD 100,000 per grant.")[0],
            100_000)

    def test_a_shared_pot_with_no_award_stated_reads_as_unknown(self):
        # Saying "USD 5,000,000" here would be a tenfold overstatement of the award.
        self.assertIsNone(
            _extract_amount("", "A total budget of USD 5,000,000 is available "
                                "across 10 projects.")[0])


class ThePortalIsNotARelationshipTests(unittest.TestCase):
    # An org registered on the shared EU submission portal and nothing more.
    ORG = {"org_donor_registrations": ["ec.europa.eu (EU Funding & Tenders Portal)"],
           "trusted_partners": [], "partners": [],
           "org_funder_history": [], "org_active_donors": []}
    RFP = {"funding_agency": "Global Health EDCTP3 Joint Undertaking",
           "opportunity_link": "https://ec.europa.eu/info/funding-tenders/opportunities/"
                               "portal/screen/opportunities/topic-details/X"}
    DONOR = {"donor": "Global Health EDCTP3 (Horizon Europe)",
             "canonical_key": "global_health_edctp3",
             "donor_website": "https://www.global-health-edctp3.europa.eu"}

    def _contact(self, org):
        facts = CD._relationship_factors(org, self.RFP, self.DONOR)
        return next(f for f in facts if f["key"] == "rel_contact")

    def test_a_portal_account_no_longer_claims_contact_with_the_funder(self):
        self.assertFalse(self._contact(self.ORG)["met"])

    def test_a_genuinely_shared_partner_still_counts(self):
        org = dict(self.ORG, trusted_partners=["Regional Health Institute"])
        donor = dict(self.DONOR,
                     donor_funders_collaborators=["Regional Health Institute"])
        facts = CD._relationship_factors(org, self.RFP, donor)
        self.assertTrue(next(f for f in facts if f["key"] == "rel_contact")["met"])

    def test_the_component_says_what_it_now_measures(self):
        self.assertEqual(self._contact(self.ORG)["name"],
                         "Shared collaborator with this funder")

    def test_portal_familiarity_is_still_available_to_prefer_8(self):
        # Removed from PREFER-7 only. PREFER-8 asks a different question — can we work
        # this submission system — and there the registration is a real edge.
        self.assertTrue(CD._registered_on_portal(self.ORG, self.RFP, self.DONOR))


class TheFundingQualityOutcomeTests(unittest.TestCase):
    ORG = {"org_min_target": 50_000, "org_max_target": 5_000_000}

    def test_the_true_award_sits_inside_the_band(self):
        rfp = {"call_award_value": 2_250_000, "currency": "USD"}
        facts = {f["key"]: f for f in CD._funding_quality_factors(rfp, self.ORG)}
        self.assertTrue(facts["fq_floor"]["met"])
        self.assertTrue(facts["fq_ceiling"]["met"])

    def test_an_unsized_call_is_not_sure_rather_than_over_the_ceiling(self):
        # The point of the whole change: no stated award value must never render as a
        # measured failure against the ceiling.
        rfp = {"call_award_value": None, "currency": "EUR"}
        facts = {f["key"]: f for f in CD._funding_quality_factors(rfp, self.ORG)}
        self.assertIsNone(facts["fq_ceiling"]["met"])
        self.assertIsNone(facts["fq_floor"]["met"])
        self.assertEqual(CD.derive_funding_quality(rfp, self.ORG), "Not sure")

    def test_the_envelope_would_have_failed_the_ceiling(self):
        # Guards the regression: this is what the reported row scored.
        rfp = {"call_award_value": 33_000_000, "currency": "USD"}
        facts = {f["key"]: f for f in CD._funding_quality_factors(rfp, self.ORG)}
        self.assertFalse(facts["fq_ceiling"]["met"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
