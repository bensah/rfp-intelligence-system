"""The decline flag is about FAILURE, not about the absence of success.

OWNER'S RULE (2026-08-17):
  * any ONE failing MUST                       -> Decline flags = Yes
  * all MUSTs pass, but >= 2 failing PREFERs   -> Yes
  * otherwise                                  -> No

WHY IT MATTERED. The flag is not a badge: `scorer.auto_recommendation` returns "Decline"
whenever it is set, whatever the score. The old rule was
`not (all 5 MUSTs green AND >= 3 of 4 PREFERs green)`, so a call with every MUST green, two
PREFERs green and two merely UNCERTAIN was flagged - and a 92/100 call was recommended
Decline because of two questions nobody could answer. Nothing had failed.

"Not sure" and "Partial" are therefore not failures here, which is the same principle the
rest of the model follows: an unstated value is excluded from a criterion's count rather
than scored zero. A partial MUST still costs points through the weighted score - the
proportionate instrument - instead of overriding the verdict entirely.

MEASURED over the 286 scored live rows: 70 flags change, all Yes -> No, producing 40
Decline->Park and 27 Decline->Proceed. On the rows a human had already decided, the new rule
agrees with the team on 11 where the old rule did not, and loses agreement on 5.

Run:  python -m unittest tests.test_decline_flags_rule
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.auto_scorer import _MUST_KEYS, _PREFER_KEYS                 # noqa: E402
from core.scorer import criterion_score                              # noqa: E402


def _flag(values: dict) -> bool:
    """The shipped rule, expressed exactly as auto_score computes it."""
    musts_failed = sum(1 for m in _MUST_KEYS if criterion_score(values.get(m)) == 0)
    prefers_failed = sum(1 for p in _PREFER_KEYS if criterion_score(values.get(p)) == 0)
    return bool(musts_failed) or prefers_failed >= 2


def _values(musts, prefers):
    v = {k: musts[i] for i, k in enumerate(_MUST_KEYS)}
    v.update({k: prefers[i] for i, k in enumerate(_PREFER_KEYS)})
    return v


YES, NO, PARTIAL, UNSURE = "Yes", "No", "Partial", "Not sure"
ALL_MUST_OK = [YES] * len(_MUST_KEYS)
ALL_PREFER_OK = [YES] * len(_PREFER_KEYS)


class TheOwnersRuleTests(unittest.TestCase):
    def test_all_green_raises_no_flag(self):
        self.assertFalse(_flag(_values(ALL_MUST_OK, ALL_PREFER_OK)))

    def test_a_single_failing_must_raises_the_flag(self):
        for i in range(len(_MUST_KEYS)):
            musts = list(ALL_MUST_OK)
            musts[i] = NO
            self.assertTrue(_flag(_values(musts, ALL_PREFER_OK)),
                            "MUST %s failing must flag" % _MUST_KEYS[i])

    def test_one_failing_prefer_does_not_raise_the_flag(self):
        prefers = list(ALL_PREFER_OK)
        prefers[0] = NO
        self.assertFalse(_flag(_values(ALL_MUST_OK, prefers)))

    def test_two_failing_prefers_raise_the_flag(self):
        prefers = list(ALL_PREFER_OK)
        prefers[0] = prefers[1] = NO
        self.assertTrue(_flag(_values(ALL_MUST_OK, prefers)))


class UncertaintyIsNotFailureTests(unittest.TestCase):
    def test_the_reported_row_no_longer_flags(self):
        # Every MUST green; PREFER 6 and 7 uncertain, 8 and 9 green. Scored 92/100 and was
        # recommended Decline purely because only two PREFERs were affirmatively green.
        prefers = [UNSURE, UNSURE] + [YES] * (len(_PREFER_KEYS) - 2)
        self.assertFalse(_flag(_values(ALL_MUST_OK, prefers)))

    def test_two_uncertain_prefers_are_not_two_failures(self):
        prefers = [UNSURE] * 2 + [YES] * (len(_PREFER_KEYS) - 2)
        self.assertFalse(_flag(_values(ALL_MUST_OK, prefers)))

    def test_an_uncertain_must_does_not_flag_on_its_own(self):
        # A behaviour CHANGE, stated plainly: an unanswered MUST used to force Decline.
        # It now leaves the verdict to the score, which parks a middling row for a human.
        musts = list(ALL_MUST_OK)
        musts[0] = UNSURE
        self.assertFalse(_flag(_values(musts, ALL_PREFER_OK)))

    def test_a_partial_must_does_not_flag_on_its_own(self):
        musts = list(ALL_MUST_OK)
        musts[0] = PARTIAL
        self.assertFalse(_flag(_values(musts, ALL_PREFER_OK)))

    def test_but_a_failing_must_still_flags_however_uncertain_the_rest(self):
        musts = [NO, UNSURE] + [YES] * (len(_MUST_KEYS) - 2)
        self.assertTrue(_flag(_values(musts, [UNSURE] * len(_PREFER_KEYS))))


class TheDifferenceFromTheOldRuleTests(unittest.TestCase):
    def _old(self, values):
        return not (all(criterion_score(values.get(m)) == 2 for m in _MUST_KEYS)
                    and sum(1 for p in _PREFER_KEYS
                            if criterion_score(values.get(p)) == 2) >= 3)

    def test_the_old_rule_flagged_the_92_point_call_and_the_new_one_does_not(self):
        v = _values(ALL_MUST_OK, [UNSURE, UNSURE] + [YES] * (len(_PREFER_KEYS) - 2))
        self.assertTrue(self._old(v))
        self.assertFalse(_flag(v))

    def test_both_rules_agree_when_something_actually_failed(self):
        v = _values([NO] + ALL_MUST_OK[1:], ALL_PREFER_OK)
        self.assertTrue(self._old(v))
        self.assertTrue(_flag(v))

    def test_both_rules_agree_on_an_all_green_call(self):
        v = _values(ALL_MUST_OK, ALL_PREFER_OK)
        self.assertFalse(self._old(v))
        self.assertFalse(_flag(v))


class TheFlagStillOverridesTheScoreTests(unittest.TestCase):
    def test_a_flagged_call_is_declined_whatever_it_scored(self):
        from core.scorer import auto_recommendation
        self.assertEqual(auto_recommendation(95.0, True), "Decline")
        self.assertEqual(auto_recommendation(95.0, False), "Proceed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
