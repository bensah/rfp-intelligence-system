"""A criterion's label and its component panel must never contradict each other.

These lock down the two ways they used to:

  1. the label was read from the DATABASE on a reviewed row while the components were
     recomputed live, so a submit-time answer froze beside moving components — PREFER-9
     read "Tight but doable, with a team" next to its own components at 2/2 100%;
  2. a human component override moved the panel but not the label.

Plus the two things the count slot must never do: name a decision ("Park" is the verdict
for the whole opportunity, not for one criterion), and render an unmeasured component as
0.0 (which reads as a measured failure).
"""
from __future__ import annotations

import unittest

from core import criteria_review as cr
from core.scorer import CRITERIA, CRITERION_RESPONSES


def f(key, *, active=True, score=None, met=None, override=False, hard=False):
    d = {"key": key, "name": key, "active": active, "score": score, "met": met,
         "hard": hard}
    if override:
        d["_override"] = True
    return d


class TestNoDecisionNameAtCriterionLevel(unittest.TestCase):
    def test_nothing_to_count_never_says_park(self):
        # MUST-1 on a call that imposes no qualification requirement: 6 inactive items.
        facts = [f(f"q{i}", active=False) for i in range(6)]
        txt = cr.count_text("qualification", facts, "Not sure", True)
        self.assertEqual(txt, cr.NOT_SCORED)
        for word in ("Park", "Proceed", "Decline"):
            self.assertNotIn(word, txt)

    def test_no_criterion_count_text_ever_names_a_decision(self):
        shapes = [
            [],
            [f("a", active=False)],
            [f("a", score=1.0, met=True)],
            [f("a", score=0.0, met=False), f("b", score=0.5)],
            [f("a", active=True, score=None, met=None)],
        ]
        for key in CRITERIA:
            for facts in shapes:
                for unsure in (True, False):
                    txt = cr.count_text(key, facts, "x", unsure)
                    for word in ("Park", "Proceed", "Decline"):
                        self.assertNotIn(word, txt, f"{key} {facts} -> {txt!r}")

    def test_a_measured_criterion_shows_its_ratio(self):
        facts = [f("a", score=1.0, met=True), f("b", score=1.0, met=True)]
        self.assertEqual(cr.count_text("capacity", facts, "Yes, comfortably", False),
                         "2/2 · 100%")


class TestLabelNeverFreezesBesideItsComponents(unittest.TestCase):
    """#3 — the general case, not just PREFER-9."""

    def test_prefer9_at_two_of_two_reads_ample_not_tight(self):
        # The exact live shape: bid_time flipped to 1.0 once the bid was submitted, while
        # the stored label still said "Tight but doable, with a team".
        facts = [f("bid_time", score=1.0, met=True), f("bid_team", score=None, met=True)]
        self.assertEqual(cr.roll_up("bid_effort", facts),
                         "Ample time, sufficient resources")
        # And the label used for display agrees with the count beside it.
        lbl = cr.criterion_label("bid_effort", facts, "Ample time, sufficient resources")
        self.assertEqual(lbl, "Ample time, sufficient resources")
        self.assertEqual(cr.count_text("bid_effort", facts, lbl, False), "2/2 · 100%")

    def test_the_stored_column_is_never_consulted(self):
        # criterion_label takes the DERIVED label, never the row. Passing a stale value as
        # `derived_label` is the only way it could appear, and that is the caller's live
        # derivation by construction.
        facts = [f("bid_time", score=1.0, met=True), f("bid_team", score=None, met=True)]
        self.assertNotEqual(
            cr.criterion_label("bid_effort", facts, "Ample time, sufficient resources"),
            "Tight but doable, with a team")

    def test_all_nine_criteria_produce_a_valid_response_label(self):
        facts_by_key = {
            "qualification": [f("a", score=1.0, met=True)],
            "strategic_fit": [f("strat_fitness", score=0.5)],
            "capacity": [f("a", score=0.5), f("b", score=1.0, met=True)],
            "geographic_fit": [f("geo_presence", score=1.0, met=True)],
            "cofinancing": [f("a", score=0.0, met=False)],
            "funding_quality": [f("a", score=1.0, met=True)],
            "funder_relationship": [f("rel_grantee", score=1.0, met=True)],
            "competitiveness": [f("comp_track", score=1.0, met=True)],
            "bid_effort": [f("bid_time", score=0.5), f("bid_team", score=1.0, met=True)],
        }
        for key in CRITERIA:
            lbl = cr.roll_up(key, facts_by_key[key])
            self.assertIn(lbl, CRITERION_RESPONSES[key], f"{key} -> {lbl!r}")


