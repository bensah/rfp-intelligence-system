"""MUST-1's call side, compliance-flag polarity, and the repeat-applicant restriction.

Three defects on one pipeline, all found by executing the shipped code.

#1  THE CALL SIDE WAS DEAD CODE. `core.llm_synthesis`'s prompt instructs the model to
    emit BARE keys (`entity_type_required`, `registration_region`, …); `_sanitize_must1`
    read the `donor_`-prefixed column names. Nothing errored — the values were dropped.
    Six of eight MUST-1 signals never survived, so NO MUST-1 component could be activated
    by what a call actually said; the criterion ran purely on hand-curated donor intel.
    That is why a call spelling out "open to nonprofit organizations, for-profit
    companies, international organizations, government agencies and academic
    institutions" still showed "? Eligible legal type — not stated by this call".

#2  A CALL SAYING A REQUIREMENT DOES NOT APPLY ACTIVATED IT. `_merge_rfp_compliance`
    coerced every non-empty value to True, and "no" / "not required" / "N/A" are all
    truthy in Python — so they activated a HARD gate and scored it 0.

#3  THE REPEAT-APPLICANT RESTRICTION WAS INVERTED. It scored 1 when the org WAS a prior
    beneficiary, for all four rule values — so `eligible` ("prior grantees explicitly
    welcome") auto-DECLINED every org that had never been funded, and the three
    `ineligible_*` rules passed exactly the orgs they exist to bar.

Run:  python -m unittest tests.test_must1_call_side_and_flags
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
from core.llm_synthesis import _sanitize_must1                  # noqa: E402

BMGF = {"donor": "Bill & Melinda Gates Foundation",
        "canonical_key": "bill_melinda_gates_foundation",
        "donor_aliases": "Gates Foundation; BMGF; Grand Challenges"}
RFP = {"funding_agency": "Grand Challenges"}


def _by_key(items):
    return {i["key"]: i for i in items}


class CallSideSurvivesSanitisationTests(unittest.TestCase):
    """Exactly the vocabulary the prompt asks the model to produce."""

    ASKED = {
        "entity_type_required": "grassroot_local",
        "hq_country_required": "Cameroon",
        "registration_region": "Sub-Saharan Africa",
        "prior_beneficiary_rule": "ineligible_current",
        "requires_pi": "yes",
        "pi_country_scope": "donor_in_scope",
        "org_stage_required": "established",
        "experience_required": "significant",
    }

    def test_every_bare_key_the_prompt_asks_for_now_survives(self):
        got = _sanitize_must1(self.ASKED)
        for col in ("donor_entity_type_required", "donor_hq_country_required",
                    "donor_registration_region", "donor_prior_beneficiary_rule",
                    "donor_requires_pi", "donor_pi_country_scope",
                    "org_stage_required", "experience_required"):
            self.assertIn(col, got, col)

    def test_the_prefixed_spelling_still_works(self):
        prefixed = {"donor_entity_type_required": "grassroot_local",
                    "donor_registration_region": "Sub-Saharan Africa",
                    "donor_requires_pi": "yes"}
        got = _sanitize_must1(prefixed)
        self.assertEqual(got["donor_entity_type_required"], "grassroot_local")
        self.assertEqual(got["donor_registration_region"], "Sub-Saharan Africa")
        self.assertTrue(got["donor_requires_pi"])

    def test_ungrounded_values_are_still_dropped(self):
        self.assertNotIn("donor_entity_type_required",
                         _sanitize_must1({"entity_type_required": "whatever"}))
        self.assertNotIn("donor_prior_beneficiary_rule",
                         _sanitize_must1({"prior_beneficiary_rule": "maybe"}))

    def test_a_call_stated_requirement_now_reaches_the_criterion(self):
        # End to end: the model's bare key → sanitiser → _merge_rfp_compliance → MUST-1.
        # The org must have RECORDED its entity type for the component to be scored —
        # absence of org data is "Not sure", not a verdict (action #6). This test is
        # about the CALL side reaching the criterion, so it supplies the org side.
        flags = _sanitize_must1({"entity_type_required": "grassroot_local"})
        eff = CD._merge_rfp_compliance({}, flags)
        self.assertEqual(eff.get("donor_entity_type_required"), "grassroot_local")
        org = {"org_entity_type": "grassroot_local"}
        item = _by_key(CD.qualification_factors(org, {}, eff, {}))["entity_type"]
        self.assertTrue(item["active"], "the call stated it — it must be scored")
        self.assertEqual(item["score"], 1.0)

    def test_the_call_side_reaches_a_criterion_that_needs_no_org_field(self):
        # Same end-to-end path, on an item whose activation depends only on the call —
        # so it holds regardless of what the org profile has recorded.
        flags = _sanitize_must1({"registration_region": "Sub-Saharan Africa"})
        eff = CD._merge_rfp_compliance({}, flags)
        item = _by_key(CD.qualification_factors({}, {}, eff, {}))["local_registration"]
        self.assertTrue(item["active"], "the call stated it — it must be scored")


class NotImposedFlagTests(unittest.TestCase):
    def test_an_explicit_negative_does_not_impose_the_requirement(self):
        for v in ("no", "No", "false", "not required", "N/A", "none",
                  "unknown", "not stated", "not applicable"):
            eff = CD._merge_rfp_compliance({}, {"audited_financials_required": v})
            item = _by_key(CD.compliance_factors({}, {}, eff, {}))["audited_financials"]
            self.assertFalse(item["active"], f"{v!r} activated a hard gate")

    def test_a_real_requirement_still_activates(self):
        for v in (True, "yes", "required", "Required", "mandatory"):
            eff = CD._merge_rfp_compliance({}, {"audited_financials_required": v})
            item = _by_key(CD.compliance_factors({}, {}, eff, {}))["audited_financials"]
            self.assertTrue(item["active"], f"{v!r} failed to activate")

    def test_a_negative_does_not_overwrite_a_valued_donor_field(self):
        eff = CD._merge_rfp_compliance({"donor_registration_region": "Sub-Saharan Africa"},
                                       {"registration_region": "not stated"})
        self.assertEqual(eff["donor_registration_region"], "Sub-Saharan Africa")

    def test_booleans_are_left_to_normal_truthiness(self):
        self.assertFalse(CD._explicitly_not_imposed(True))
        self.assertFalse(CD._explicitly_not_imposed(False))   # falsy — skipped upstream


class RepeatApplicantRestrictionTests(unittest.TestCase):
    CURRENT = {"org_active_donors": ["Bill & Melinda Gates Foundation"]}
    PAST = {"org_funder_history": ["Bill & Melinda Gates Foundation"]}
    NEITHER = {"org_funder_history": ["Wellcome Trust"]}

    def _item(self, org, rule):
        eff = {"donor_prior_beneficiary_rule": rule, **BMGF} if rule else dict(BMGF)
        return _by_key(CD.qualification_factors(org, RFP, eff, {}))["prior_beneficiary"]

    def test_it_is_renamed_so_it_stops_reading_as_a_relationship_duplicate(self):
        self.assertEqual(self._item(self.NEITHER, "ineligible_any")["name"],
                         "Repeat-applicant restriction")

    def test_eligible_means_no_restriction_and_is_not_scored(self):
        # THE WORST CASE: this value's help text is "prior grantees explicitly welcome",
        # and it used to auto-Decline every org that had never been funded.
        it = self._item(self.NEITHER, "eligible")
        self.assertFalse(it["active"])
        self.assertIsNone(it["score"])
        self.assertFalse(CD.fatal_decline(self.NEITHER, RFP, {**BMGF,
                         "donor_prior_beneficiary_rule": "eligible"})[0])

    def test_nothing_stated_is_not_scored(self):
        it = self._item(self.NEITHER, "")
        self.assertFalse(it["active"])

    def test_current_grantees_are_barred_only_by_the_current_rule(self):
        self.assertEqual(self._item(self.CURRENT, "ineligible_current")["score"], 0.0)
        self.assertEqual(self._item(self.NEITHER, "ineligible_current")["score"], 1.0)

    def test_past_grantees_are_barred_only_by_the_previous_rule(self):
        self.assertEqual(self._item(self.PAST, "ineligible_previous")["score"], 0.0)
        self.assertEqual(self._item(self.NEITHER, "ineligible_previous")["score"], 1.0)

    def test_ineligible_any_bars_both(self):
        self.assertEqual(self._item(self.CURRENT, "ineligible_any")["score"], 0.0)
        self.assertEqual(self._item(self.PAST, "ineligible_any")["score"], 0.0)
        self.assertEqual(self._item(self.NEITHER, "ineligible_any")["score"], 1.0)

    def test_the_four_values_are_no_longer_scored_identically(self):
        scores = {r: self._item(self.NEITHER, r)["score"]
                  for r in ("eligible", "ineligible_current",
                            "ineligible_previous", "ineligible_any")}
        self.assertEqual(scores["eligible"], None)
        self.assertEqual(set(v for k, v in scores.items() if k != "eligible"), {1.0})

    def test_a_barred_org_still_auto_declines(self):
        # The gate must survive the correction — this is a real eligibility rule.
        fatal, trigger = CD.fatal_decline(
            self.CURRENT, RFP, {**BMGF, "donor_prior_beneficiary_rule": "ineligible_current"})
        self.assertTrue(fatal)
        self.assertEqual(trigger, "Repeat-applicant restriction")

    def test_it_matches_the_donor_canonically_like_everything_else(self):
        # "Grand Challenges" must resolve to the Gates Foundation in the org's lists.
        self.assertEqual(self._item(self.CURRENT, "ineligible_current")["score"], 0.0)


class NoBlanketDeclineTests(unittest.TestCase):
    """A call that states no eligibility requirement at all must not auto-Decline."""

    def test_a_silent_call_declines_nobody_on_must1(self):
        org = {"org_registered_countries": ["Cameroon"],
               "org_operating_countries": ["Cameroon"]}
        rfp = {"opportunity_title": "Call for proposals",
               "call_geographic_scope": ["Cameroon"]}
        fatal, trigger = CD.fatal_decline(org, rfp, {})
        self.assertFalse(fatal, f"declined on {trigger!r} with nothing imposed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
