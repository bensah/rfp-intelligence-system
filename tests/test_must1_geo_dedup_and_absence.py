"""MUST-1 must stop duplicating MUST-4, and absence of org data must stop being a verdict.

Actions #5 and #6.

#5  MUST-1 item D "Registration region" fell back to the call's GEOGRAPHIC SCOPE when no
    registration rule was stated. Where the money is SPENT is not where the applicant
    must be INCORPORATED, so the item was MUST-4 wearing a legal-eligibility label.
    Executed across eight scopes against an org registered in Cameroon, item D and MUST-4
    `geo_presence` agreed 8/8 — zero independent information, geography double-counted
    into Bid Strength, and because `fatal_decline` checks MUST-1 FIRST the reviewer was
    told the blocker was "Registration region" when the real finding was geographic reach.
    Exactly what the Grand Challenges row displayed.

#6  Absence of ORG data was being scored as a verdict:
      a. `org_entity_type` defaults to "" — an unset field, not a declared mismatch — and
         a donor stating an entity type turned it into a fatal 0.
      c. `_num(org_annual_budget) or 0.0` made an org with NO recorded budget PASS a fatal
         ceiling, and said so: "our $0 annual budget vs the call's $1,000,000 ceiling".
      d. A blank local-board answer is the Settings UI's explicit "Unknown — don't apply
         this gate" ("'Unknown' leaves the gate off"), yet it scored 0 on a HARD gate.

    NOT changed: `org_has_established_pi`. It is a real checkbox in Settings, so unchecked
    is a RECORDED "we have no established PI", not absence — treating it as unknown would
    turn a genuine "no" into a pass. The audit flagged it; the data model says otherwise.

Run:  python -m unittest tests.test_must1_geo_dedup_and_absence
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

ORG = {"org_registered_countries": ["Cameroon"],
       "org_operating_countries": ["Cameroon"]}
SCOPES = (["Cameroon"], ["India"], ["Global / worldwide"], ["Sub-Saharan Africa"],
          ["Low- and middle-income countries (LMICs)"], ["Nigeria", "Kenya"],
          ["United States"], ["Bangladesh", "Global / worldwide"])


def _by(items):
    return {i["key"]: i for i in items}


def _regitem(org, rfp, donor=None):
    return _by(CD.qualification_factors(org, rfp, donor or {}, {}))["local_registration"]


class RegistrationIsNoLongerGeographyTests(unittest.TestCase):
    def test_a_call_scope_alone_never_activates_the_registration_gate(self):
        for scope in SCOPES:
            it = _regitem(ORG, {"call_geographic_scope": scope})
            self.assertFalse(it["active"], f"{scope} activated a REGISTRATION gate")

    def test_the_blocker_is_now_named_geographic_reach(self):
        # Was: (True, 'Registration region') — the wrong finding, from the wrong criterion.
        fatal, trigger = CD.fatal_decline(ORG, {"call_geographic_scope": ["India"]}, {})
        self.assertTrue(fatal, "an out-of-scope call must still be declined")
        self.assertIn("Geographic", trigger)

    def test_geographic_reach_is_still_gated_for_every_scope(self):
        # The gate must not be lost, only relocated to the criterion that owns it.
        for scope in SCOPES:
            rfp = {"call_geographic_scope": scope}
            reach = CD.derive_geographic_fit(ORG, rfp, {}, {})
            fatal, _ = CD.fatal_decline(ORG, rfp, {})
            self.assertEqual(fatal, reach == "No presence there", scope)

    def test_an_explicit_registration_rule_still_gates(self):
        it = _regitem(ORG, {}, {"donor_registration_region": "United States"})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)
        self.assertTrue(CD.fatal_decline(ORG, {}, {"donor_registration_region": "United States"})[0])

    def test_a_matching_registration_rule_passes(self):
        it = _regitem(ORG, {}, {"donor_registration_region": "Sub-Saharan Africa"})
        self.assertEqual(it["score"], 1.0)

    def test_explicit_any_is_still_a_real_pass(self):
        it = _regitem(ORG, {}, {"donor_registration_region": "Any"})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 1.0)

    def test_a_us_federal_call_no_longer_demands_us_registration(self):
        # A grants.gov / USDoS call for INTERNATIONAL work does not require US
        # incorporation: foreign entities obtain SAM/UEI without being US-registered, and a
        # genuinely US-only call is caught by the HQ/geographic gates. So a bare US-federal
        # cue no longer ACTIVATES registration region — it is "Not sure" (excluded), not a
        # 0. (owner 2026-08-31; SAM/UEI stays a MUST-5 credential, still _is_us_federal-fed.)
        rfp = {"opportunity_link": "https://www.grants.gov/web/grants/view-opportunity.html"}
        it = _regitem(ORG, rfp)
        self.assertFalse(it["active"])
        self.assertIsNone(it["score"])

    def test_a_donor_geographic_scope_alone_does_not_activate_it_either(self):
        it = _regitem(ORG, {}, {"donor_geographic_scope": "India"})
        self.assertFalse(it["active"])


class AbsenceIsNotAVerdictTests(unittest.TestCase):
    def test_an_unrecorded_entity_type_is_not_a_fatal_mismatch(self):
        it = _by(CD.qualification_factors(
            {}, {}, {"donor_entity_type_required": "grassroot_local"}, {}))["entity_type"]
        self.assertFalse(it["active"])
        self.assertIsNone(it["score"])

    def test_a_recorded_mismatch_is_still_a_real_failure(self):
        org = {"org_entity_type": "multi_country"}
        it = _by(CD.qualification_factors(
            org, {}, {"donor_entity_type_required": "grassroot_local"}, {}))["entity_type"]
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)

    def test_a_recorded_match_still_passes(self):
        org = {"org_entity_type": "grassroot_local"}
        it = _by(CD.qualification_factors(
            org, {}, {"donor_entity_type_required": "grassroot_local"}, {}))["entity_type"]
        self.assertEqual(it["score"], 1.0)

    def test_an_unrecorded_budget_no_longer_passes_a_ceiling_as_zero(self):
        fin = _by(CD.capacity_factors({}, {}, {"donor_max_annual_budget": 1_000_000}))
        parts = fin["financial_capacity"]["_parts"]
        self.assertEqual([p["key"] for p in parts], [],
                         "an unknown budget must not be measured as $0")

    def test_a_recorded_budget_still_measures_the_ceiling(self):
        for budget, want in ((4_000_000, 0.0), (500_000, 1.0)):
            fin = _by(CD.capacity_factors({"org_annual_budget": budget}, {},
                                          {"donor_max_annual_budget": 1_000_000}))
            parts = fin["financial_capacity"]["_parts"]
            self.assertEqual([p["key"] for p in parts], ["budget_ceiling"], budget)
            self.assertEqual(parts[0]["score"], want, budget)

    def test_the_same_holds_for_the_prior_grant_ceiling(self):
        fin = _by(CD.capacity_factors({}, {}, {"donor_max_prior_grant": 100_000}))
        self.assertEqual([p["key"] for p in fin["financial_capacity"]["_parts"]], [])
        fin = _by(CD.capacity_factors({"org_largest_grant": 2_000_000}, {},
                                      {"donor_max_prior_grant": 100_000}))
        self.assertEqual(fin["financial_capacity"]["_parts"][0]["score"], 0.0)

    def test_unknown_local_board_leaves_the_gate_off_as_the_ui_promises(self):
        for blank in ("", "   ", None):
            it = _by(CD.compliance_factors({}, {}, {"donor_local_board_required": True},
                                           {"org_has_local_board": blank}))["local_board"]
            self.assertFalse(it["active"], repr(blank))

    def test_an_answered_local_board_still_gates(self):
        no = _by(CD.compliance_factors({}, {}, {"donor_local_board_required": True},
                                       {"org_has_local_board": "no"}))["local_board"]
        self.assertTrue(no["active"])
        self.assertEqual(no["score"], 0.0)
        yes = _by(CD.compliance_factors({}, {}, {"donor_local_board_required": True},
                                        {"org_has_local_board": "yes"}))["local_board"]
        self.assertEqual(yes["score"], 1.0)


class RecordedNoIsNotAbsenceTests(unittest.TestCase):
    """`org_has_established_pi` is a Settings checkbox — unchecked is a recorded "no"."""

    PI_CALL = {"brief_description": "Applications are invited from individual investigators."}

    def test_an_unchecked_pi_box_is_still_a_real_failure(self):
        it = _by(CD.qualification_factors({}, self.PI_CALL, {}, {}))["individual_pi"]
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)

    def test_a_checked_pi_box_passes(self):
        it = _by(CD.qualification_factors({"org_has_established_pi": True},
                                          self.PI_CALL, {}, {}))["individual_pi"]
        self.assertEqual(it["score"], 1.0)


class NoBlanketDeclineTests(unittest.TestCase):
    def test_a_call_imposing_nothing_declines_nobody(self):
        rfp = {"opportunity_title": "Call for proposals",
               "call_geographic_scope": ["Cameroon"]}
        fatal, trigger = CD.fatal_decline(ORG, rfp, {})
        self.assertFalse(fatal, f"declined on {trigger!r} with nothing imposed")

    def test_an_empty_org_profile_is_not_auto_declined_by_missing_data(self):
        rfp = {"call_geographic_scope": ["Global / worldwide"]}
        fatal, trigger = CD.fatal_decline({}, rfp, {"donor_entity_type_required": "grassroot_local",
                                                    "donor_local_board_required": True})
        self.assertFalse(fatal, f"declined on {trigger!r} purely for unrecorded org data")


if __name__ == "__main__":
    unittest.main(verbosity=2)
