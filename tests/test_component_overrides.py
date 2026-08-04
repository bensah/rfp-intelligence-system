"""Regression tests for persisted HUMAN component verdicts (migration 087).

The Review screen lets a reviewer score each criterion's COMPONENT sub-factors, but those
numbers were never stored — Save wrote only the rolled-up criterion label, so a corrected
component reverted on the next render (the panel re-derives from org profile / donor intel /
call text every time).

`rfp_submissions.criteria_component_overrides` now stores them as
    {criterion: {component: score}}
and `criteria_derive.apply_component_overrides` merges them ON TOP of the derived breakdown,
so the human answer WINS over the inference.

Run:  python -m unittest tests.test_component_overrides
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.criteria_derive import apply_component_overrides    # noqa: E402


def _bd():
    """A derived breakdown where the human disagrees with the machine."""
    return {
        "cofinancing": [
            {"key": "authorized_signatory", "name": "Authorized signatory (this donor)",
             "score": 0.0, "met": False, "active": True, "hard": True},
            {"key": "audited_financials", "name": "Audited financials",
             "score": None, "met": None, "active": False},
        ],
        "bid_effort": [
            {"key": "bid_time", "name": "Time before the deadline",
             "score": 0.0, "met": False, "active": True},
        ],
    }


class ApplyOverrideTests(unittest.TestCase):
    def test_human_verdict_wins_over_derivation(self):
        out = apply_component_overrides(_bd(), {"cofinancing": {"authorized_signatory": 1}})
        it = out["cofinancing"][0]
        self.assertEqual(it["score"], 1.0)
        self.assertIs(it["met"], True)
        self.assertTrue(it["_override"])

    def test_untouched_components_keep_the_derivation(self):
        out = apply_component_overrides(_bd(), {"cofinancing": {"authorized_signatory": 1}})
        other = out["cofinancing"][1]
        self.assertIsNone(other["score"])
        self.assertFalse(other.get("_override", False))
        self.assertEqual(out["bid_effort"][0]["score"], 0.0)   # different criterion untouched

    def test_override_can_also_fail_a_passing_component(self):
        bd = _bd()
        bd["bid_effort"][0].update(score=1.0, met=True)
        out = apply_component_overrides(bd, {"bid_effort": {"bid_time": 0}})
        self.assertEqual(out["bid_effort"][0]["score"], 0.0)
        self.assertIs(out["bid_effort"][0]["met"], False)

    def test_partial_score_maps_to_uncertain(self):
        out = apply_component_overrides(_bd(), {"cofinancing": {"authorized_signatory": 0.5}})
        it = out["cofinancing"][0]
        self.assertEqual(it["score"], 0.5)
        self.assertIsNone(it["met"])

    def test_override_activates_an_inactive_component(self):
        # Scoring something the call didn't visibly impose asserts that it DOES apply.
        out = apply_component_overrides(_bd(), {"cofinancing": {"audited_financials": 1}})
        it = out["cofinancing"][1]
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 1.0)

    def test_scores_are_clamped(self):
        out = apply_component_overrides(_bd(), {"cofinancing": {"authorized_signatory": 9}})
        self.assertEqual(out["cofinancing"][0]["score"], 1.0)
        out = apply_component_overrides(_bd(), {"cofinancing": {"authorized_signatory": -3}})
        self.assertEqual(out["cofinancing"][0]["score"], 0.0)

    def test_garbage_is_ignored_not_raised(self):
        for bad in (None, {}, "nonsense", {"cofinancing": "nope"},
                    {"cofinancing": {"authorized_signatory": "abc"}},
                    {"nosuchcriterion": {"x": 1}},
                    {"cofinancing": {"nosuchcomponent": 1}}):
            out = apply_component_overrides(_bd(), bad)
            self.assertEqual(out["cofinancing"][0]["score"], 0.0, repr(bad))

    def test_merge_is_idempotent(self):
        ov = {"cofinancing": {"authorized_signatory": 1}}
        out = apply_component_overrides(apply_component_overrides(_bd(), ov), ov)
        self.assertEqual(out["cofinancing"][0]["score"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