class TestHumanOverrideMovesTheLabel(unittest.TestCase):
    def test_an_override_makes_the_components_name_the_criterion(self):
        # Derivation says fully met; the reviewer has marked a component failed.
        facts = [f("tax_exempt", score=0.0, met=False, override=True),
                 f("audited_financials", score=1.0, met=True)]
        self.assertTrue(cr.has_human_override(facts))
        self.assertEqual(cr.criterion_label("cofinancing", facts, "Yes, fully met"),
                         "Not met")

    def test_without_an_override_the_derivation_still_names_it(self):
        facts = [f("cofinance", score=1.0, met=True)]
        self.assertEqual(cr.criterion_label("cofinancing", facts, "Partial, with effort"),
                         "Partial, with effort")

    def test_an_in_progress_edit_moves_the_label_before_saving(self):
        facts = [f("a", score=1.0, met=True), f("b", score=1.0, met=True)]
        self.assertEqual(
            cr.criterion_label("capacity", facts, "Yes, comfortably",
                               session_scores={"a": 0.0}),
            "No, beyond us")


class TestPrefer6And8AreNamedByTheirOwnModel(unittest.TestCase):
    """Owner 2026-08-10: the DERIVATION is authoritative for funding_quality and
    competitiveness. Their derivations are weighted models — PREFER-6 gates on whether the
    award can be sized at all, PREFER-8 counts track record 1.5x and SUBTRACTS for unmet
    donor requirements — and a flat component mean expresses neither, so the mean must
    never replace them."""

    def test_a_human_override_does_not_rename_competitiveness(self):
        facts = [f("comp_track", score=1.0, met=True, override=True),
                 f("comp_age", met=True)]
        self.assertTrue(cr.has_human_override(facts))
        # The roll-up WOULD say "Strong"; the derivation says Moderate and wins.
        self.assertEqual(cr.roll_up("competitiveness", facts),
                         "Strong (limited field / incumbent / clear edge)")
        self.assertEqual(cr.criterion_label("competitiveness", facts, "Moderate"),
                         "Moderate")

    def test_an_in_progress_edit_does_not_rename_funding_quality(self):
        facts = [f("fq_floor", met=True), f("fq_ceiling", met=True)]
        self.assertEqual(
            cr.criterion_label("funding_quality", facts, "Not sure",
                               session_scores={"fq_floor": 0.0}),
            "Not sure")

    def test_the_other_seven_criteria_still_follow_a_human_override(self):
        for key, facts, derived, want in (
            ("qualification", [f("a", score=0.0, met=False, override=True)],
             "Yes, fully", "No, not eligible"),
            ("cofinancing", [f("a", score=0.0, met=False, override=True)],
             "Yes, fully met", "Not met"),
            ("capacity", [f("a", score=0.5, override=True)],
             "Yes, comfortably", "Yes, but a stretch"),
            ("bid_effort", [f("bid_time", score=0.5, override=True),
                            f("bid_team", score=1.0, met=True)],
             "Ample time, sufficient resources", "Tight but doable, with a team"),
        ):
            with self.subTest(criterion=key):
                self.assertEqual(cr.criterion_label(key, facts, derived), want)

    def test_force_roll_up_is_still_available_as_a_last_resort(self):
        # A caller with no derived label at all can still get something to show.
        facts = [f("comp_track", score=1.0, met=True)]
        self.assertEqual(
            cr.criterion_label("competitiveness", facts, None, force_roll_up=True),
            "Strong (limited field / incumbent / clear edge)")

    def test_no_derived_label_falls_back_to_the_components(self):
        facts = [f("comp_track", score=1.0, met=True)]
        self.assertEqual(cr.criterion_label("competitiveness", facts, None),
                         "Strong (limited field / incumbent / clear edge)")


class TestTheLabelSourceNoteExplainsTheDifference(unittest.TestCase):
    """A label that disagrees with the count beside it looks exactly like the frozen-label
    defect. For these two it isn't — both numbers are live and measure different things —
    so the panel has to say so."""

    def test_competitiveness_explains_itself_when_the_ratio_disagrees(self):
        facts = [f("comp_track", score=1.0, met=True), f("comp_age", met=True)]
        note = cr.label_source_note("competitiveness", facts, "Moderate")
        self.assertIn("weighted competitiveness model", note)
        self.assertIn("Strong", note)          # what the ratio alone would have said

    def test_funding_quality_explains_itself(self):
        facts = [f("fq_value", met=True)]
        note = cr.label_source_note("funding_quality", facts, "Not sure")
        self.assertIn("funding-quality model", note)

    def test_no_note_when_the_two_agree(self):
        facts = [f("comp_track", score=1.0, met=True)]
        self.assertEqual(
            cr.label_source_note("competitiveness", facts,
                                 "Strong (limited field / incumbent / clear edge)"), "")

    def test_no_note_for_the_other_seven_criteria(self):
        facts = [f("a", score=0.0, met=False)]
        for key in ("qualification", "strategic_fit", "capacity", "geographic_fit",
                    "cofinancing", "funder_relationship", "bid_effort"):
            with self.subTest(criterion=key):
                self.assertEqual(cr.label_source_note(key, facts, "anything"), "")

    def test_no_note_when_nothing_is_active(self):
        facts = [f("comp_track", active=False)]
        self.assertEqual(cr.label_source_note("competitiveness", facts, "Moderate"), "")


