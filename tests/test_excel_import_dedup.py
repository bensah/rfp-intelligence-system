"""The Excel import never consulted the deduplicator.

Identity on that path was the uid (Form_ID) and nothing else, so a workbook row
given a new Form_ID for a call ALREADY in the pipeline inserted a second copy -
and the matcher that would have caught it was never asked. Neither
`scripts/migrate_excel.py` nor `core/excel_sync.py` referenced
`core.deduplicator` at all.

Measured on the live database: `find_duplicates` flags fifteen duplicate pairs
sitting in one tenant, six of them with byte-identical links, and TWELVE of the
fifteen arrived on the import path (`source='migration'`). So the matcher works;
it simply was not wired in.

Only the two DISPOSITIVE rules are applied here - an identical link or an
identical opportunity_id. Title similarity is deliberately excluded: it is what
merged sibling programmes and corrupted the surviving row, and an import is the
wrong place to take that risk.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.migrate_excel import drop_rows_already_in_the_pipeline as drop


def _row(uid, link=None, oppid=None, title="A call"):
    return {"uid": uid, "opportunity_link": link, "opportunity_id": oppid,
            "opportunity_title": title}


class IdenticalLink(unittest.TestCase):
    def test_a_workbook_row_repeating_a_live_link_is_skipped(self):
        existing = [_row("LA-260616-2115",
                         "https://www.thepandemicfund.org/call-for-proposals")]
        new = [_row("SH-260504-0525",
                    "https://www.thepandemicfund.org/call-for-proposals",
                    title="4th Call for Proposals")]
        keep, dups = drop(new, existing)
        self.assertEqual(keep, [])
        self.assertEqual(len(dups), 1)
        self.assertIn("LA-260616-2115", dups[0])
        self.assertIn("SH-260504-0525", dups[0])

    def test_the_real_pairs_from_the_live_database(self):
        """Every identical-link pair actually sitting in the pipeline."""
        pairs = (
            ("OR-260825-1423", "OR-260612-1645",
             "https://www.catalyticopportunityfund.org/h-iud"),
            ("SH-260504-0525", "LA-260616-2115",
             "https://www.thepandemicfund.org/call-for-proposals"),
            ("TA-260207-1255", "MA-260223-0615",
             "https://unitaid.org/call-for-proposal/accelerating-cervical-cancer"),
            ("TA-260207-1255", "BE-260202-1220",
             "https://unitaid.org/call-for-proposal/accelerating-cervical-cancer"),
            ("AS-260625-133009", "BE-260831-1152",
             "https://coefficientgiving.org/funds/global-health-wellbeing/rfp"),
            ("SH-260213-1634", "AB-260215-1624",
             "https://solve.mit.edu/challenges/future-health-challenge"),
        )
        for first, second, link in pairs:
            with self.subTest(pair=f"{first}/{second}"):
                keep, dups = drop([_row(second, link)], [_row(first, link)])
                self.assertEqual(keep, [], f"{second} would still be inserted")
                self.assertEqual(len(dups), 1)

    def test_url_normalisation_is_the_deduplicators_own(self):
        # Trailing slash, scheme and www differences are the same call.
        existing = [_row("A", "https://www.example.org/call-for-proposals")]
        for variant in ("http://example.org/call-for-proposals",
                        "https://example.org/call-for-proposals/",
                        "https://www.example.org/call-for-proposals?utm_source=x"):
            with self.subTest(variant=variant):
                keep, _ = drop([_row("B", variant)], existing)
                self.assertEqual(keep, [], variant)


class IdenticalOpportunityId(unittest.TestCase):
    def test_the_same_donor_reference_is_the_same_call(self):
        keep, dups = drop([_row("NEW-1", "https://other.example/x", oppid="RFP-2026-014")],
                          [_row("OLD-1", "https://donor.example/y", oppid="RFP-2026-014")])
        self.assertEqual(keep, [])
        self.assertIn("OLD-1", dups[0])

    def test_a_blank_opportunity_id_matches_nothing(self):
        existing = [_row("OLD-1", "https://a.example/1", oppid=None),
                    _row("OLD-2", "https://a.example/2", oppid="")]
        keep, dups = drop([_row("NEW-1", "https://b.example/3", oppid=None)], existing)
        self.assertEqual(len(keep), 1, "a missing id must not collapse unrelated rows")
        self.assertEqual(dups, [])


class GenuinelyNewRowsStillImport(unittest.TestCase):
    """The direction that would cost the owner data from their own workbook."""

    def test_a_new_call_is_kept(self):
        keep, dups = drop([_row("NEW-1", "https://donor.example/new-call")],
                          [_row("OLD-1", "https://donor.example/old-call")])
        self.assertEqual(len(keep), 1)
        self.assertEqual(dups, [])

    def test_a_row_with_no_link_and_no_id_is_kept(self):
        # Manually entered workbook rows often have neither. They must import.
        keep, dups = drop([_row("NEW-1", None, None)],
                          [_row("OLD-1", "https://donor.example/x", "RFP-1")])
        self.assertEqual(len(keep), 1)
        self.assertEqual(dups, [])

    def test_similar_titles_alone_never_suppress(self):
        # Sibling programmes under one call: this is the merge that corrupted a
        # surviving row, and it must NOT happen on the import path.
        existing = [_row("OLD-1", "https://donor.example/climate-health-a",
                         title="Global collaboration action on climate and health")]
        new = [_row("NEW-1", "https://donor.example/climate-health-b",
                    title="Global collaboration action on climate and health")]
        keep, dups = drop(new, existing)
        self.assertEqual(len(keep), 1, "identical titles are not proof of one call")
        self.assertEqual(dups, [])

    def test_an_empty_pipeline_imports_everything(self):
        rows = [_row("A", "https://x.example/1"), _row("B", "https://x.example/2")]
        keep, dups = drop(rows, [])
        self.assertEqual(len(keep), 2)
        self.assertEqual(dups, [])


class Robustness(unittest.TestCase):
    def test_none_inputs(self):
        self.assertEqual(drop(None, None), ([], []))
        self.assertEqual(drop([], None), ([], []))

    def test_two_workbook_rows_sharing_a_link_both_import(self):
        # Within-batch collision is a different problem (the workbook contradicting
        # itself) and this function is about the pipeline it is importing INTO.
        # Documented rather than silently half-handled.
        rows = [_row("A", "https://x.example/same"), _row("B", "https://x.example/same")]
        keep, dups = drop(rows, [])
        self.assertEqual(len(keep), 2)
        self.assertEqual(dups, [])

    def test_the_report_line_names_both_rows_and_the_title(self):
        _, dups = drop([_row("NEW-1", "https://x.example/1", title="Some Call Title")],
                       [_row("OLD-1", "https://x.example/1")])
        self.assertIn("NEW-1", dups[0])
        self.assertIn("OLD-1", dups[0])
        self.assertIn("Some Call Title", dups[0])


if __name__ == "__main__":
    unittest.main()
