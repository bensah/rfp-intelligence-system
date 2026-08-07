"""PREFER-7 must recognise the funder behind a programme brand.

THE REPORTED CASE. A Grand Challenges RFP (a Bill & Melinda Gates Foundation programme —
the call itself cites "the Gates Foundation's indirect cost policy") scored:

    🟡 PREFER 7 · Donor relationship — Some contact · 1/3 · 33%
       ○ Past / current grantee of this donor      (alternative route — not needed)
       ○ Donor engaged (prior contact, no funding yet)
       ✓ Shared collaborator or registered

…for an org whose `org_funder_history` lists "Bill & Melinda Gates Foundation" as a
long-standing funder. Two independent faults:

1. `_relationship_factors`, `derive_funder_relationship` and `_registered_on_portal` all
   tested the funder history with `_funder_in_history`, which compares
   `rfp.funding_agency` as a RAW STRING. "Grand Challenges" never matches "Bill &
   Melinda Gates Foundation". Everywhere else in the model the same fact is matched
   CANONICALLY (`_canonical_donor_match` — acronym ⇄ alias ⇄ full name through
   donor_intel); PREFER-7 was the odd one out. All three now share `_is_past_grantee`,
   so the label, the components and PREFER-8's portal edge cannot disagree.

2. The ratio. PREFER-7 is an OR-criterion: the panel labels unused tiers "(alternative
   route — not needed)", yet the ratio averaged all three, so the BEST possible outcome
   displayed as 1/3 · 33% — a denominator counting exactly the rows the panel says were
   not required.

Run:  python -m unittest tests.test_prefer7_canonical_grantee
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

# donor_intel row as match_donor resolves it from the alias "Grand Challenges".
BMGF = {"donor": "Bill & Melinda Gates Foundation",
        "donor_short": "BMGF",
        "canonical_key": "bill_melinda_gates_foundation",
        "donor_aliases": "Gates Foundation; BMGF; Grand Challenges"}
ORG = {"org_funder_history": ["Gavi, the Vaccine Alliance",
                              "Bill & Melinda Gates Foundation",
                              "Unitaid"]}
# The call is published under the PROGRAMME brand, not the funder's legal name.
RFP = {"funding_agency": "Grand Challenges",
       "opportunity_title": "Multiplex Platforms to Assess Indicators of Micronutrient Status"}


def _by_key(items):
    return {i["key"]: i for i in items}


class CanonicalGranteeTests(unittest.TestCase):
    def test_a_programme_brand_resolves_to_the_funder_behind_it(self):
        self.assertTrue(CD._is_past_grantee(ORG, RFP, BMGF))

    def test_the_component_now_reads_met(self):
        f = _by_key(CD._relationship_factors(ORG, RFP, BMGF))["rel_grantee"]
        self.assertTrue(f["met"])
        self.assertEqual(CD.component_mark(f)[0], "✓")

    def test_the_label_agrees_with_the_component(self):
        # The label function used the same raw-name test, so both had to move together.
        self.assertEqual(CD.derive_funder_relationship(ORG, RFP, BMGF),
                         "Current/past grantee")

    def test_prefer8_portal_familiarity_moves_with_it(self):
        # _registered_on_portal shares the helper — a known funder implies familiarity.
        self.assertTrue(CD._registered_on_portal(ORG, RFP, BMGF))

    def test_the_plain_name_still_matches_without_a_donor_record(self):
        # Free-typed funders that are not in the catalog keep the raw-name fallback.
        rfp = {"funding_agency": "Unitaid"}
        self.assertTrue(CD._is_past_grantee(ORG, rfp, None))

    def test_an_unrelated_funder_is_still_not_a_grantee(self):
        other = {"donor": "Wellcome Trust", "canonical_key": "wellcome_trust"}
        self.assertFalse(CD._is_past_grantee(ORG, {"funding_agency": "Wellcome Trust"},
                                             other))
        self.assertEqual(
            CD.derive_funder_relationship(ORG, {"funding_agency": "Wellcome Trust"}, other),
            "None")

    def test_an_empty_history_is_never_a_grantee(self):
        self.assertFalse(CD._is_past_grantee({}, RFP, BMGF))
        self.assertFalse(CD._is_past_grantee({"org_funder_history": []}, RFP, BMGF))

    def test_no_donor_record_and_no_name_match_is_not_a_grantee(self):
        self.assertFalse(CD._is_past_grantee(ORG, {"funding_agency": "Some New Fund"}, None))


class LabelAndComponentsAgreeTests(unittest.TestCase):
    """The three call sites read one helper, so they cannot drift apart."""

    CASES = [
        (ORG, RFP, BMGF),
        (ORG, {"funding_agency": "Bill & Melinda Gates Foundation"}, BMGF),
        (ORG, {"funding_agency": "BMGF"}, BMGF),
        (ORG, {"funding_agency": "Wellcome Trust"},
         {"donor": "Wellcome Trust", "canonical_key": "wellcome_trust"}),
        ({}, RFP, BMGF),
    ]

    def test_the_grantee_component_and_the_label_never_disagree(self):
        for org, rfp, donor in self.CASES:
            comp = _by_key(CD._relationship_factors(org, rfp, donor))["rel_grantee"]
            label = CD.derive_funder_relationship(org, rfp, donor)
            self.assertEqual(comp["met"] is True, label == "Current/past grantee",
                             f"{rfp.get('funding_agency')} / history={bool(org)}")


class OrRatioTests(unittest.TestCase):
    """Mirror of the VIEW-mode ratio branch for an OR-criterion (views/review_rfp.py)."""

    @staticmethod
    def _ratio(factors, is_or):
        act = [f for f in factors if f.get("active", True)]
        if is_or and any(f.get("met") is True for f in act):
            return 1.0, 1
        meas = [f for f in act
                if f.get("score") is not None or f.get("met") is not None]
        num = sum(f["score"] if f.get("score") is not None else (1.0 if f["met"] else 0.0)
                  for f in meas)
        return num, len(meas)

    def test_one_satisfied_route_is_the_whole_criterion(self):
        facts = CD._relationship_factors(ORG, RFP, BMGF)
        self.assertEqual(self._ratio(facts, True), (1.0, 1))       # was 1/3 · 33%

    def test_an_unsatisfied_or_criterion_still_counts_every_active_route(self):
        # The denominator counts the ACTIVE tiers. "Donor already engaged" is a human
        # answer (action #10) and is excluded until someone gives one, so an unanswered
        # row has two active tiers, not three.
        other = {"donor": "Wellcome Trust", "canonical_key": "wellcome_trust"}
        rfp = {"funding_agency": "Wellcome Trust"}
        self.assertEqual(self._ratio(CD._relationship_factors(ORG, rfp, other), True),
                         (0.0, 2))
        answered = {**rfp, "donor_engaged": "no"}
        self.assertEqual(self._ratio(CD._relationship_factors(ORG, answered, other), True),
                         (0.0, 3))

    def test_a_non_or_criterion_is_untouched(self):
        # The AND-style mean still counts every route. Two are now met, not one: the
        # shared `_is_past_grantee` also makes PREFER-8's portal-familiarity route true
        # (a known funder implies familiarity with how they take submissions).
        facts = CD._relationship_factors(ORG, RFP, BMGF)
        self.assertEqual(self._ratio(facts, False), (2.0, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
