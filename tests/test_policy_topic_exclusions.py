"""Financial-integrity topics are out of scope and must be rejected on topic alone.

THE LEAK. A regional economic bloc tendered baseline assessments on trade-based money
laundering. It reached a health pipeline, and every gate that should have caught it had a
reason not to:

  * the theme gate  — an excluded term is the only hard topic reject, and the list held
                      nothing but clinical-trial and basic-research wording
  * the geo gate    — the bloc is a legitimate African region, so geography passed
  * the deadline gate — the deadline never extracted, so it had nothing to reject on

The deadline and geography holes are fixed elsewhere. This one is a SCOPE question, not a
bug: the call is simply not the kind of work a health implementer does, and the place to
say so is the topic policy. Being policy, an admin can reverse it in Settings.

The two things worth pinning are the FALSE-POSITIVE boundaries, because the theme gate's
matcher is not a plain substring test: terms of five characters or fewer (and ALL-CAPS
ones) match on a whole-word boundary, while longer lowercase terms match as a PREFIX stem.
That makes one acronym actively dangerous to add, and makes stem choice load-bearing.
"""
from __future__ import annotations

import copy
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.auto_scorer import _theme_hit, theme_eligible      # noqa: E402
from core.policies import DEFAULT_POLICIES                   # noqa: E402

EXCLUDED = DEFAULT_POLICIES["themes"]["excluded_any"]


def _policy():
    """The shipped topic policy, with a realistic required-theme list."""
    p = copy.deepcopy(DEFAULT_POLICIES)
    p["themes"]["required_any"] = ["health", "malaria", "HIV", "immuni", "nutrition",
                                  "diagnostic", "essential medicine"]
    return p


def _cand(title, body="", agency=""):
    return {"opportunity_title": title, "brief_description": body,
            "funding_agency": agency, "call_geographic_scope": []}


class TheOffTopicCallIsRejectedTests(unittest.TestCase):
    def test_a_trade_based_money_laundering_tender_is_rejected(self):
        ok, why = theme_eligible(
            _cand("EOI - Multinational - Baseline Assessments on Trade Based Money "
                  "Laundering", agency="A Regional Economic Bloc"),
            _policy(), llm_theme=False)
        self.assertFalse(ok)
        self.assertIn("excluded theme", why)

    def test_the_neighbouring_topics_are_rejected_too(self):
        for title in ["Anti-Corruption and Public Accountability Programme",
                      "Anti-money-laundering supervision capacity building",
                      "Countering terrorist financing in the Sahel",
                      "Trade finance facility for regional importers",
                      "Trade financing guarantees for small traders",
                      "Curbing illicit financial flows"]:
            with self.subTest(title=title):
                self.assertFalse(theme_eligible(_cand(title), _policy(),
                                                llm_theme=False)[0])

    def test_rejection_does_not_depend_on_a_missing_geography_or_deadline(self):
        # The point of putting this in topic policy: it stands alone. Give the call a
        # perfectly good geography and it is still out of scope.
        c = _cand("Baseline Assessments on Trade Based Money Laundering")
        c["call_geographic_scope"] = ["Sub-Saharan Africa"]
        self.assertFalse(theme_eligible(c, _policy(), llm_theme=False)[0])


class TheAcronymAMLIsDeliberatelyNotExcludedTests(unittest.TestCase):
    """AML is Acute Myeloid Leukaemia. Short terms match on a whole-word boundary, so
    adding it would hard-reject genuine oncology calls that carry the abbreviation."""

    def test_aml_is_absent_from_the_exclusion_list(self):
        self.assertNotIn("aml", [t.lower() for t in EXCLUDED])

    def test_an_oncology_call_using_the_acronym_survives(self):
        ok, _ = theme_eligible(
            _cand("Improving access to treatment for acute myeloid leukaemia (AML)",
                  body="Paediatric cancer diagnostic and essential medicine access."),
            _policy(), llm_theme=False)
        self.assertTrue(ok)

    def test_the_acronym_would_have_matched_had_it_been_added(self):
        # Proves the carve-out is doing real work rather than guarding a case that
        # could never fire.
        self.assertTrue(_theme_hit("AML", "treatment for acute myeloid leukaemia (AML)"))


class TheStemsDoNotCatchHealthWordingTests(unittest.TestCase):
    """"trade financ" must cover finance/financing without reaching health financing or
    trade in medicines — the two words have to be adjacent."""

    def test_genuine_health_titles_are_untouched(self):
        for title in ["Health financing and domestic resource mobilisation for UHC",
                      "Strengthening pharmaceutical procurement and supply chains",
                      "Transparency and accountability in health service delivery",
                      "Cross-border trade in essential medicines",
                      "Financing malaria elimination in border districts"]:
            with self.subTest(title=title):
                hits = [t for t in EXCLUDED if _theme_hit(t, title)]
                self.assertEqual(hits, [], f"unexpected exclusion hit: {hits}")

    def test_the_stem_covers_both_word_endings(self):
        self.assertTrue(_theme_hit("trade financ", "Trade finance facility"))
        self.assertTrue(_theme_hit("trade financ", "Trade financing for traders"))


class AnIncidentalMentionIsNotARejectTests(unittest.TestCase):
    """Unchanged gate behaviour, re-asserted because the list just grew: an excluded term
    only hard-rejects from the TITLE, or from the body when the call is off-theme anyway.
    An on-theme call that name-drops a term in passing must survive."""

    def test_an_on_theme_call_mentioning_a_term_in_its_body_survives(self):
        ok, _ = theme_eligible(
            _cand("Strengthening malaria commodity supply chains",
                  body="Grantees must comply with the funder's anti-corruption policy "
                       "and money laundering safeguards."),
            _policy(), llm_theme=False)
        self.assertTrue(ok)

    def test_the_same_term_in_the_TITLE_still_rejects(self):
        ok, _ = theme_eligible(
            _cand("Anti-corruption safeguards in malaria commodity supply chains"),
            _policy(), llm_theme=False)
        self.assertFalse(ok)


class TheListStaysAdminEditableTests(unittest.TestCase):
    def test_an_admin_who_clears_the_topic_exclusions_sees_the_call_again(self):
        # Reversibility is the reason this went in policy rather than code.
        pol = _policy()
        pol["themes"]["excluded_any"] = []
        pol["themes"]["required_any"] = []
        ok, _ = theme_eligible(
            _cand("Baseline Assessments on Trade Based Money Laundering"),
            pol, llm_theme=False)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
