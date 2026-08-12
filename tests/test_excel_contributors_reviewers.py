"""The workbook split "Contributors/Reviewers" into two columns; the import must follow.

The app schema already held both `contributors` and `reviewers`, and the mapper already read
them, so this is mostly a REGRESSION GUARD: header names are the contract between a spreadsheet
somebody edits by hand and a database, and nothing else in the suite would notice if one drifted.
A silent failure here does not raise — it writes a blank column, and the loss is only visible
much later on a page that shows nobody as a reviewer.

One real fault was found and fixed while checking: the legacy combined header was consulted
BEFORE the new split one, so a half-renamed workbook (both headers present, which is exactly
what a partly-migrated sheet looks like) would take the mashed-together legacy value and
overwrite the split data with it.
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.migrate_excel import _norm_header, map_form1_row_by_header   # noqa: E402

_BASE_HEADERS = ["Form_ID", "Opportunity Title"]
_BASE_VALUES = ["F-001", "A health call"]


def _map(extra_headers, extra_values):
    headers = _BASE_HEADERS + list(extra_headers)
    values = _BASE_VALUES + list(extra_values)
    col_map = {_norm_header(h): i + 1 for i, h in enumerate(headers)}
    return map_form1_row_by_header(list(values), col_map)


class TheSplitColumnsAreImportedTests(unittest.TestCase):
    def test_contributors_and_reviewers_land_in_their_own_fields(self):
        rec = _map(["Contributors", "Reviewers"], ["Ada Nwosu; Bo Eze", "Cara Diallo"])
        self.assertEqual(rec["contributors"], ["Ada Nwosu", "Bo Eze"])
        self.assertEqual(rec["reviewers"], ["Cara Diallo"])

    def test_the_sheets_semicolon_separator_is_respected(self):
        # Excel multi-select uses ";", not ",", and a name can contain a comma.
        rec = _map(["Contributors"], ["Nwosu, Ada; Eze, Bo"])
        self.assertEqual(rec["contributors"], ["Nwosu, Ada", "Eze, Bo"])

    def test_header_case_and_spacing_do_not_matter(self):
        rec = _map(["  contributors ", "REVIEWERS"], ["Ada", "Cara"])
        self.assertEqual(rec["contributors"], ["Ada"])
        self.assertEqual(rec["reviewers"], ["Cara"])

    def test_a_singular_reviewer_header_is_accepted(self):
        rec = _map(["Reviewer"], ["Cara"])
        self.assertEqual(rec["reviewers"], ["Cara"])

    def test_blank_cells_import_as_nothing_rather_than_an_empty_name(self):
        rec = _map(["Contributors", "Reviewers"], ["", "   "])
        self.assertIsNone(rec["contributors"])
        self.assertIsNone(rec["reviewers"])

    def test_a_trailing_separator_does_not_create_a_blank_person(self):
        rec = _map(["Contributors"], ["Ada; Bo;"])
        self.assertEqual(rec["contributors"], ["Ada", "Bo"])


class TheSplitWinsOverTheLegacyColumnTests(unittest.TestCase):
    """A half-renamed workbook has BOTH headers. The split one is the current truth."""

    def test_a_half_renamed_sheet_uses_the_split_columns(self):
        rec = _map(["Contributors/Reviewers", "Contributors", "Reviewers"],
                   ["Ada; Bo; Cara", "Ada; Bo", "Cara"])
        self.assertEqual(rec["contributors"], ["Ada", "Bo"],
                         "the legacy combined column overwrote the split data")
        self.assertEqual(rec["reviewers"], ["Cara"])

    def test_a_legacy_only_sheet_still_imports(self):
        # An un-migrated workbook must not start failing.
        rec = _map(["Contributors/Reviewers"], ["Ada; Bo; Cara"])
        self.assertEqual(rec["contributors"], ["Ada", "Bo", "Cara"])
        self.assertIsNone(rec["reviewers"])


class NeitherColumnIsNotAFailureTests(unittest.TestCase):
    def test_a_sheet_without_either_column_still_imports_the_row(self):
        # Sync runs over the whole workbook; one absent optional column must not abort it.
        rec = _map([], [])
        self.assertIsNotNone(rec)
        self.assertIsNone(rec["contributors"])
        self.assertIsNone(rec["reviewers"])

    def test_both_fields_are_always_present_in_the_record(self):
        # They go straight into the upsert: a MISSING key writes nothing, which is fine, but the
        # keys must exist or a schema mismatch would surface as a sync error instead.
        rec = _map(["Contributors"], ["Ada"])
        self.assertIn("contributors", rec)
        self.assertIn("reviewers", rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
