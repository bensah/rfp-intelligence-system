"""Tests for the MUST-3 rework and the component verdict symbol (owner 2026-08-06).

THE SYMBOL — "?" meant two different things on the Review card: "the call stated
nothing, so this is excluded" AND "we measured it and it landed halfway". A reviewer
reading "? Strategic priority fitness — 0.5 (partial priority match) · Matched 1 of 1
funder theme(s)" sees a question mark on a component backed by real, matched data. The
symbol now follows the SCORE, so a measured partial gets ◐ and only the genuinely
undetermined keeps "?".

MUST-3 — was four components, three of which (annual-budget ceiling, prior-grant
ceiling, award absorption) all need the call's award VALUE. When extraction misses that
value, all three blank at once and the card reads as three separate unknowns rather than
one. They are now ONE 0-1 composite scored over whichever value checks ARE determinable.
The fourth, Experience, was active only when the call stated a bar — silence dragged the
criterion to "Not sure"/Park even though a call that says nothing about maturity is open
to a start-up and an established org alike. Silence is now a PASS, and the STAGE
restriction a call can impose (extracted as org_stage_required but never scored) is
finally measured: a window reserved for young organisations must score an established
applicant 0.

Run:  python -m unittest tests.test_must3_capacity_and_marks
"""
import os
import sys
import unittest
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD          # noqa: E402

# An org with the capacity facts MUST-3 reads: founded long ago, sizeable budget and a
# large grant already managed.
ORG = {
    "org_founding_year": 2002,
    "org_stage": "established",
    "org_annual_budget": 4_000_000,
    "org_largest_grant": 2_000_000,
    "org_grants_count": 25,
}
# A call with an award value the extractor DID capture.
RFP = {"call_award_value": 1_000_000, "currency": "USD"}
# The reported case: EDCTP3-style — no award value, no duration, no ceilings.
RFP_NO_VALUE = {"opportunity_title": "Global collaboration action on climate and health"}


def _by_key(items):
    return {i["key"]: i for i in items}


class ComponentMarkTests(unittest.TestCase):
    """The symbol must distinguish 'measured, partial' from 'nothing to measure'."""

    def test_a_measured_partial_is_not_a_question_mark(self):
        # THE REPORTED CASE: strategic fit 0.5 — real data, a partial match.
        sym, _ = CD.component_mark({"score": 0.5, "met": None, "active": True})
        self.assertEqual(sym, "◐")
        self.assertNotEqual(sym, "?")

    def test_full_and_zero_keep_their_symbols(self):
        self.assertEqual(CD.component_mark({"score": 1.0, "met": True})[0], "✓")
        self.assertEqual(CD.component_mark({"score": 0.0, "met": False})[0], "✗")

    def test_unmeasurable_still_reads_as_unknown(self):
        # No score at all (inactive / nothing stated) → "?" is correct there.
        self.assertEqual(CD.component_mark({"score": None, "met": None})[0], "?")

    def test_every_partial_band_gets_the_middle_symbol(self):
        # The composite averages, so scores are no longer only 0 / 0.5 / 1.
        for sc in (0.25, 0.33, 0.5, 0.667, 0.75, 0.99):
            self.assertEqual(CD.component_mark({"score": sc})[0], "◐", sc)

    def test_falls_back_to_met_when_there_is_no_score(self):
        # PREFER factors carry `met` only.
        self.assertEqual(CD.component_mark({"met": True})[0], "✓")
        self.assertEqual(CD.component_mark({"met": False})[0], "✗")

    def test_the_four_symbols_are_distinct(self):
        marks = {CD.MARK_MET[0], CD.MARK_PARTIAL[0], CD.MARK_FAILED[0], CD.MARK_UNKNOWN[0]}
        self.assertEqual(len(marks), 4)


class CapacityShapeTests(unittest.TestCase):
    def test_must3_now_presents_two_components(self):
        keys = list(_by_key(CD.capacity_factors(ORG, RFP, {})))
        self.assertEqual(keys, ["financial_capacity", "experience"])

    def test_the_value_checks_are_folded_into_the_composite(self):
        # Not separate components any more — they are sub-parts of financial_capacity.
        fin = _by_key(CD.capacity_factors(ORG, RFP, {}))["financial_capacity"]
        self.assertIn("award_absorption", {p["key"] for p in fin["_parts"]})