class TestSettingAGreyedComponentActivatesIt(unittest.TestCase):
    """The chosen semantics: a reviewer asserting a requirement applies makes it count."""

    def test_an_edit_on_an_inactive_component_activates_and_counts(self):
        facts = [f("audited_financials", active=False)]
        self.assertEqual(cr.criterion_count("cofinancing", facts), ("0", 0, 0))
        eff = cr.with_session_edits(facts, {"audited_financials": 0.5})
        self.assertTrue(eff[0]["active"])
        self.assertTrue(eff[0]["_override"])
        self.assertEqual(cr.criterion_count("cofinancing", eff), ("0.5", 1, 50))
        self.assertEqual(cr.roll_up("cofinancing", eff), "Partial, with effort")

    def test_an_untouched_component_is_left_alone(self):
        facts = [f("a", score=1.0, met=True), f("b", active=False)]
        eff = cr.with_session_edits(facts, {"a": 0.0})
        self.assertEqual(eff[0]["score"], 0.0)
        self.assertFalse(eff[1]["active"])          # no entry → derivation still drives it
        self.assertNotIn("_override", eff[1])

    def test_with_session_edits_does_not_mutate_the_input(self):
        facts = [f("a", active=False)]
        cr.with_session_edits(facts, {"a": 1.0})
        self.assertFalse(facts[0]["active"])


class TestSamUeiIsTheOneExceptionToEditability(unittest.TestCase):
    """#5 — SAM.gov / UEI is a US-federal registration. For a non-US-government funder
    there is nothing a reviewer could learn from the call that makes it relevant, so it
    must not be scoreable into MUST-5's denominator."""

    def test_sam_uei_is_not_editable_while_the_derivation_excludes_it(self):
        self.assertFalse(cr.is_editable(f("sam_uei", active=False, hard=True)))

    def test_sam_uei_is_editable_once_the_derivation_activates_it(self):
        # A grants.gov / US-federal call, or a donor record that demands it.
        self.assertTrue(cr.is_editable(f("sam_uei", active=True, hard=True)))

    def test_every_other_compliance_component_is_editable_when_greyed(self):
        for ck in ("audited_financials", "tax_exempt", "safeguarding", "partner_mou",
                   "govt_mou", "govt_endorsement", "local_board", "indirect_cost",
                   "authorized_signatory", "partnership", "cofinance"):
            with self.subTest(component=ck):
                self.assertTrue(cr.is_editable(f(ck, active=False)))


class TestUnsureRendersAsADashNotZero(unittest.TestCase):
    def test_an_unmeasured_component_is_not_scored(self):
        self.assertFalse(cr.is_scored(f("a", active=True, score=None, met=None)))

    def test_a_measured_zero_is_scored(self):
        self.assertTrue(cr.is_scored(f("a", score=0.0, met=False)))
        self.assertTrue(cr.is_scored(f("a", score=None, met=False)))

    def test_a_measured_partial_is_scored(self):
        self.assertTrue(cr.is_scored(f("a", score=0.5)))

    def test_unmeasured_components_stay_out_of_the_denominator(self):
        facts = [f("a", score=1.0, met=True), f("b", active=True, score=None, met=None)]
        self.assertEqual(cr.criterion_count("competitiveness", facts), ("1", 1, 100))


class TestOrCriterionCounting(unittest.TestCase):
    def test_one_satisfied_route_is_the_whole_criterion(self):
        facts = [f("rel_grantee", met=True), f("rel_contact", met=False),
                 f("rel_engaged", met=False)]
        self.assertEqual(cr.criterion_count("funder_relationship", facts), ("1", 1, 100))

    def test_an_unsatisfied_or_criterion_counts_normally(self):
        facts = [f("rel_grantee", met=False), f("rel_contact", met=False)]
        self.assertEqual(cr.criterion_count("funder_relationship", facts), ("0", 2, 0))


class TestSnapAndComponentScore(unittest.TestCase):
    def test_snap_clamps_to_the_allowed_values(self):
        for raw, want in ((0.3, 0.5), (0.2, 0.0), (0.9, 1.0), (-4, 0.0), (7, 1.0),
                          ("0.5", 0.5), (None, 0.0), ("junk", 0.0)):
            with self.subTest(raw=raw):
                self.assertEqual(cr.snap(raw), want)

    def test_met_maps_to_a_score_when_no_score_is_carried(self):
        self.assertEqual(cr.component_score(f("a", met=True)), 1.0)
        self.assertEqual(cr.component_score(f("a", met=False)), 0.0)
        self.assertEqual(cr.component_score(f("a", met=None)), 0.5)

    def test_an_explicit_score_wins_over_met(self):
        self.assertEqual(cr.component_score(f("a", score=0.5, met=True)), 0.5)


if __name__ == "__main__":
    unittest.main()
