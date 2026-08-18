"""One donor, one identity — whatever dash the funder's name was typed with.

The report drew `BMGF - Gates Foundation` (5 calls) beside `BMGF – Gates Foundation`
(4 calls, EN DASH) as two funders. Word and Excel autocorrect the hyphen the moment a cell
is edited, so both spellings are in the data and neither is wrong; only grouping on the
literal string is.

These tests hold three things: the STORED form repairs the dash family and nothing else
(it is shown to people, so it must not rewrite their words), the IDENTITY form collapses
every spelling of one donor onto one key, and that key agrees with the one
`core.donor_intel` already matches donors on — two normalisers disagreeing about who a
funder is would be worse than one bug.

Run:  python -m unittest tests.test_funder_names
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.funder_names import (canonical_funder, dominant_spelling,  # noqa: E402
                               funder_key, group_by_funder)

HYPHEN = "BMGF - Gates Foundation"
EN_DASH = "BMGF – Gates Foundation"
EM_DASH = "BMGF — Gates Foundation"
NB_HYPHEN = "BMGF ‑ Gates Foundation"
MINUS = "BMGF − Gates Foundation"


class CanonicalFormTests(unittest.TestCase):
    def test_every_dash_variant_stores_as_one_spelling(self):
        for variant in (EN_DASH, EM_DASH, NB_HYPHEN, MINUS, "BMGF ‒ Gates Foundation"):
            self.assertEqual(canonical_funder(variant), HYPHEN, variant)

    def test_whitespace_is_tidied_including_non_breaking_spaces(self):
        self.assertEqual(canonical_funder("  BMGF  -   Gates  Foundation "), HYPHEN)

    def test_an_invisible_soft_hyphen_disappears_rather_than_becoming_a_dash(self):
        self.assertEqual(canonical_funder("Well­come Trust"), "Wellcome Trust")

    def test_the_words_themselves_are_never_rewritten(self):
        # Case, punctuation and the organisation's own wording are theirs, not ours.
        for name in ("Gavi, the Vaccine Alliance", "UK Foreign, Commonwealth & Development",
                     "BMGF - Gates Foundation", "unicef"):
            self.assertEqual(canonical_funder(name), name)

    def test_canonicalising_is_idempotent(self):
        once = canonical_funder(EN_DASH)
        self.assertEqual(canonical_funder(once), once)

    def test_blank_input_is_blank_not_an_error(self):
        self.assertEqual(canonical_funder(None), "")
        self.assertEqual(canonical_funder("   "), "")


class IdentityKeyTests(unittest.TestCase):
    def test_all_spellings_of_one_donor_share_a_key(self):
        keys = {funder_key(v) for v in (HYPHEN, EN_DASH, EM_DASH, MINUS,
                                        "bmgf-gates foundation", "BMGF  –  Gates Foundation")}
        self.assertEqual(len(keys), 1)

    def test_different_donors_keep_different_keys(self):
        # The pairs that must NOT merge: an organisation and its philanthropic arm, and
        # two agencies sharing a prefix.
        self.assertNotEqual(funder_key("Wellcome"), funder_key("Wellcome Trust"))
        self.assertNotEqual(funder_key("UN - UNICEF"), funder_key("UN - UNOPS"))
        self.assertNotEqual(funder_key("Gates Foundation"), funder_key("Gates Ventures"))

    def test_punctuation_between_words_is_irrelevant_to_identity(self):
        self.assertEqual(funder_key("Gavi, the Vaccine Alliance"),
                         funder_key("Gavi the Vaccine Alliance"))
        self.assertEqual(funder_key("UN-UNICEF"), funder_key("UN — UNICEF"))

    def test_the_rule_stays_as_literal_as_the_matcher(self):
        # Accent and &/and variants are NOT folded, because donor_intel._norm does not fold
        # them either and the two must agree. Documented, not accidental: widening it here
        # alone would split the chart from the donor matcher.
        self.assertNotEqual(funder_key("Agence Française"), funder_key("Agence Francaise"))
        self.assertNotEqual(funder_key("Bill & Melinda"), funder_key("Bill and Melinda"))

    def test_key_agrees_with_the_donor_matcher(self):
        # core.donor_intel._norm builds the key donors are MATCHED on. If these two ever
        # disagree, the chart and the matcher would hold different opinions about who a
        # funder is — a subtler bug than the one this module fixes.
        from core.donor_intel import _norm
        for name in (HYPHEN, EN_DASH, "Gavi, the Vaccine Alliance", "WHO - World Health",
                     "UK Foreign, Commonwealth & Development Office"):
            self.assertEqual(funder_key(name), _norm(canonical_funder(name)), name)


class GroupingTests(unittest.TestCase):
    def test_the_majority_spelling_is_the_label(self):
        self.assertEqual(dominant_spelling([HYPHEN, HYPHEN, HYPHEN, EN_DASH, EN_DASH]),
                         HYPHEN)

    def test_a_tie_is_broken_deterministically(self):
        # Both spellings canonicalise to the same string here, so the label is stable
        # whichever order the rows arrive in — a chart label must not flicker.
        first = dominant_spelling([EN_DASH, HYPHEN])
        second = dominant_spelling([HYPHEN, EN_DASH])
        self.assertEqual(first, second)

    def test_grouping_reports_the_whole_picture(self):
        rows = [HYPHEN] * 5 + [EN_DASH] * 4 + ["GiveWell"] * 2 + ["", None]
        groups = group_by_funder(rows)
        self.assertEqual(len(groups), 2)                    # blanks are not a funder
        gates = groups[funder_key(HYPHEN)]
        self.assertEqual(gates["count"], 9)                 # the real number, not 5 and 4
        self.assertEqual(gates["label"], HYPHEN)
        self.assertEqual(len(gates["variants"]), 2)

    def test_a_consistently_spelled_donor_is_not_reported_as_split(self):
        groups = group_by_funder(["UN — UNICEF"] * 7)
        entry = next(iter(groups.values()))
        self.assertEqual(len(entry["variants"]), 1)         # nothing to reconcile
        self.assertEqual(entry["label"], "UN - UNICEF")     # but stored form is canonical


class WritePathTests(unittest.TestCase):
    """The value is canonicalised where it enters the database, not only where it is read —
    otherwise every new scan re-creates the split the backfill just repaired."""

    def _src(self, relative):
        with open(os.path.join(_ROOT, relative), encoding="utf-8") as fh:
            return fh.read()

    def test_every_writer_canonicalises(self):
        for path in ("core/scan_pipeline.py", "core/found_loader.py",
                     "scripts/migrate_excel.py"):
            self.assertIn("canonical_funder", self._src(path), path)

    def test_the_chart_groups_by_identity(self):
        src = self._src("views/report.py")
        self.assertIn("funder_key", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
