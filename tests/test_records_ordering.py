"""Records — All RFPs lists newest-ADDED first, not newest-last-seen.

THE REPORTED CASE. "Newer RFPs end up lower instead of at the top."

`_fetch_all` already asks the DB for the right order — created_at, submitted_at, uid, all
descending — and the page then re-sorted the DataFrame by `search_date`, throwing that
away. The comment on the old sort called search_date "the table's primary order regardless
of insertion order", which was the bug stated as an intention.

WHY search_date IS THE WRONG KEY. It is not "when we found this". A scan refreshes it
whenever it re-encounters a call, so it means "last seen". Measured on the live table:
115 of 291 rows had a search_date more than a day from their created_at, and seven rows
inserted between 2026-07-03 and 2026-08-31 all carried search_date 2026-09-11 11:10 — the
last scan's timestamp, stamped onto rows that were already there. Three consequences, all
of which the user saw:

  * every re-seen old call bunches at the top sharing one timestamp, so their order
    relative to each other is arbitrary;
  * a genuinely new row is indistinguishable from an old row that was merely re-seen;
  * a manually submitted RFP — which no scan ever re-stamps — sinks fastest of all.

created_at is the true insertion time: DB-defaulted now() on insert, never touched by
Excel sync or later updates.

Run:  python -m unittest tests.test_records_ordering
"""
import io
import os
import sys
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

_PAGE = os.path.join(_ROOT, "views", "rfp_records.py")


def _src():
    with io.open(_PAGE, encoding="utf-8") as fh:
        return fh.read()


class ThePageSortsOnInsertionTimeTests(unittest.TestCase):
    def test_the_sort_key_is_created_at_not_search_date(self):
        src = _src()
        sort = src[src.index("df = df.sort_values("):]
        sort = sort[:sort.index(")") + 1]
        self.assertIn("_entered_dt", sort)
        self.assertNotIn("_search_dt", src,
                         "search_date means 'last seen', not 'when added'")

    def test_the_query_order_and_the_frame_order_agree(self):
        # The re-sort used to contradict the query. Whatever the page sorts by must be the
        # same thing `_fetch_all` asked the DB for, or one of them is decorative.
        src = _src()
        fetch = src[src.index("def _fetch_all"):src.index("df = _fetch_all")]
        self.assertIn('.order("created_at", desc=True)', fetch)
        self.assertIn('.order("submitted_at", desc=True)', fetch)
        self.assertIn('.order("uid", desc=True)', fetch)

    def test_helper_columns_are_not_displayed(self):
        src = _src()
        display = src[src.index("DISPLAY = ["):src.index("]", src.index("DISPLAY = ["))]
        for helper in ("_entered_dt", "_submitted_dt"):
            self.assertNotIn(helper, display)


class TheOrderingSemanticsTests(unittest.TestCase):
    """The pandas behaviour the page relies on, on a fixture shaped like the real bug."""

    def _sorted(self, rows):
        df = pd.DataFrame(rows)
        df["_entered_dt"] = pd.to_datetime(df.get("created_at"), errors="coerce",
                                           format="ISO8601")
        df["_submitted_dt"] = pd.to_datetime(df.get("submitted_at"), errors="coerce",
                                             format="ISO8601")
        return df.sort_values(["_entered_dt", "_submitted_dt", "uid"],
                              ascending=[False, False, False],
                              na_position="last").reset_index(drop=True)

    def test_a_new_row_outranks_an_old_row_the_last_scan_re_stamped(self):
        # Exactly the reported symptom: the old row was re-seen on 09-11 so its search_date
        # is NEWER than the new row's, and it used to sit above it.
        out = self._sorted([
            {"uid": "OLD-1", "created_at": "2026-07-03T09:31:00+00:00",
             "submitted_at": "2026-07-03T09:31:00+00:00",
             "search_date": "2026-09-11T11:10:00+00:00"},
            {"uid": "NEW-1", "created_at": "2026-09-12T08:00:00+00:00",
             "submitted_at": "2026-09-12T08:00:00+00:00",
             "search_date": "2026-09-12T08:00:00+00:00"},
        ])
        self.assertEqual(list(out["uid"]), ["NEW-1", "OLD-1"])

    def test_a_manual_row_with_no_search_date_still_ranks_by_when_it_was_added(self):
        # A manually submitted RFP is never re-stamped by a scan, so under the old sort it
        # sank to the bottom however recently it was added.
        out = self._sorted([
            {"uid": "AUTO-1", "created_at": "2026-08-01T09:00:00+00:00",
             "submitted_at": "2026-08-01T09:00:00+00:00",
             "search_date": "2026-09-11T11:10:00+00:00"},
            {"uid": "BE-MANUAL", "created_at": "2026-09-12T10:00:00+00:00",
             "submitted_at": "2026-09-12T10:00:00+00:00", "search_date": None},
        ])
        self.assertEqual(out.iloc[0]["uid"], "BE-MANUAL")

    def test_a_bulk_import_sharing_created_at_is_broken_by_submitted_at(self):
        # The Excel migration inserted its whole batch in one transaction, so created_at
        # ties to the microsecond and submitted_at carries each row's real original date.
        same = "2026-08-31T12:10:31.000000+00:00"
        out = self._sorted([
            {"uid": "MI-A", "created_at": same,
             "submitted_at": "2026-02-15T16:24:00+00:00", "search_date": None},
            {"uid": "MI-B", "created_at": same,
             "submitted_at": "2026-08-25T14:23:00+00:00", "search_date": None},
        ])
        self.assertEqual(list(out["uid"]), ["MI-B", "MI-A"])

    def test_an_unparseable_created_at_sinks_rather_than_crashing(self):
        out = self._sorted([
            {"uid": "BAD", "created_at": "not-a-date",
             "submitted_at": None, "search_date": None},
            {"uid": "GOOD", "created_at": "2026-09-12T10:00:00+00:00",
             "submitted_at": "2026-09-12T10:00:00+00:00", "search_date": None},
        ])
        self.assertEqual(list(out["uid"]), ["GOOD", "BAD"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
