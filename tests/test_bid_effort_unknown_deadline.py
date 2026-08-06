"""PREFER-9 must not invent a time verdict it never measured.

THE REPORTED BUG. A call whose deadline was never extracted rendered as:

    🔴 PREFER 9 · Bid effort — Not enough time, no team · 1/1 · 100%
       ? Time before the deadline  (Not sure — not stated by this call; excluded)
       ✓ Has a business-development team

The badge asserts the single WORST verdict on the scale while its own component panel
says the time check was excluded and the team is in place. The chain:

  1. no deadline  → scorer.bid_effort_label(None, bd) → None
  2.              → criteria_derive.derive_bid_effort → None
  3. review_rfp._baseline_val → `_derived.get(key) or stored` → None (stored is None too)
  4. → scorer.default_response(key, None): bid_effort is the ONE criterion with no
       "Not sure" option, so the fallback `opts[-1]` picked the LAST option — and the
       list is ordered best→worst, so that is "Not enough time, no team".

Not cosmetic: that label scores 0, and PREFER-9 feeds Bid Strength and the
Proceed/Park/Decline suggestion. Unknown time is now EXCLUDED, not failed — matching
what the Review editor's own rule (`_bid_rule`) already did, so VIEW and EDIT agree.

Run:  python -m unittest tests.test_bid_effort_unknown_deadline
"""
import os
import sys
import unittest
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                                   # noqa: E402
from core.scorer import (CRITERION_RESPONSES, criterion_score,           # noqa: E402
                         default_response, bid_effort_label)

TEAM = {"org_has_bd_team": "true"}
NO_TEAM = {"org_has_bd_team": "false"}
NO_DEADLINE = {"opportunity_title": "Global collaboration action on climate and health"}


def _dl(days):
    return {"call_submission_deadline": (date.today() + timedelta(days=days)).isoformat()}


class TheReportedCaseTests(unittest.TestCase):
    def test_a_missing_deadline_no_longer_asserts_the_worst_verdict(self):
        label = CD.derive_bid_effort(NO_DEADLINE, TEAM)
        self.assertIsNotNone(label)
        self.assertNotIn("Not enough time", label)
        self.assertEqual(criterion_score(label), 2)      # matches the panel's 1/1 · 100%

    def test_the_badge_now_agrees_with_the_component_panel(self):
        # Panel: time EXCLUDED (inactive), team MET → ratio 1/1 = 100%.
        facts = {f["key"]: f for f in CD._bid_effort_factors(NO_DEADLINE, TEAM)}
        self.assertFalse(facts["bid_time"]["active"])
        self.assertTrue(facts["bid_team"]["met"])
        measured = [f for f in facts.values()
                    if f.get("active", True) and (f.get("score") is not None
                                                  or f.get("met") is not None)]
        num = sum(f["score"] if f.get("score") is not None else (1.0 if f["met"] else 0.0)
                  for f in measured)
        self.assertEqual((num, len(measured)), (1.0, 1))          # the displayed 1/1
        # ...and the label must be worth the same 100%, not 0.
        self.assertEqual(criterion_score(CD.derive_bid_effort(NO_DEADLINE, TEAM)), 2)

    def test_no_deadline_and_no_team_reports_the_team_gap_only(self):
        label = CD.derive_bid_effort(NO_DEADLINE, NO_TEAM)
        self.assertNotIn("Not enough time", label)                # time was not measured
        self.assertIn("no dedicated team", label)                 # the part we DID measure

    def test_derive_is_total_across_every_deadline_shape(self):
        for rfp in ({}, NO_DEADLINE, {"call_submission_deadline": None},
                    {"call_submission_deadline": ""},
                    {"call_submission_deadline": "not-a-date"}):
            for st in (TEAM, NO_TEAM, {}, None):
                self.assertIsNotNone(CD.derive_bid_effort(rfp, st), (rfp, st))


class RealDeadlinesStillScoreTests(unittest.TestCase):
    """Excluding UNKNOWN time must not stop a KNOWN tight deadline from biting."""

    def test_ample_time(self):
        self.assertEqual(CD.derive_bid_effort(_dl(30), TEAM),
                         "Ample time, sufficient resources")

    def test_tight_deadline_still_downgrades(self):
        self.assertEqual(CD.derive_bid_effort(_dl(10), TEAM),
                         "Tight but doable, with a team")
        self.assertEqual(criterion_score(CD.derive_bid_effort(_dl(10), TEAM)), 1)

    def test_a_real_shortage_of_time_still_scores_zero(self):
        label = CD.derive_bid_effort(_dl(2), NO_TEAM)
        self.assertEqual(label, "Not enough time, no team")
        self.assertEqual(criterion_score(label), 0)

    def test_a_passed_deadline_still_scores_zero(self):
        self.assertEqual(criterion_score(CD.derive_bid_effort(_dl(-5), NO_TEAM)), 0)

    def test_a_completed_submission_is_never_penalised_for_time(self):
        row = {**_dl(-30), "progress_status": "Completed"}
        self.assertEqual(CD.derive_bid_effort(row, TEAM),
                         "Ample time, sufficient resources")


