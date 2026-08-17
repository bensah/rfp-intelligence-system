"""The tombstone ledger must survive the field rename that silently killed it.

THE REPORTED CASE. Opportunities the owner had already dealt with kept reappearing in the
pipeline week after week - including two expired calls from one funder that had been
removed more than once.

`core/seen_ledger` is the mechanism that is supposed to make that impossible. Its own
docstring calls it "the backstop for DELETED rows - the leak it closes". It had been dead
for as long as the rename has been in place:

  * migration 033 created `rfp_seen` with `submission_deadline` / `estimated_value`
  * migrations 056 and 059 renamed those two columns on `rfp_submissions` ONLY
  * this module asked `rfp_seen` for the NEW names

so every read and every write raised Postgres 42703 (`column
"call_submission_deadline" does not exist`). Both call sites caught the exception and
logged it at DEBUG, so the scan reported normally while recording nothing and suppressing
nothing. Proved live: `select uid, call_submission_deadline from rfp_seen` fails, 150
tombstones sat in the table unreadable, and `fetch_all()` returned [].

The fix resolves the column names against the live table instead of pinning one spelling,
so a future rename cannot reintroduce the same silent failure.

Run:  python -m unittest tests.test_seen_ledger_schema_drift
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import seen_ledger                                          # noqa: E402


class _Q:
    """Minimal stand-in for the query builder: refuses columns the table lacks."""

    def __init__(self, table, columns, rows):
        self.table, self.columns, self.rows = table, columns, rows
        self.selected, self.upserted = None, None

    def select(self, cols):
        wanted = [c.strip() for c in cols.split(",")]
        missing = [c for c in wanted if c not in self.columns]
        if missing:
            raise RuntimeError('column "%s" does not exist' % missing[0])
        self.selected = wanted
        return self

    def limit(self, _n):
        return self

    def upsert(self, payload, on_conflict=None):
        for row in payload:
            bad = [k for k in row if k not in self.columns and k != "reason"]
            if bad:
                raise RuntimeError('column "%s" does not exist' % bad[0])
        self.upserted = payload
        return self

    @property
    def data(self):
        return [{c: r.get(c) for c in (self.selected or self.columns)} for r in self.rows]


class _Client:
    def __init__(self, columns, rows):
        self.columns, self.rows, self.last = columns, rows, None

    def table(self, name):
        self.last = _Q(name, self.columns, self.rows)
        return self.last


LEGACY_COLUMNS = ("uid", "opportunity_id", "opportunity_title", "opportunity_link",
                  "funding_agency", "submission_deadline", "estimated_value", "reason")
MODERN_COLUMNS = ("uid", "opportunity_id", "opportunity_title", "opportunity_link",
                  "funding_agency", "call_submission_deadline", "call_award_value",
                  "reason")
ROW = {"uid": "AS-1", "opportunity_id": "RFP-9", "opportunity_title": "A call",
       "opportunity_link": "https://example.org/c", "funding_agency": "F",
       "submission_deadline": "2026-03-15", "estimated_value": 100,
       "call_submission_deadline": "2026-03-15", "call_award_value": 100}


class _Base(unittest.TestCase):
    def _install(self, columns, rows=(ROW,)):
        client = _Client(columns, list(rows))
        seen_ledger._colmap = None                       # forget the cached probe
        self._orig_get = seen_ledger.get_client
        seen_ledger.get_client = lambda: client
        import db.supabase_client as sc
        self._orig_safe = sc.safe_execute
        sc.safe_execute = lambda q: q
        self.addCleanup(self._restore, sc)
        return client

    def _restore(self, sc):
        seen_ledger.get_client = self._orig_get
        sc.safe_execute = self._orig_safe
        seen_ledger._colmap = None


class TheColumnProbeTests(_Base):
    def test_it_finds_the_legacy_names_the_live_table_actually_has(self):
        self._install(LEGACY_COLUMNS)
        self.assertEqual(seen_ledger.column_map()["call_submission_deadline"],
                         "submission_deadline")

    def test_it_prefers_the_current_names_when_the_table_has_been_renamed(self):
        self._install(MODERN_COLUMNS)
        self.assertEqual(seen_ledger.column_map()["call_submission_deadline"],
                         "call_submission_deadline")

    def test_neither_spelling_falls_back_to_identity_columns_only(self):
        self._install(("uid", "opportunity_id", "opportunity_title",
                       "opportunity_link", "funding_agency", "reason"))
        self.assertEqual(seen_ledger.column_map(), {})


class ReadingTombstonesTests(_Base):
    def test_rows_come_back_keyed_the_way_the_matcher_expects(self):
        # This is the whole point: the table says submission_deadline, find_duplicates
        # reads call_submission_deadline, and neither should have to know about the other.
        self._install(LEGACY_COLUMNS)
        rows = seen_ledger.fetch_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["call_submission_deadline"], "2026-03-15")
        self.assertNotIn("submission_deadline", rows[0])

    def test_a_tombstoned_opportunity_is_matched_by_the_deduplicator(self):
        self._install(LEGACY_COLUMNS)
        from core import deduplicator as D
        cand = {"uid": "AS-returning", "opportunity_title": "A call",
                "opportunity_link": "https://example.org/c", "funding_agency": "F"}
        self.assertEqual(len(D.find_duplicates(cand, seen_ledger.fetch_all())), 1)

    def test_an_unreadable_ledger_is_loud_rather_than_silent(self):
        # It failed for weeks precisely because this was logged at DEBUG.
        self._install(("uid",))
        seen_ledger._colmap = {}
        with self.assertLogs("core.seen_ledger", level="WARNING") as caught:
            self.assertEqual(seen_ledger.fetch_all(), [])
        self.assertTrue(any("suppression is OFF" in m for m in caught.output))


class WritingTombstonesTests(_Base):
    def test_it_writes_the_column_names_the_table_really_has(self):
        client = self._install(LEGACY_COLUMNS)
        n = seen_ledger.record([{"uid": "AS-2", "opportunity_title": "T",
                                 "opportunity_link": "L", "funding_agency": "F",
                                 "call_submission_deadline": "2026-05-01",
                                 "call_award_value": 5}], reason="removed")
        self.assertEqual(n, 1)
        written = client.last.upserted[0]
        self.assertEqual(written["submission_deadline"], "2026-05-01")
        self.assertNotIn("call_submission_deadline", written)
        self.assertEqual(written["reason"], "removed")

    def test_it_omits_columns_the_table_cannot_store(self):
        client = self._install(("uid", "opportunity_id", "opportunity_title",
                               "opportunity_link", "funding_agency", "reason"))
        self.assertEqual(seen_ledger.record([{"uid": "AS-3",
                                              "call_submission_deadline": "2026-05-01"}]), 1)
        self.assertEqual(client.last.upserted[0].get("uid"), "AS-3")
        self.assertNotIn("submission_deadline", client.last.upserted[0])

    def test_a_failed_write_is_reported_not_swallowed(self):
        self._install(("nothing_useful",))
        seen_ledger._colmap = {}
        with self.assertLogs("core.seen_ledger", level="WARNING") as caught:
            self.assertEqual(seen_ledger.record([{"uid": "AS-4"}]), 0)
        self.assertTrue(any("not tombstoned" in m for m in caught.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
