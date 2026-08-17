"""A sync must not silently revert a decision somebody made in the app.

THE REPORTED CASE, twice. Two grants had been marked "Not Approved"; only one showed. The
one that reverted was a `source=migration` row whose `updated_at` lands inside an Excel sync
run - the workbook still said "Under Review", and the sync treated every non-blank cell as
authoritative:

    payload = {k: v for k, v in r.items() if v is not None}
    sb.table("rfp_submissions").update(payload).eq("uid", r["uid"]).execute()

The blank-preserving rule stopped the sheet NULLING a value, but not overwriting a newer
human judgement with a staler one. And nothing recorded that it had happened, which is why it
read as "it regressed, not sure what caused this" - a sync is not a moment anybody associates
with a decision changing.

The workbook still SEEDS these fields on a new row, and still owns everything factual about a
call (title, deadline, value, geography). It just stops rewriting the outcomes.

Run:  python -m unittest tests.test_excel_sync_protects_decisions
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

import importlib.util                                                  # noqa: E402

# The script is not an importable package module (scripts/ has no __init__), so load it by
# path. Importing it must not connect to anything - everything under __main__ is guarded.
_SPEC = importlib.util.spec_from_file_location(
    "migrate_excel_mod", os.path.join(_ROOT, "scripts", "migrate_excel.py"))
MX = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(MX)


class _Table:
    """Just enough of the query builder for the batch read the guard performs."""

    def __init__(self, rows):
        self.rows, self._cols, self._uids = rows, None, None

    def select(self, cols):
        self._cols = [c.strip() for c in cols.split(",")]
        return self

    def in_(self, _col, vals):
        self._uids = list(vals)
        return self

    def execute(self):
        data = [{c: r.get(c) for c in self._cols}
                for r in self.rows if r["uid"] in (self._uids or [])]
        return type("R", (), {"data": data})()


class _SB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _Table(self.rows)


class TheFieldsTheAppOwnsTests(unittest.TestCase):
    def test_the_funder_answer_is_one_of_them(self):
        # The reported field.
        self.assertIn("donor_decision", MX._APP_OWNED_FIELDS)

    def test_our_own_decision_and_its_paperwork_are_too(self):
        for f in ("decision", "decision_date", "decision_note"):
            self.assertIn(f, MX._APP_OWNED_FIELDS)

    def test_money_actually_received_is_protected(self):
        self.assertIn("amount_secured", MX._APP_OWNED_FIELDS)

    def test_facts_about_the_call_are_NOT_protected(self):
        # The workbook remains the place to correct these.
        for f in ("opportunity_title", "call_submission_deadline", "call_award_value",
                  "funding_agency", "call_geographic_scope"):
            self.assertNotIn(f, MX._APP_OWNED_FIELDS)


class WhatCountsAsNothingToProtectTests(unittest.TestCase):
    def test_blank_and_missing_values_are_not_decisions(self):
        for v in (None, "", "   ", "none", "NaN"):
            self.assertTrue(MX._blankish(v), repr(v))

    def test_not_submitted_is_the_absence_of_a_funder_answer(self):
        # It is the default every row carries, not somebody's judgement, so the sheet may
        # still fill it in.
        self.assertTrue(MX._blankish("Not submitted"))

    def test_a_real_decision_is_protected(self):
        for v in ("Not Approved", "Approved", "Under Review", "Proceed", 1500):
            self.assertFalse(MX._blankish(v), repr(v))


class TheBatchReadTests(unittest.TestCase):
    ROWS = [{"uid": "A", "donor_decision": "Not Approved", "decision": "Proceed",
             "decision_date": None, "decision_note": None, "progress_status": None,
             "amount_secured": None},
            {"uid": "B", "donor_decision": "Not submitted", "decision": None,
             "decision_date": None, "decision_note": None, "progress_status": None,
             "amount_secured": None}]

    def test_it_returns_stored_values_keyed_by_uid(self):
        got = MX._fetch_app_owned(_SB(self.ROWS), ["A", "B"])
        self.assertEqual(got["A"]["donor_decision"], "Not Approved")
        self.assertEqual(got["B"]["donor_decision"], "Not submitted")

    def test_a_read_failure_degrades_to_the_old_behaviour(self):
        # The guard must never be the reason a sync fails: no stored values means nothing
        # looks like a conflict, and the sheet applies exactly as it used to.
        class _Boom:
            def table(self, _n):
                raise RuntimeError("network")
        self.assertEqual(MX._fetch_app_owned(_Boom(), ["A"]), {})


class TheReportedRegressionTests(unittest.TestCase):
    """The decision the guard has to reach, expressed as the reported case."""

    def _keeps(self, stored, sheet_says, trust=False):
        """Mirror of the loop's decision for one field."""
        if trust:
            return False
        if MX._blankish(stored):
            return False
        return str(stored).strip() != str(sheet_says).strip()

    def test_a_not_approved_grant_is_not_reverted_to_under_review(self):
        self.assertTrue(self._keeps("Not Approved", "Under Review"))

    def test_agreement_is_not_reported_as_a_conflict(self):
        self.assertFalse(self._keeps("Not Approved", "Not Approved"))

    def test_the_sheet_still_fills_a_row_that_has_no_answer_yet(self):
        self.assertFalse(self._keeps("Not submitted", "Approved"))
        self.assertFalse(self._keeps(None, "Approved"))

    def test_the_flag_restores_the_old_behaviour(self):
        self.assertFalse(self._keeps("Not Approved", "Under Review", trust=True))


class TheOperatorIsToldTests(unittest.TestCase):
    def _src(self):
        import io
        with io.open(os.path.join(_ROOT, "scripts", "migrate_excel.py"),
                     encoding="utf-8") as fh:
            return fh.read()

    def test_it_prints_what_it_protected(self):
        # Silence is what turned this into a mystery twice over.
        src = self._src()
        self.assertIn("PROTECTED", src)
        self.assertIn("kept", src)
        self.assertIn("sheet says", src)

    def test_the_escape_hatch_is_offered_in_that_message(self):
        self.assertIn("--trust-sheet-decisions to let the sheet win", self._src())

    def test_the_flag_exists_and_defaults_off(self):
        src = self._src()
        self.assertIn('"--trust-sheet-decisions", action="store_true"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
