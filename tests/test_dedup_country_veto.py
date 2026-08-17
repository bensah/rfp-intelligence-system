"""Two calls that name different countries are not the same call.

THE REPORTED CASE. A pipeline row titled "Modern Slavery Fund Viet Nam Programme 2026 to
2029" carried a description about ALBANIA - Albanian nationals, Albanian victims, Albanian
lots - and an award value of GBP 3.24M. It was reported as an LLM hallucination.

It was not. The global store holds two correct, separately-sourced rows, one per country
programme, each faithfully summarised from its own page. The corruption happened later, in
the duplicate merge: the two titles are one boilerplate with the country swapped, so they
score 91% character similarity and cleared the 0.90 threshold. `find_duplicates` declared
them the same call and the merge gap-filled the Viet Nam row's blank brief and blank award
value from the Albania candidate, leaving the title and link as Viet Nam.

Proved on the live row: the pipeline brief is BYTE-IDENTICAL (sha bd3d76f4..., 1000 chars)
to the Albania store row's brief, while the Viet Nam store row's own correct brief (199
chars) went unused; the award value 3.24M is Albania's, not Viet Nam's 2.97M.

A merge is not a tie-break. It keeps one row's identity and fills the rest from the other,
so a wrong match does not leave a duplicate for a human to dismiss - it produces a single
row that is confidently wrong. Hence a veto rather than a tuned threshold.

Run:  python -m unittest tests.test_dedup_country_veto
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import deduplicator as D                                    # noqa: E402

ALBANIA = "Modern Slavery Fund Albania Programme 2026 to 2029: call for proposals"
VIETNAM = "Modern Slavery Fund Viet Nam Programme 2026 to 2029: call for proposals"


def _row(uid, title, **kw):
    row = {"uid": uid, "opportunity_title": title, "opportunity_link": None,
           "funding_agency": "Government of the United Kingdom",
           "call_submission_deadline": "2029-03-31", "call_award_value": None}
    row.update(kw)
    return row


class TheSiblingProgrammeCaseTests(unittest.TestCase):
    def test_the_two_titles_really_do_clear_the_similarity_threshold(self):
        # Without this the veto would be untested: the whole point is that the numeric
        # rule says "duplicate" and must be overruled.
        sim = D._title_similarity(D._norm_title(ALBANIA), D._norm_title(VIETNAM))
        self.assertGreaterEqual(sim, D.TITLE_THRESHOLD)

    def test_they_are_no_longer_treated_as_the_same_call(self):
        cand = _row("AS-new", ALBANIA)
        self.assertEqual(D.find_duplicates(cand, [_row("AS-260814-0748266", VIETNAM)]), [])

    def test_the_veto_holds_in_the_other_direction_too(self):
        cand = _row("AS-new", VIETNAM)
        self.assertEqual(D.find_duplicates(cand, [_row("AS-old", ALBANIA)]), [])


class TheProcurementNoticeCaseTests(unittest.TestCase):
    # Same funder, same deadline, three shared distinctive tokens: rule 4, not rule 3.
    CABO = ("IFB - Cabo Verde - Supply, Installation of Facilities Equipments and "
            "Training - Technology Park Phase II")
    ZIM = ("SPN - Zimbabwe - Supply, Delivery and Installation of Data Loggers, Sensors "
           "and Various ICT Equipment")

    def test_rule_4_can_no_longer_collapse_two_countries_notices(self):
        cand = _row("AS-new", self.ZIM, funding_agency="African Development Bank",
                    call_submission_deadline="2026-09-15")
        existing = [_row("AS-260814-074891", self.CABO,
                         funding_agency="African Development Bank",
                         call_submission_deadline="2026-09-15")]
        self.assertEqual(D.find_duplicates(cand, existing), [])


class WhatMustStillDeduplicateTests(unittest.TestCase):
    def test_an_identical_link_still_wins_over_a_country_in_the_title(self):
        # Dispositive evidence of the same call. Two country names in one title, or a
        # title edited between scans, must not defeat an exact URL match.
        link = "https://example.org/calls/abc"
        cand = _row("AS-new", "Health call - Mali", opportunity_link=link)
        existing = [_row("AS-old", "Health call - Kenya", opportunity_link=link)]
        self.assertEqual(len(D.find_duplicates(cand, existing)), 1)

    def test_an_identical_opportunity_id_still_wins(self):
        cand = _row("AS-new", "Health call - Mali", opportunity_id="RFP-77")
        existing = [_row("AS-old", "Health call - Kenya", opportunity_id="RFP-77")]
        self.assertEqual(len(D.find_duplicates(cand, existing)), 1)

    def test_the_same_call_reworded_still_deduplicates(self):
        # No country on either side: the veto must not fire and rule 3 should still work.
        a = "9th African Call for proposals on malaria drug discovery"
        b = "Malaria drug discovery: 9th African call for proposals"
        cand = _row("AS-new", a, funding_agency="MMV",
                    call_submission_deadline="2026-08-29")
        existing = [_row("AS-old", b, funding_agency="MMV",
                         call_submission_deadline="2026-08-29")]
        self.assertEqual(len(D.find_duplicates(cand, existing)), 1)

    def test_one_side_naming_a_country_does_not_veto(self):
        # A general call and a country-specific one may still be the same call reworded,
        # so only a DISAGREEMENT between two named countries vetoes.
        cand = _row("AS-new", "Sickle cell disease call for projects - Mali",
                    funding_agency="F", call_submission_deadline="2026-12-01")
        existing = [_row("AS-old", "Sickle cell disease call for projects",
                         funding_agency="F", call_submission_deadline="2026-12-01")]
        self.assertEqual(len(D.find_duplicates(cand, existing)), 1)

    def test_the_same_country_on_both_sides_does_not_veto(self):
        cand = _row("AS-new", "Health systems call - Mali", funding_agency="F",
                    call_submission_deadline="2026-12-01")
        existing = [_row("AS-old", "Mali health systems call", funding_agency="F",
                         call_submission_deadline="2026-12-01")]
        self.assertEqual(len(D.find_duplicates(cand, existing)), 1)


class TheCountryReaderTests(unittest.TestCase):
    def test_a_region_or_tier_is_not_a_country(self):
        # Regions and income tiers are qualifiers two identical calls may word
        # differently, so they must never veto.
        self.assertEqual(D._title_countries("Call for Sub-Saharan Africa and LMICs"),
                         set())

    def test_a_two_word_country_is_read_and_canonicalised(self):
        # "Viet Nam" and "Vietnam" must land on one key, or the two spellings of the same
        # country would look disjoint and veto a genuine duplicate.
        self.assertEqual(D._title_countries(VIETNAM), {"vietnam"})
        self.assertEqual(D._title_countries("Modern Slavery Fund Vietnam Programme"),
                         {"vietnam"})

    def test_disjoint_sets_are_required(self):
        self.assertTrue(D._different_countries("Call - Mali", "Call - Kenya"))
        self.assertFalse(D._different_countries("Call - Mali", "Call - Mali extension"))
        self.assertFalse(D._different_countries("Generic call", "Call - Kenya"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