class FinancialCapacityTests(unittest.TestCase):
    def test_no_money_facts_at_all_leaves_it_undetermined(self):
        # THE REPORTED CASE: value extraction failed and no ceiling is stated → the
        # component is inactive (excluded), not three separate "Not sure" rows.
        fin = _by_key(CD.capacity_factors(ORG, RFP_NO_VALUE, {}))["financial_capacity"]
        self.assertFalse(fin["active"])
        self.assertIsNone(fin["score"])
        self.assertEqual(CD.component_mark(fin)[0], "?")

    def test_a_single_determinable_check_still_composites_to_0_1(self):
        # Only absorption is knowable (no ceilings stated) — the composite is that
        # check's score, on the same 0-1 scale.
        fin = _by_key(CD.capacity_factors(ORG, RFP, {}))["financial_capacity"]
        self.assertTrue(fin["active"])
        self.assertEqual(len(fin["_parts"]), 1)
        self.assertGreaterEqual(fin["score"], 0.0)
        self.assertLessEqual(fin["score"], 1.0)

    def test_a_ceiling_alone_is_enough_to_activate_it(self):
        fin = _by_key(CD.capacity_factors(
            ORG, RFP_NO_VALUE, {"donor_max_annual_budget": 10_000_000}))["financial_capacity"]
        self.assertTrue(fin["active"])
        self.assertEqual(fin["score"], 1.0)          # $4M budget is under the $10M ceiling

    def test_the_composite_is_the_mean_of_its_determinable_parts(self):
        # A failed ceiling (0) alongside a comfortable absorption (1) → 0.5.
        fin = _by_key(CD.capacity_factors(
            ORG, RFP, {"donor_max_annual_budget": 1_000_000}))["financial_capacity"]
        scores = sorted(p["score"] for p in fin["_parts"])
        self.assertEqual(scores, [0.0, 1.0])
        self.assertAlmostEqual(fin["score"], 0.5)

    def test_a_big_annual_budget_carries_a_small_award(self):
        # The owner's point: an org managing a large budget yearly can carry this award.
        self.assertEqual(CD._award_absorption_score(ORG, {"call_award_value": 500_000,
                                                          "currency": "USD"}), 1.0)

    def test_an_award_far_beyond_the_track_record_is_not_absorbable(self):
        small = {"org_annual_budget": 200_000, "org_founding_year": 2021}
        self.assertEqual(CD._award_absorption_score(
            small, {"call_award_value": 50_000_000, "currency": "USD"}), 0.0)

    def test_the_padlock_only_shows_when_a_real_ceiling_is_in_play(self):
        # A soft absorption stretch is not an auto-Decline and must not wear 🔒.
        soft = _by_key(CD.capacity_factors(ORG, RFP, {}))["financial_capacity"]
        self.assertFalse(soft["fatal"])
        hard = _by_key(CD.capacity_factors(
            ORG, RFP, {"donor_max_prior_grant": 5_000_000}))["financial_capacity"]
        self.assertTrue(hard["fatal"])


class HardCeilingStaysAGateTests(unittest.TestCase):
    """Folding the ceilings into a soft composite must NOT quietly retire the gate:
    a fund reserved for organisations below a stated size is a structural ineligibility,
    and the composite's mean can average a failed ceiling away to a passing-looking 0.5."""

    DONOR_SMALL_ORGS_ONLY = {"donor_max_annual_budget": 1_000_000}

    def test_exceeding_a_stated_ceiling_still_auto_declines(self):
        fatal, trigger = CD.fatal_decline(ORG, RFP, self.DONOR_SMALL_ORGS_ONLY)
        self.assertTrue(fatal)
        self.assertEqual(trigger, "Annual-budget ceiling")

    def test_the_label_is_beyond_us_even_when_the_mean_reads_0_5(self):
        fin = _by_key(CD.capacity_factors(
            ORG, RFP, self.DONOR_SMALL_ORGS_ONLY))["financial_capacity"]
        self.assertAlmostEqual(fin["score"], 0.5)                  # would read "stretch"
        self.assertEqual(CD.derive_capacity(ORG, RFP, self.DONOR_SMALL_ORGS_ONLY),
                         "No, beyond us")                          # but it is structural

    def test_a_prior_grant_ceiling_gates_too(self):
        fatal, trigger = CD.fatal_decline(ORG, RFP, {"donor_max_prior_grant": 100_000})
        self.assertTrue(fatal)
        self.assertEqual(trigger, "Prior-grant ceiling")

    def test_sitting_under_the_ceiling_does_not_decline(self):
        fatal, _ = CD.fatal_decline(ORG, RFP, {"donor_max_annual_budget": 50_000_000})
        self.assertFalse(fatal)

    def test_a_soft_absorption_failure_is_never_fatal(self):
        small = {"org_annual_budget": 100_000, "org_founding_year": 2022}
        fatal, _ = CD.fatal_decline(small, {"call_award_value": 90_000_000,
                                            "currency": "USD"}, {})
        self.assertFalse(fatal)


