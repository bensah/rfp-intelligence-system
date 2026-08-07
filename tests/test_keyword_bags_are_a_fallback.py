"""The admin keyword bags must never overturn the derivation (action #4).

`_apply_criteria_keywords` supplements the objective org × call × donor derivation with
admin-configurable per-criterion terms (Settings → Policies). Its POSITIVE branch was
correctly gated — "confirms this criterion when it can't be derived", exactly as the
Settings help text promises. Its NEGATIVE branch was NOT: it ran FIRST and
unconditionally, so one substring beat the whole derivation.

Measured on the live catalog, that turned four TB / malaria drug-discovery calls from a
derived "Strongly aligns" into "No" — the `strategic_fit` negatives carry "drug
discovery" to screen out basic science, and it fired on calls squarely inside the org's
own priority areas, costing each 15 points of Bid Strength. Three of the four were in the
set of stored Declines the model would not otherwise have declined.

The `cofinancing` bag is worse than over-broad, it is INVERTED: its negatives contain
"cost-share required", which is a substring of "**no** cost-share required" — so the
phrase meaning the requirement is ABSENT scored the criterion 0.

The bags also wrote the bare strings "Yes"/"No", which are in NO criterion's response
vocabulary; they scored only via criterion_score's legacy fallback and displayed as a
bare "No" beside a panel of rich labels.

The feature is deliberate and UI-exposed, so it is KEPT — just demoted to what it always
claimed to be: a fallback.

Run:  python -m unittest tests.test_keyword_bags_are_a_fallback
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.auto_scorer import _apply_criteria_keywords          # noqa: E402
from core.policies import DEFAULT_POLICIES                     # noqa: E402
from core.scorer import CRITERION_RESPONSES, criterion_score   # noqa: E402

POL = DEFAULT_POLICIES


class DerivationWinsTests(unittest.TestCase):
    def test_the_reported_case_a_drug_discovery_call_keeps_its_derived_match(self):
        # "Malaria drug discovery: 9th African call for proposals" — derived
        # "Strongly aligns" (the org's priority areas include Malaria & NTDs and TB).
        vals = {"strategic_fit": "Strongly aligns"}
        out = _apply_criteria_keywords(dict(vals), "malaria drug discovery call", POL)
        self.assertEqual(out["strategic_fit"], "Strongly aligns")

    def test_no_negative_term_can_overturn_any_derived_value(self):
        for key, rule in (POL.get("criteria") or {}).items():
            if key not in CRITERION_RESPONSES:
                continue
            neg = [n for n in (rule.get("negative") or []) if n]
            if not neg:
                continue
            derived = CRITERION_RESPONSES[key][0]          # the best label
            out = _apply_criteria_keywords({key: derived}, neg[0].lower(), POL)
            self.assertEqual(out[key], derived, f"{key}: {neg[0]!r} overturned it")

    def test_a_derived_negative_is_equally_protected(self):
        # The guard is "already determined", not "already positive".
        out = _apply_criteria_keywords({"qualification": "No, not eligible"},
                                       "open to all, any legal entity", POL)
        self.assertEqual(out["qualification"], "No, not eligible")

    def test_the_inverted_cofinancing_phrase_no_longer_scores_zero(self):
        # "no cost-share required" contains "cost-share required".
        out = _apply_criteria_keywords({"cofinancing": "Yes, fully met"},
                                       "no cost-share required for this award", POL)
        self.assertEqual(out["cofinancing"], "Yes, fully met")


class StillAFallbackTests(unittest.TestCase):
    """Demoted, not disabled — an undetermined criterion is still assisted."""

    def test_a_negative_term_still_fills_an_undetermined_criterion(self):
        out = _apply_criteria_keywords({"qualification": None},
                                       "domestic applicants only", POL)
        self.assertEqual(criterion_score(out["qualification"]), 0)

    def test_a_positive_term_still_fills_an_undetermined_criterion(self):
        out = _apply_criteria_keywords({"strategic_fit": None},
                                       "health system strengthening at scale", POL)
        self.assertEqual(criterion_score(out["strategic_fit"]), 2)

    def test_negatives_still_beat_positives_on_an_undetermined_criterion(self):
        out = _apply_criteria_keywords(
            {"qualification": None}, "open to all applicants; domestic applicants only", POL)
        self.assertEqual(criterion_score(out["qualification"]), 0)

    def test_a_criterion_with_no_matching_term_is_left_undetermined(self):
        out = _apply_criteria_keywords({"strategic_fit": None},
                                       "an unrelated call about bridges", POL)
        self.assertIsNone(out["strategic_fit"])


class CanonicalVocabularyTests(unittest.TestCase):
    """The bags wrote bare "Yes"/"No", in no criterion's vocabulary."""

    def test_every_value_the_bags_write_is_a_real_response_option(self):
        for key, rule in (POL.get("criteria") or {}).items():
            if key not in CRITERION_RESPONSES:
                continue
            for terms, want in ((rule.get("negative"), 0), (rule.get("positive"), 2)):
                t = next((x for x in (terms or []) if x), None)
                if not t:
                    continue
                out = _apply_criteria_keywords({key: None}, t.lower(), POL)
                self.assertIn(out[key], CRITERION_RESPONSES[key],
                              f"{key}: wrote {out[key]!r}, not a response option")
                self.assertEqual(criterion_score(out[key]), want, key)

    def test_a_negated_term_is_not_a_hit(self):
        # The `cofinancing` bag fires on its OWN positives: "match required" is inside
        # "no match required", "cost-share required" inside "no cost-share required" —
        # and negatives run first.
        from core.auto_scorer import _term_hit
        for text, term in (("no cost-share required", "cost-share required"),
                           ("no match required", "match required"),
                           ("without matching funds required", "matching funds required"),
                           ("not fully funded", "fully funded")):
            self.assertFalse(_term_hit(term, text), f"{term!r} hit in {text!r}")

    def test_an_unnegated_term_is_still_a_hit(self):
        from core.auto_scorer import _term_hit
        for text, term in (("cost-share required for this award", "cost-share required"),
                           ("a match required from the applicant", "match required"),
                           ("the project is fully funded", "fully funded")):
            self.assertTrue(_term_hit(term, text), f"{term!r} missed in {text!r}")

    def test_a_later_unnegated_occurrence_still_counts(self):
        from core.auto_scorer import _term_hit
        self.assertTrue(_term_hit(
            "cost-share required",
            "no cost-share required in phase 1; cost-share required in phase 2"))

    def test_the_no_cost_share_phrase_now_confirms_instead_of_failing(self):
        out = _apply_criteria_keywords({"cofinancing": None},
                                       "no cost-share required for this award", POL)
        self.assertEqual(criterion_score(out["cofinancing"]), 2)

    def test_the_bare_words_are_gone(self):
        out = _apply_criteria_keywords({"qualification": None},
                                       "domestic applicants only", POL)
        self.assertNotEqual(out["qualification"], "No")
        self.assertEqual(out["qualification"], "No, not eligible")


class SafetyTests(unittest.TestCase):
    def test_empty_text_changes_nothing(self):
        vals = {"strategic_fit": None, "qualification": "Yes, fully"}
        self.assertEqual(_apply_criteria_keywords(dict(vals), "", POL), vals)

    def test_a_criterion_absent_from_values_is_ignored(self):
        # `criteria.feasibility` is a separate hard kill-switch, not scored here.
        out = _apply_criteria_keywords({"strategic_fit": None}, "anything", POL)
        self.assertNotIn("feasibility", out)

    def test_no_configured_terms_leaves_values_alone(self):
        vals = {"strategic_fit": None}
        self.assertEqual(
            _apply_criteria_keywords(dict(vals), "text", {"criteria": {}}), vals)


if __name__ == "__main__":
    unittest.main(verbosity=2)
