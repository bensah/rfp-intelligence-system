"""Geographic fit asks two questions, and the second one is the one we were missing.

THE CASE: a Fondation Pierre Fabre call funds health work in the Global South and requires
applicants to be headquartered in Occitania, France. An organisation registered in Cameroon
and operating across the Global South matched on the work geography, scored 81/100 and
reached a review week. The disqualifying sentence was in the call's own brief description
and in the LLM's key-risks; scoring never looked at it, because MUST-4 only ever asked
where the work happens.

Run:  python -m unittest tests.test_geo_fit
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import geo_fit        # noqa: E402

ORG = {"org_registered_countries": ["Cameroon"],
       "org_operating_countries": ["Cameroon", "Nigeria"]}

FPF_CALL = {"opportunity_title": "Support for Associations in Occitania for Health in the South",
            "call_geographic_scope": ["Global South"],
            "eligibility_countries": ["France"]}


class TheReportedCase(unittest.TestCase):
    def test_the_call_that_leaked_now_fails_the_applicant_component(self):
        result = geo_fit.evaluate(ORG, FPF_CALL)
        self.assertEqual(result["denom"], 2)            # the call poses both questions
        applicant = result["components"][0]
        self.assertTrue(applicant["active"])
        self.assertEqual(applicant["score"], geo_fit.NO_MATCH)
        self.assertEqual(result["score"], 1.0)          # work geography still matches
        self.assertEqual(result["label"], "Not eligible to apply from here")

    def test_the_label_does_not_claim_we_lack_presence(self):
        # We work exactly where the money goes; we are simply barred from applying. Saying
        # "No presence there" would send a reviewer to check the wrong thing.
        self.assertNotEqual(geo_fit.evaluate(ORG, FPF_CALL)["label"], "No presence there")

    def test_the_reason_names_the_restriction(self):
        why = geo_fit.evaluate(ORG, FPF_CALL)["why"]
        self.assertIn("applicants must be based in France", why)


class ApplicantComponent(unittest.TestCase):
    def test_a_call_that_states_nothing_drops_the_component(self):
        # Never invent a restriction the funder did not write: an ordinary call must score
        # out of 1, not 1 out of 2.
        result = geo_fit.evaluate(ORG, {"call_geographic_scope": ["Cameroon"]})
        self.assertEqual(result["denom"], 1)
        self.assertFalse(result["components"][0]["active"])
        self.assertEqual(result["score"], 1.0)

    def test_being_registered_where_applicants_must_be_scores_full(self):
        call = {"call_geographic_scope": ["Global South"], "eligibility_countries": ["Cameroon"]}
        result = geo_fit.evaluate(ORG, call)
        self.assertEqual((result["score"], result["denom"]), (2.0, 2))
        self.assertEqual(result["label"], "Yes, our own presence")

    def test_a_partner_registered_there_scores_the_partner_tier(self):
        org = {**ORG, "partners": [{"name": "Local NGO", "country": "France",
                                    "partner_type": "implementing", "status": "active"}]}
        result = geo_fit.evaluate(org, FPF_CALL)
        self.assertEqual(result["components"][0]["score"], geo_fit.VIA_PARTNER)
        self.assertEqual(result["score"], 1.5)

    def test_an_explicit_applicant_field_wins_over_the_eligibility_list(self):
        call = {**FPF_CALL, "call_applicant_base_scope": ["Cameroon"]}
        self.assertEqual(geo_fit.applicant_component(ORG, call)["score"], geo_fit.OWN)


class OperationsComponent(unittest.TestCase):
    def test_silence_from_call_and_donor_reads_as_global_and_passes(self):
        # Owner's rule: geography missing everywhere → global → this component is 1/1.
        result = geo_fit.evaluate(ORG, {"opportunity_title": "Open call"})
        self.assertEqual((result["score"], result["denom"]), (1.0, 1))
        self.assertEqual(result["components"][1]["source"], "unstated")

    def test_the_donor_scope_is_used_only_when_the_call_is_silent(self):
        donor = {"donor_geographic_scope": ["Nigeria"]}
        stated = geo_fit.operations_component(ORG, {"call_geographic_scope": ["India"]}, donor)
        self.assertEqual(stated["source"], "call")      # donor must not widen the call
        self.assertEqual(stated["score"], geo_fit.NO_MATCH)
        silent = geo_fit.operations_component(ORG, {}, donor)
        self.assertEqual(silent["source"], "donor")
        self.assertEqual(silent["score"], geo_fit.OWN)

    def test_work_somewhere_we_are_not_scores_zero(self):
        result = geo_fit.evaluate(ORG, {"call_geographic_scope": ["India"]})
        self.assertEqual((result["score"], result["denom"]), (0.0, 1))
        self.assertEqual(result["label"], "No presence there")

    def test_a_label_only_scope_is_treated_as_unstated(self):
        # "Regional" is not a place; a row scoped only that way must not read as somewhere
        # we are not.
        result = geo_fit.operations_component(ORG, {"call_geographic_scope": ["Regional"]})
        self.assertEqual(result["source"], "unstated")
        self.assertEqual(result["score"], geo_fit.OWN)


class UnconfiguredOrgTests(unittest.TestCase):
    """An organisation that has recorded no countries is UNKNOWN, not disqualified. Scoring
    that zero once auto-Declined every scoped call for a tenant mid-onboarding."""

    def test_no_recorded_geography_scores_nothing_rather_than_zero(self):
        result = geo_fit.evaluate({}, FPF_CALL)
        self.assertEqual(result["denom"], 0)
        self.assertEqual(result["label"], "Not sure")
        self.assertTrue(all(not c["active"] for c in result["components"]))

    def test_an_unconfigured_org_on_an_unstated_call_still_passes(self):
        # Nothing is claimed about us and nothing is required of us.
        result = geo_fit.evaluate({}, {"opportunity_title": "Open call"})
        self.assertEqual((result["score"], result["denom"]), (1.0, 1))


class ShapeTests(unittest.TestCase):
    def test_bid_strength_matches_the_scorer_contract(self):
        self.assertEqual(geo_fit.bid_strength(ORG, FPF_CALL), (1.0, 2))
        self.assertEqual(geo_fit.bid_strength(ORG, {"call_geographic_scope": ["Cameroon"]}),
                         (1.0, 1))

    def test_semicolon_and_pipe_separated_strings_are_accepted(self):
        call = {"call_geographic_scope": "Global South", "eligibility_countries": "France; Spain"}
        self.assertEqual(geo_fit.applicant_component(ORG, call)["required"],
                         ["France", "Spain"])

    def test_nothing_raises_on_junk_input(self):
        for rfp in ({}, {"call_geographic_scope": None}, {"eligibility_countries": 7}):
            self.assertIn("label", geo_fit.evaluate(ORG, rfp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
