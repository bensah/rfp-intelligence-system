"""Call-first precedence (#8) and the indirect-cost policy match (#7).

#8  THE PRECEDENCE RULE, in one place. For every requirement the model scores:
      1. the CALL, if it states it — it wins outright on a conflict;
      2. else DONOR INTEL, as the fallback standing guideline;
      3. else nothing — "Not sure", excluded from the denominator, never defaulted
         to a pass OR a fail.
    It was BACKWARDS for valued keys: the call's value was written only when the donor
    field was blank, so a stale donor record beat the call in front of you — a call
    saying "Sub-Saharan Africa" lost to a donor record saying "United States".
    (`_geo_scope` already did call-first for geography; this brings compliance into line.)

#7  INDIRECT-COST POLICY. The org's own overhead rate vs the maximum a call/funder
    reimburses, as a % of project cost. Read call-first, per #8. Active only when BOTH
    sides are known — an unpublished cap or an unrecorded org rate is "Not sure".
    The long-unused `indirect_cost_disallowed` boolean becomes the 0% case.

Run:  python -m unittest tests.test_call_first_precedence_and_indirect_cost
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


def _by(items):
    return {i["key"]: i for i in items}


class CallWinsTests(unittest.TestCase):
    DONOR = {"donor_registration_region": "United States",
             "donor_entity_type_required": "multi_country",
             "donor_audited_financials_required": True,
             "donor_hq_country_required": "United States"}

    def test_a_valued_key_stated_by_the_call_beats_the_donor_record(self):
        eff = CD._merge_rfp_compliance(self.DONOR, {
            "registration_region": "Sub-Saharan Africa",
            "entity_type_required": "grassroot_local",
            "hq_country_required": "Cameroon"})
        self.assertEqual(eff["donor_registration_region"], "Sub-Saharan Africa")
        self.assertEqual(eff["donor_entity_type_required"], "grassroot_local")
        self.assertEqual(eff["donor_hq_country_required"], "Cameroon")

    def test_a_silent_call_leaves_the_donor_record_standing(self):
        eff = CD._merge_rfp_compliance(self.DONOR, {})
        self.assertEqual(eff["donor_registration_region"], "United States")
        self.assertTrue(eff["donor_audited_financials_required"])

    def test_an_empty_string_from_the_call_is_silence_not_an_override(self):
        for blank in ("", "   ", None):
            eff = CD._merge_rfp_compliance(self.DONOR, {"registration_region": blank})
            self.assertEqual(eff["donor_registration_region"], "United States", repr(blank))

    def test_the_call_can_clear_a_requirement_the_donor_asserts(self):
        # The same rule in the negative direction — the call governs THIS opportunity.
        eff = CD._merge_rfp_compliance(self.DONOR, {"audited_financials_required": "no"})
        item = _by(CD.compliance_factors({}, {}, eff, {}))["audited_financials"]
        self.assertFalse(item["active"])

    def test_a_boolean_false_from_the_call_also_clears_it(self):
        eff = CD._merge_rfp_compliance(self.DONOR, {"audited_financials_required": False})
        item = _by(CD.compliance_factors({}, {}, eff, {}))["audited_financials"]
        self.assertFalse(item["active"])

    def test_the_call_can_impose_what_the_donor_never_mentioned(self):
        eff = CD._merge_rfp_compliance({}, {"tax_exempt_status_required": True})
        item = _by(CD.compliance_factors({}, {}, eff, {}))["tax_exempt"]
        self.assertTrue(item["active"])

    def test_neither_side_leaves_it_unset(self):
        self.assertIsNone(CD._merge_rfp_compliance({}, {}).get("donor_registration_region"))

    def test_geography_already_followed_the_rule_and_still_does(self):
        self.assertEqual(
            CD._geo_scope({"call_geographic_scope": ["Cameroon"]},
                          {"donor_geographic_scope": ["India"]}), ["Cameroon"])
        self.assertEqual(
            CD._geo_scope({}, {"donor_geographic_scope": ["India"]}), ["India"])

    def test_a_zero_is_a_real_value_not_a_negative(self):
        # "0" must not be read as "not imposed" — a 0% cap is a real, strict rule.
        eff = CD._merge_rfp_compliance({}, {"indirect_cost_max_pct": 0})
        self.assertEqual(eff["donor_indirect_cost_max_pct"], 0)


class PercentParsingTests(unittest.TestCase):
    def test_accepted_forms(self):
        for v, want in ((15, 15.0), ("15", 15.0), ("15%", 15.0), (" 12.5 % ", 12.5),
                        (0, 0.0), (100, 100.0)):
            self.assertEqual(CD._pct(v), want, repr(v))

    def test_rejected_forms(self):
        for v in (None, "", "   ", "abc", -1, 101, 500, True, False):
            self.assertIsNone(CD._pct(v), repr(v))

    def test_a_fraction_is_not_silently_multiplied(self):
        # The field is labelled "% of project cost" on both forms; 0.15 means 0.15%.
        self.assertEqual(CD._pct(0.15), 0.15)


class IndirectCostComponentTests(unittest.TestCase):
    ORG15 = {"org_indirect_cost_rate": 15}

    def _item(self, org, donor):
        return _by(CD.compliance_factors(org, {}, donor, {}))["indirect_cost"]

    def test_within_the_cap_passes(self):
        it = self._item(self.ORG15, {"donor_indirect_cost_max_pct": 15})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 1.0)

    def test_above_the_cap_fails_and_says_by_how_much(self):
        it = self._item({"org_indirect_cost_rate": 25},
                        {"donor_indirect_cost_max_pct": 15})
        self.assertEqual(it["score"], 0.0)
        self.assertIn("10-point", it["_detail"])

    def test_an_unpublished_cap_is_not_sure(self):
        it = self._item(self.ORG15, {})
        self.assertFalse(it["active"])
        self.assertIsNone(it["score"])
        self.assertEqual(CD.component_mark(it)[0], "?")

    def test_an_unrecorded_org_rate_is_not_sure(self):
        it = self._item({}, {"donor_indirect_cost_max_pct": 15})
        self.assertFalse(it["active"])

    def test_disallowed_is_the_zero_percent_case(self):
        it = self._item(self.ORG15, {"donor_indirect_cost_disallowed": True})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)
        it0 = self._item({"org_indirect_cost_rate": 0},
                         {"donor_indirect_cost_disallowed": True})
        self.assertEqual(it0["score"], 1.0)

    def test_an_explicit_cap_beats_the_legacy_boolean(self):
        it = self._item(self.ORG15, {"donor_indirect_cost_disallowed": True,
                                     "donor_indirect_cost_max_pct": 20})
        self.assertEqual(it["score"], 1.0)

    def test_the_call_cap_beats_the_donor_cap(self):
        eff = CD._merge_rfp_compliance({"donor_indirect_cost_max_pct": 10},
                                       {"indirect_cost_max_pct": 20})
        self.assertEqual(self._item(self.ORG15, eff)["score"], 1.0)   # 15 <= 20

    def test_the_donor_cap_is_used_when_the_call_is_silent(self):
        eff = CD._merge_rfp_compliance({"donor_indirect_cost_max_pct": 10}, {})
        self.assertEqual(self._item(self.ORG15, eff)["score"], 0.0)   # 15 > 10

    def test_it_is_not_a_fatal_gate(self):
        it = self._item({"org_indirect_cost_rate": 25},
                        {"donor_indirect_cost_max_pct": 15})
        self.assertFalse(it["fatal"])
        self.assertFalse(CD.fatal_decline({"org_indirect_cost_rate": 25}, {},
                                          {"donor_indirect_cost_max_pct": 15})[0])

    # NOTE: these assert only what #7 owns — whether `indirect_cost` joins the ACTIVE
    # set. They deliberately do not pin the rest of the MUST-5 component list, which
    # differs between main and the pending all-clear/sam_uei rework (#136).
    def test_it_adds_nothing_to_the_denominator_when_no_cap_is_stated(self):
        act = {i["key"] for i in CD.compliance_factors(self.ORG15, {}, {}, {})
               if i["active"] and i["score"] is not None}
        self.assertNotIn("indirect_cost", act)

    def test_a_stated_cap_joins_the_denominator(self):
        act = {i["key"] for i in CD.compliance_factors(
            self.ORG15, {}, {"donor_indirect_cost_max_pct": 15}, {})
            if i["active"] and i["score"] is not None}
        self.assertIn("indirect_cost", act)


class ProfileWiringTests(unittest.TestCase):
    def test_the_org_field_exists_with_no_default_rate(self):
        from core.org_profile import DEFAULT_PROFILE
        self.assertIn("org_indirect_cost_rate", DEFAULT_PROFILE)
        self.assertIsNone(DEFAULT_PROFILE["org_indirect_cost_rate"])

    def test_the_cap_is_a_valued_key_so_it_survives_the_merge(self):
        # Not in _RFP_VALUED_KEYS it would be flattened to True and lose the number.
        self.assertIn("donor_indirect_cost_max_pct", CD._RFP_VALUED_KEYS)

    def test_the_bare_llm_key_maps_to_the_column(self):
        self.assertEqual(CD._eff_column("indirect_cost_max_pct"),
                         "donor_indirect_cost_max_pct")


if __name__ == "__main__":
    unittest.main(verbosity=2)