class ExperienceDefaultPassTests(unittest.TestCase):
    def test_silence_is_a_pass_not_a_not_sure(self):
        # THE REPORTED CASE: nothing stated by the call OR donor intel → assume a
        # start-up and an established org can both apply.
        exp = _by_key(CD.capacity_factors(ORG, RFP_NO_VALUE, {}))["experience"]
        self.assertTrue(exp["active"])
        self.assertEqual(exp["score"], 1.0)
        self.assertTrue(exp["default"])              # renders "(no restriction)"
        self.assertEqual(CD.component_mark(exp)[0], "✓")

    def test_a_default_pass_is_never_a_fatal_gate(self):
        exp = _by_key(CD.capacity_factors(ORG, RFP_NO_VALUE, {}))["experience"]
        self.assertFalse(exp["fatal"])

    def test_a_call_with_no_money_and_no_bar_stops_reading_not_sure(self):
        self.assertEqual(CD.derive_capacity(ORG, RFP_NO_VALUE, {}), "Yes, comfortably")


class ExperienceYearsBarTests(unittest.TestCase):
    def _score(self, donor, org=None):
        org = ORG if org is None else org       # NOT `org or ORG` — {} is a real case
        return _by_key(CD.capacity_factors(org, RFP_NO_VALUE, donor))["experience"]["score"]

    def test_meeting_the_bar_scores_full(self):
        self.assertEqual(self._score({"experience_required": "significant"}), 1.0)

    def test_a_near_miss_scores_partial(self):
        young = {**ORG, "org_founding_year": date.today().year - 9}      # 9y vs a 10y bar
        self.assertEqual(self._score({"experience_required": "significant"}, young), 0.5)

    def test_falling_well_short_scores_zero(self):
        young = {**ORG, "org_founding_year": date.today().year - 2}
        self.assertEqual(self._score({"experience_required": "significant"}, young), 0.0)

    def test_an_explicit_year_count_is_taken_literally(self):
        # "no less than 3 years since creation" — scored as written, not rounded into
        # the coarse significant/moderate band.
        self.assertEqual(CD._experience_required_years({"experience_required": "3"}), 3)
        self.assertEqual(CD._experience_required_years({"experience_required": "5+"}), 5)
        self.assertEqual(CD._experience_required_years({"experience_required": "2 years"}), 2)
        three_yr_old = {**ORG, "org_founding_year": date.today().year - 3}
        self.assertEqual(self._score({"experience_required": "3"}, three_yr_old), 1.0)

    def test_the_graded_vocabulary_still_maps(self):
        self.assertEqual(CD._experience_required_years({"experience_required": "moderate"}), 5)
        self.assertIsNone(CD._experience_required_years({"experience_required": ""}))
        self.assertIsNone(CD._experience_required_years({"experience_required": "gibberish"}))

    def test_an_unrecorded_founding_year_leans_on_the_stage(self):
        no_year = {"org_stage": "established"}
        self.assertEqual(self._score({"experience_required": "moderate"}, no_year), 1.0)
        self.assertEqual(self._score({"experience_required": "moderate"},
                                     {"org_stage": "early-stage"}), 0.0)
        self.assertEqual(self._score({"experience_required": "moderate"}, {}), 0.5)


class ExperienceStageBarTests(unittest.TestCase):
    """org_stage_required was extracted but never scored — the gap the owner spotted."""

    def _exp(self, donor, org=None):
        org = ORG if org is None else org       # NOT `org or ORG` — {} is a real case
        return _by_key(CD.capacity_factors(org, RFP_NO_VALUE, donor))["experience"]

    def test_a_startups_only_call_scores_an_established_org_zero(self):
        exp = self._exp({"org_stage_required": "early-stage"})
        self.assertEqual(exp["score"], 0.0)
        self.assertEqual(CD.component_mark(exp)[0], "✗")
        self.assertIn("early-stage orgs only", exp["_detail"])

    def test_an_established_only_call_passes_an_established_org(self):
        self.assertEqual(self._exp({"org_stage_required": "established"})["score"], 1.0)

    def test_an_established_only_call_fails_a_startup(self):
        self.assertEqual(self._exp({"org_stage_required": "established"},
                                   {"org_stage": "startup"})["score"], 0.0)

    def test_an_unrecorded_stage_is_a_measured_halfway_not_an_unknown(self):
        exp = self._exp({"org_stage_required": "early-stage"}, {})
        self.assertEqual(exp["score"], 0.5)
        self.assertEqual(CD.component_mark(exp)[0], "◐")

    def test_the_weaker_of_the_two_bars_governs(self):
        # Established org: passes the 10y bar, fails the early-stage-only restriction.
        exp = self._exp({"experience_required": "significant",
                         "org_stage_required": "early-stage"})
        self.assertEqual(exp["score"], 0.0)

    def test_the_stage_bar_is_not_a_fatal_gate(self):
        fatal, _ = CD.fatal_decline(ORG, RFP_NO_VALUE, {"org_stage_required": "early-stage"})
        self.assertFalse(fatal)


