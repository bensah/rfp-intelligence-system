"""`opportunity_type` and `instrument_type` are two axes, not two guesses at one answer.

A reviewer opened a call reading **Instrument: Contract** above **Opportunity type: grant**
and reasonably read it as the extraction contradicting itself. It is not: the two are
separated by the moment of award —

    opportunity_type   what pursuing this IS, BEFORE you win it (the pursuit class the
                       eligibility gate opts out of)
    instrument_type    the vehicle IF you win (what the relationship becomes)

so a grant that is contracted after award is ordinary. 30 of 686 live rows are exactly that,
and none is an error.

The thing being protected here is the SIGNAL. Flagging those 30 as conflicts would teach a
reviewer to ignore the flag on the 7 rows that genuinely do not add up — a procurement
issuing a grant, a procurement issuing equity. Measured over the whole catalogue:
623 consistent · 55 unclassified · 7 unusual · 1 with neither axis.
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import award_type as AT             # noqa: E402


class TheVocabularyIsCanonicalisedFirstTests(unittest.TestCase):
    """The column was written by several code paths over time: "Grant/funding call" on 325
    rows, bare "grant" on 23, "Announcement" on 11 and "announcement" on 44, "award" on 7.
    No pairing rule is possible until the same fact has one spelling."""

    def test_the_spellings_of_a_funding_call_collapse_to_one(self):
        for raw in ("Grant/funding call", "grant", "Grant", "grants", "funding call",
                    "Funding Call", "call for proposals"):
            with self.subTest(raw=raw):
                self.assertEqual(AT.canon_opportunity(raw), "Grant/funding call")

    def test_procurement_and_tender_are_the_same_pursuit_class(self):
        self.assertEqual(AT.canon_opportunity("tender"), "Procurement")
        self.assertEqual(AT.canon_opportunity("PROCUREMENT"), "Procurement")

    def test_an_award_is_a_prize_pursuit(self):
        self.assertEqual(AT.canon_opportunity("award"), "Prize/Challenge")

    def test_instrument_spellings_collapse_too(self):
        self.assertEqual(AT.canon_instrument("grant agreement"), "Grant")
        self.assertEqual(AT.canon_instrument("co-operative agreement"),
                         "Cooperative Agreement")
        self.assertEqual(AT.canon_instrument("Award"), "Prize/Award")
        self.assertEqual(AT.canon_instrument("technical assistance"), "In-kind/TA")

    def test_an_unrecognised_value_is_not_forced_into_the_vocabulary(self):
        for raw in ("", "   ", None, "banana"):
            with self.subTest(raw=raw):
                self.assertIsNone(AT.canon_opportunity(raw))
                self.assertIsNone(AT.canon_instrument(raw))


class AGrantContractedAfterAwardIsNotAConflictTests(unittest.TestCase):
    """The case that started this. A grant is contracted once awarded, so the pair is
    ordinary and must not be flagged — 30 live rows depend on it."""

    def test_a_grant_call_awarded_as_a_contract_is_consistent(self):
        verdict, why = AT.coherence("Grant/funding call", "Contract")
        self.assertEqual(verdict, "consistent")
        self.assertIn("normally awarded", why)

    def test_the_lower_case_variant_behaves_identically(self):
        # 21 of the 30 rows store the pursuit class as bare "grant".
        self.assertEqual(AT.coherence("grant", "Contract")[0], "consistent")

    def test_it_reads_as_one_sentence_rather_than_two_labels(self):
        self.assertEqual(AT.pairing("grant", "Contract")["text"],
                         "Grant/funding call, awarded as a contract")

    def test_no_warning_is_raised_on_it(self):
        self.assertEqual(AT.pairing("grant", "Contract")["note"], "")

    def test_a_federal_grant_awarded_as_a_cooperative_agreement_is_consistent(self):
        # 103 live rows — the commonest US federal shape.
        self.assertEqual(AT.coherence("Grant/funding call", "Cooperative Agreement")[0],
                         "consistent")


class OnlyTheGenuinelyOddPairsAreFlaggedTests(unittest.TestCase):
    """7 of 686 rows. Crying wolf on the other 679 is the failure mode being avoided."""

    def test_a_procurement_issuing_a_grant_is_unusual(self):
        verdict, why = AT.coherence("Procurement", "Grant")
        self.assertEqual(verdict, "unusual")
        self.assertIn("likely misread", why)

    def test_a_procurement_issuing_equity_is_unusual(self):
        self.assertEqual(AT.coherence("Procurement", "Equity/Investment")[0], "unusual")

    def test_a_grant_call_issuing_equity_is_unusual(self):
        self.assertEqual(AT.coherence("Grant/funding call", "Equity/Investment")[0],
                         "unusual")

    def test_an_unusual_pair_carries_a_note_a_reviewer_can_act_on(self):
        note = AT.pairing("Procurement", "Grant")["note"]
        self.assertTrue(note.startswith("A procurement"))
        self.assertIn("one of the two is likely misread", note)

    def test_the_ordinary_pairs_carry_no_note(self):
        for opp, inst in (("Grant/funding call", "Grant"), ("Procurement", "Contract"),
                          ("Consultancy", "Contract"), ("Loan", "Loan"),
                          ("Prize/Challenge", "Prize/Award"),
                          ("Grant/funding call", "Fellowship"),
                          ("Grant/funding call", "Scholarship")):
            with self.subTest(pair=(opp, inst)):
                self.assertEqual(AT.pairing(opp, inst)["note"], "")


class AnUnclassifiedValueIsNeverJudgedTests(unittest.TestCase):
    """"Announcement" means the classifier could not tell — it asserts no pursuit class.
    55 live rows are announcements; a pairing rule must not evaluate a value that was never
    asserted, or every one of them becomes a false conflict."""

    def test_an_announcement_is_unclassified_whatever_the_instrument(self):
        for inst in ("Contract", "Grant", "Loan"):
            with self.subTest(instrument=inst):
                self.assertEqual(AT.coherence("Announcement", inst)[0], "unclassified")

    def test_the_lower_case_variant_too(self):
        self.assertEqual(AT.coherence("announcement", "Contract")[0], "unclassified")

    def test_an_unclassified_row_raises_no_warning(self):
        self.assertEqual(AT.pairing("announcement", "Contract")["note"], "")

    def test_it_still_shows_the_instrument_it_does_know(self):
        self.assertEqual(AT.pairing("announcement", "Contract")["text"],
                         "Announcement, awarded as a contract")

    def test_nothing_is_inferred_from_an_unclassified_value(self):
        _o, inst, _oi, _ii = AT.complement("Announcement", None)
        self.assertIsNone(inst)


class AMissingAxisIsFilledFromTheOtherTests(unittest.TestCase):
    """They imply each other, so one present half answers for the other: 187 live rows,
    148 of them a Procurement with no instrument recorded."""

    def test_a_procurement_implies_a_contract(self):
        opp, inst, opp_inf, inst_inf = AT.complement("Procurement", None)
        self.assertEqual((opp, inst), ("Procurement", "Contract"))
        self.assertTrue(inst_inf)
        self.assertFalse(opp_inf)

    def test_a_contract_implies_a_procurement(self):
        opp, inst, opp_inf, _ = AT.complement(None, "Contract")
        self.assertEqual((opp, inst), ("Procurement", "Contract"))
        self.assertTrue(opp_inf)

    def test_a_cooperative_agreement_implies_a_funding_call(self):
        self.assertEqual(AT.complement(None, "Cooperative Agreement")[0],
                         "Grant/funding call")

    def test_a_stated_value_is_never_overwritten_by_an_inference(self):
        opp, inst, opp_inf, inst_inf = AT.complement("Grant/funding call", "Contract")
        self.assertEqual((opp, inst), ("Grant/funding call", "Contract"))
        self.assertFalse(opp_inf or inst_inf)

    def test_an_inferred_value_is_labelled_as_inferred_on_the_page(self):
        # A derived value must not be presented as one the funder published.
        self.assertIn("inferred", AT.pairing("Procurement", None)["text"])
        self.assertEqual(AT.pairing("Procurement", None)["inferred"], ["instrument_type"])

    def test_neither_axis_yields_nothing_rather_than_a_guess(self):
        p = AT.pairing(None, None)
        self.assertEqual(p["text"], "")
        self.assertEqual(p["verdict"], "unknown")
        self.assertEqual(p["note"], "")


class TheWholeLiveDistributionIsAccountedForTests(unittest.TestCase):
    """Every (opportunity_type, instrument_type) pair present in the catalogue, with the
    verdict each must get. This is the table that decides whether the flag means anything."""

    CASES = (
        # (opportunity_type, instrument_type, rows, verdict)
        ("Grant/funding call", "Grant", 176, "consistent"),
        ("Procurement", "", 148, "consistent"),                 # instrument inferred
        ("Grant/funding call", "Cooperative Agreement", 103, "consistent"),
        ("Procurement", "Contract", 100, "consistent"),
        ("announcement", "Contract", 44, "unclassified"),
        ("Grant/funding call", "", 30, "consistent"),           # instrument inferred
        ("grant", "Contract", 21, "consistent"),                # THE reported case
        ("Announcement", "Grant", 10, "unclassified"),
        ("Grant/funding call", "Contract", 9, "consistent"),
        ("award", "Award", 7, "consistent"),
        ("Grant/funding call", "Prize/Award", 7, "consistent"),
        ("", "Contract", 6, "consistent"),                      # pursuit class inferred
        ("Procurement", "Grant", 4, "unusual"),
        ("Grant/funding call", "Fellowship", 4, "consistent"),
        ("Grant/funding call", "Equity/Investment", 2, "unusual"),
        ("Prize/Challenge", "Grant", 2, "consistent"),
        ("Prize/Challenge", "Award", 2, "consistent"),
        ("Prize/Challenge", "Prize/Award", 2, "consistent"),
        ("Prize/Challenge", "", 2, "consistent"),
        ("grant", "Grant", 2, "consistent"),
        ("Announcement", "Contract", 1, "unclassified"),
        ("Consultancy", "", 1, "consistent"),
        ("Grant/funding call", "Scholarship", 1, "consistent"),
        ("Procurement", "Equity/Investment", 1, "unusual"),
        ("", "", 1, "unknown"),
    )

    def test_the_table_covers_the_whole_catalogue(self):
        # Guards the table itself: if it drifts from the corpus the percentages below stop
        # meaning anything, and a missing pair is exactly how an unusual one hides.
        self.assertEqual(sum(n for _o, _i, n, _v in self.CASES), 686)

    def test_every_live_pair_gets_the_intended_verdict(self):
        for opp, inst, rows, want in self.CASES:
            with self.subTest(pair=(opp, inst), rows=rows):
                self.assertEqual(AT.pairing(opp, inst)["verdict"], want)

    def test_the_flagged_share_stays_small(self):
        # The signal only survives while it is rare. 7 of 686 rows.
        flagged = sum(n for o, i, n, _v in self.CASES
                      if AT.pairing(o, i)["verdict"] == "unusual")
        total = sum(n for _o, _i, n, _v in self.CASES)
        self.assertEqual(flagged, 7)
        self.assertLess(flagged / total, 0.02)

    def test_the_great_majority_reconcile_without_comment(self):
        ok = sum(n for o, i, n, _v in self.CASES
                 if AT.pairing(o, i)["verdict"] == "consistent")
        self.assertEqual(ok, 623)

    def test_every_pair_produces_a_display_line_unless_both_are_absent(self):
        for opp, inst, _rows, _want in self.CASES:
            with self.subTest(pair=(opp, inst)):
                text = AT.pairing(opp, inst)["text"]
                self.assertEqual(bool(text), bool(opp or inst))


class ThePageShowsOneRowNotTwoTests(unittest.TestCase):
    def test_the_card_carries_a_single_reconciled_row(self):
        from core import opportunity_detail as od
        view = {"opportunity_name": "A Call", "opportunity_type": "grant",
                "instrument_type": "Contract"}
        flat = {lb: v for _t, rows in od.sections(view) for lb, v in rows}
        self.assertEqual(flat["Award type"], "Grant/funding call, awarded as a contract")
        # The two raw labels are gone — they were the thing that looked like a disagreement.
        self.assertNotIn("Instrument", flat)
        self.assertNotIn("Opportunity type", flat)

    def test_the_page_helper_exposes_the_note(self):
        from core import opportunity_detail as od
        p = od.award_pairing({"opportunity_type": "Procurement", "instrument_type": "Grant"})
        self.assertEqual(p["verdict"], "unusual")
        self.assertTrue(p["note"])

    def test_a_row_with_neither_axis_drops_the_row_entirely(self):
        from core import opportunity_detail as od
        flat = {lb: v for _t, rows in od.sections({"opportunity_name": "A Call"})
                for lb, v in rows}
        self.assertNotIn("Award type", flat)


if __name__ == "__main__":
    unittest.main(verbosity=2)