class PureLabelMappingTests(unittest.TestCase):
    """`bid_effort_label` stays a pure mapping — the POLICY for unknown time lives in
    derive_bid_effort, so the two don't have to be changed together."""

    def test_the_mapping_still_reports_unknown_as_none(self):
        self.assertIsNone(bid_effort_label(None, True))

    def test_the_matrix_is_unchanged(self):
        self.assertEqual(bid_effort_label(30, True), "Ample time, sufficient resources")
        self.assertEqual(bid_effort_label(30, False), "Ample time, but no dedicated team")
        self.assertEqual(bid_effort_label(10, True), "Tight but doable, with a team")
        self.assertEqual(bid_effort_label(10, False), "Tight, and no dedicated team")
        self.assertEqual(bid_effort_label(1, True), "Not enough time, even with a team")
        self.assertEqual(bid_effort_label(1, False), "Not enough time, no team")


class DefaultResponseFallbackTests(unittest.TestCase):
    """The landmine underneath the reported bug: an undeterminable criterion must not
    resolve to the most damaging option just because it lacks a "Not sure" entry."""

    def test_an_unknown_value_never_resolves_to_a_zero_scoring_verdict(self):
        # For the eight criteria that HAVE "Not sure" it is legitimately the last
        # option, so "not opts[-1]" is the wrong invariant. The real one: an
        # undetermined criterion must never come back as a verdict worth 0.
        for key in CRITERION_RESPONSES:
            got = default_response(key, None)
            self.assertNotEqual(criterion_score(got), 0,
                                f"{key}: unknown resolved to a 0-scoring verdict ({got!r})")

    def test_bid_effort_specifically_no_longer_defaults_to_the_worst(self):
        got = default_response("bid_effort", None)
        self.assertNotEqual(got, "Not enough time, no team")
        self.assertEqual(criterion_score(got), 1)          # the Park midpoint

    def test_criteria_with_not_sure_are_untouched(self):
        for key in ("qualification", "strategic_fit", "capacity", "geographic_fit",
                    "cofinancing", "funding_quality", "funder_relationship",
                    "competitiveness"):
            self.assertEqual(default_response(key, None), "Not sure", key)

    def test_a_real_stored_label_still_passes_through(self):
        self.assertEqual(default_response("bid_effort", "Not enough time, no team"),
                         "Not enough time, no team")
        self.assertEqual(default_response("capacity", "Yes, comfortably"),
                         "Yes, comfortably")

    def test_legacy_values_still_map_by_score(self):
        self.assertEqual(criterion_score(default_response("capacity", "Partial")), 1)


class ViewAndEditAgreeTests(unittest.TestCase):
    """The VIEW badge and the EDIT-mode rule must produce the same label for the same
    components — the divergence is what made the bug visible."""

    @staticmethod
    def _bid_rule(by_key):
        # Mirror of views.review_rfp._bid_rule (not importable — Streamlit module).
        t = by_key.get("bid_time", 1.0)
        has_team = by_key.get("bid_team", 0.0) >= 1.0
        if t >= 1.0:
            return ("Ample time, sufficient resources" if has_team
                    else "Ample time, but no dedicated team")
        if t >= 0.5:
            return ("Tight but doable, with a team" if has_team
                    else "Tight, and no dedicated team")
        return ("Not enough time, even with a team" if has_team
                else "Not enough time, no team")

    def _components(self, rfp, settings):
        out = {}
        for f in CD._bid_effort_factors(rfp, settings):
            if not f.get("active", True):
                continue                                   # excluded → rule's default
            out[f["key"]] = (f["score"] if f.get("score") is not None
                             else (1.0 if f.get("met") else 0.0))
        return out

    def test_they_agree_on_every_deadline_and_team_combination(self):
        cases = [NO_DEADLINE, _dl(30), _dl(10), _dl(2), _dl(-5),
                 {**_dl(-30), "progress_status": "Completed"}]
        for rfp in cases:
            for settings in (TEAM, NO_TEAM):
                view = CD.derive_bid_effort(rfp, settings)
                edit = self._bid_rule(self._components(rfp, settings))
                self.assertEqual(view, edit, f"{rfp} / {settings}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