class CapacityLabelTests(unittest.TestCase):
    def test_all_components_full_reads_comfortably(self):
        self.assertEqual(CD.derive_capacity(ORG, RFP, {}), "Yes, comfortably")

    def test_a_partial_component_reads_stretch(self):
        # Absorption is a stretch → the composite is partial → "Yes, but a stretch".
        org = {"org_annual_budget": 1_000_000, "org_founding_year": 2020,
               "org_stage": "established"}
        rfp = {"call_award_value": 6_000_000, "currency": "USD"}
        self.assertEqual(CD._award_absorption_score(org, rfp), 0.5)
        self.assertEqual(CD.derive_capacity(org, rfp, {}), "Yes, but a stretch")

    def test_a_failed_component_reads_beyond_us(self):
        self.assertEqual(CD.derive_capacity(ORG, RFP_NO_VALUE,
                                            {"org_stage_required": "early-stage"}),
                         "No, beyond us")

    def test_bid_strength_counts_the_two_components(self):
        num, den = CD.capacity_bid_strength(ORG, RFP, {})
        self.assertEqual(den, 2)
        self.assertEqual(num, 2.0)

    def test_bid_strength_drops_the_undetermined_composite(self):
        num, den = CD.capacity_bid_strength(ORG, RFP_NO_VALUE, {})
        self.assertEqual((num, den), (1.0, 1))       # experience only


class BreakdownWiringTests(unittest.TestCase):
    def test_the_review_card_gets_the_new_components(self):
        bd = CD.factor_breakdown(RFP, ORG, {}, {})
        self.assertEqual([f["key"] for f in bd["capacity"]],
                         ["financial_capacity", "experience"])

    def test_a_human_override_still_wins_on_the_composite(self):
        bd = CD.factor_breakdown(RFP_NO_VALUE, ORG, {}, {},
                                 overrides={"capacity": {"financial_capacity": 1.0}})
        fin = _by_key(bd["capacity"])["financial_capacity"]
        self.assertTrue(fin["active"])               # an override activates it
        self.assertEqual(fin["score"], 1.0)
        self.assertTrue(fin["_override"])


class FeatureContractTests(unittest.TestCase):
    """The stored model reads features POSITIONALLY — retired component keys must keep
    their slots and the new one must be appended, never inserted."""

    def test_retired_keys_keep_their_positions(self):
        from core.features import COMPONENT_FEATURE_NAMES as N
        for k in ("cmp_org_stage", "cmp_budget_ceiling", "cmp_grant_ceiling",
                  "cmp_award_absorption"):
            self.assertIn(k, N, k)
        self.assertLess(N.index("cmp_budget_ceiling"), N.index("cmp_geo_presence"))

    def test_the_composite_was_appended_not_inserted(self):
        # NOT "is last" — later reworks append their own keys behind it. The durable
        # invariant is that it sits after every ORIGINAL key, so no existing feature
        # position shifted.
        from core.features import COMPONENT_FEATURE_NAMES as N
        self.assertGreater(N.index("cmp_financial_capacity"), N.index("cmp_bid_team"))


class LLMExtractionTests(unittest.TestCase):
    def test_an_explicit_year_survives_sanitisation(self):
        from core.llm_synthesis import _sanitize_must1
        self.assertEqual(_sanitize_must1({"experience_required": "3"})["experience_required"],
                         "3")
        self.assertEqual(_sanitize_must1({"experience_required": "5 years"})
                         ["experience_required"], "5")

    def test_the_graded_vocabulary_still_survives(self):
        from core.llm_synthesis import _sanitize_must1
        self.assertEqual(_sanitize_must1({"experience_required": "significant"})
                         ["experience_required"], "significant")

    def test_ungrounded_free_text_is_still_dropped(self):
        from core.llm_synthesis import _sanitize_must1
        self.assertNotIn("experience_required",
                         _sanitize_must1({"experience_required": "lots of it"}))

    def test_the_stage_restriction_is_still_extracted(self):
        from core.llm_synthesis import _sanitize_must1
        self.assertEqual(_sanitize_must1({"org_stage_required": "early-stage"})
                         ["org_stage_required"], "early-stage")


if __name__ == "__main__":
    unittest.main(verbosity=2)
