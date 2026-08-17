"""The Completed default is suggested on screen, not written behind the reviewer's back.

THE REPORTED CASE. A grant the owner had marked Not Approved in the workbook showed
"Under Review" in the app, with no event anywhere to point at. Tracing it: exactly ONE code
path in the repo stored that string - the RFP editor's invariant, which forced
donor_decision to "Under Review" whenever Progress was Completed and no answer was recorded.

The intent was sound: a Completed row with no donor decision leaves Tracking without entering
Applied Funding, which is how a grant went missing once. But writing it invisibly meant the
app stored a decision nobody had made, and read back weeks later that is indistinguishable
from data changing on its own.

Two things make it unnecessary as a stored value. The Grants page already DERIVES the same
bucket at read time, so the store was duplicating a computation. And what the gap really needs
is to be visible, not filled.

Run:  python -m unittest tests.test_donor_decision_visible_default
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.donor_decision import OUTCOMES, is_no_answer, is_outcome    # noqa: E402


class WhatCountsAsNoAnswerTests(unittest.TestCase):
    def test_the_default_every_row_carries_is_not_an_answer(self):
        # "Not submitted" is the shipped default, not a judgement, so a suggestion may
        # replace it and a spreadsheet may fill it.
        self.assertTrue(is_no_answer("Not submitted"))
        self.assertTrue(is_no_answer("not submitted"))

    def test_blanks_in_every_shape_count(self):
        # These reach the helper from DataFrames as often as from dicts.
        for v in (None, "", "   ", "none", "NaN", "NaT", "null"):
            self.assertTrue(is_no_answer(v), repr(v))

    def test_a_pending_state_IS_an_answer_for_protection_purposes(self):
        # Somebody or something put "Under Review" there deliberately; it must not be
        # treated as an empty slot that anything may overwrite.
        self.assertFalse(is_no_answer("Under Review"))

    def test_a_real_outcome_is_an_answer(self):
        self.assertFalse(is_no_answer("Not Approved"))
        self.assertFalse(is_no_answer("Approved"))


class WhatCountsAsDecidedTests(unittest.TestCase):
    def test_only_approved_and_not_approved_are_outcomes(self):
        self.assertEqual(OUTCOMES, ("Approved", "Not Approved"))
        self.assertTrue(is_outcome("Approved"))
        self.assertTrue(is_outcome("not approved"))

    def test_waiting_is_not_an_outcome(self):
        # A row carrying it belongs in Applied Funding but not in a win/loss count.
        self.assertFalse(is_outcome("Under Review"))
        self.assertFalse(is_outcome("Not submitted"))
        self.assertFalse(is_outcome(None))


class TheEditorSuggestsRatherThanWritesTests(unittest.TestCase):
    def _src(self, rel):
        import io
        with io.open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
            return fh.read()

    def test_the_silent_write_is_gone(self):
        src = self._src("views/rfp_editor.py")
        self.assertNotIn('update["donor_decision"] = "Under Review"', src)

    def test_the_value_is_offered_in_the_widget_instead(self):
        src = self._src("views/rfp_editor.py")
        self.assertIn("_dd_suggested", src)
        self.assertIn('"Under Review" if _dd_suggested else _dd_stored', src)

    def test_the_suggestion_is_explained_on_screen(self):
        # Otherwise a value appears in the field with no reason given, which is the same
        # problem one step earlier.
        src = self._src("views/rfp_editor.py")
        self.assertIn("Suggested because Progress is", src)
        self.assertIn("Nothing is saved until you", src)

    def test_it_only_suggests_when_progress_is_completed(self):
        src = self._src("views/rfp_editor.py")
        i = src.index("_dd_suggested = ")
        self.assertIn('completed', src[i:i + 200])

    def test_the_gap_is_named_on_save_rather_than_filled(self):
        src = self._src("views/rfp_editor.py")
        self.assertIn("_saving_completed", src)
        self.assertIn("will not appear in Applied Funding", src)

    def test_every_call_site_shares_one_definition(self):
        # Three files used to carry their own copy, and they have to agree: one suggests a
        # value, one displays a derived one, one decides whether a spreadsheet may overwrite.
        self.assertIn("from core.donor_decision import is_no_answer",
                      self._src("views/rfp_editor.py"))
        self.assertIn("from core.donor_decision import is_no_answer",
                      self._src("scripts/migrate_excel.py"))


class TheDerivedDisplayStillCoversTheGapTests(unittest.TestCase):
    """Removing the stored value must not lose the visibility it was there for."""

    def test_grants_still_derives_the_pending_bucket_at_read_time(self):
        import io
        with io.open(os.path.join(_ROOT, "app_pages", "grants.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('_status_display', src)
        self.assertIn('"Under Review"', src)
        # Derived onto a display column, never written back to the row.
        self.assertNotIn('update({"donor_decision"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
