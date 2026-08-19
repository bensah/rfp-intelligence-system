"""A scan log must distinguish what a run FOUND ELIGIBLE from what it ADDED.

`rfps_new` never meant what its name says: it is the first return value of
ingest_candidates, defined in that function's own docstring as "inserted + merge-updates".
A screening run where every eligible call was already tracked logged "12 new", the bell
repeated it, and the Screen tab showed nothing — which is exactly what a reader reported.

`rfps_added` (migration 094) is the count of rows a run CREATED. The rule that carries the
honesty is that a missing count is NULL, never 0: a run from before the column existed, or
an extract-only crawl that inserts nothing into any pipeline, has no created count to
report, and reporting zero would be a claim the data cannot support.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_MIGRATION = os.path.join(_ROOT, "db", "migrations", "094_scan_logs_added.sql")

# Imported at module load rather than inside a test, so the binding is made once against
# whatever `streamlit` is installed when this module is read.
#
# NOTE, if you run the whole suite in ONE process: tests/test_session_identity_isolation,
# test_tenant_isolation and test_workbook_isolation each do a module-level
# `sys.modules["streamlit"] = _fake_st` and never put the real module back, so anything
# imported after them that uses a module-level @st.cache_data raises. That predates this
# file (the same three fail together on main) and is why the suite is run module by
# module. This import cannot dodge it — unittest reads every named module before running
# anything — it just keeps the failure honest rather than hiding it behind a lazy import.
from core import notifications as _notifications          # noqa: E402


class TheMigrationTests(unittest.TestCase):
    def setUp(self):
        with open(_MIGRATION, encoding="utf-8") as fh:
            self.sql = fh.read()

    def test_it_is_re_runnable(self):
        # Migrations are not tracked here, so applying one twice has to be a no-op rather
        # than an error.
        self.assertIn("add column if not exists rfps_added", self.sql.lower())

    def test_it_does_not_default_the_column(self):
        # `default 0` would backfill every historical row with "this run added nothing",
        # which is the conflation the column exists to end.
        stmt = re.search(r"alter table scan_logs(.*?);", self.sql, re.S | re.I)
        self.assertIsNotNone(stmt)
        self.assertNotIn("default", stmt.group(1).lower())

    def test_it_declares_no_dependency_on_another_migration(self):
        # Self-contained: it must not assume 074/065 ran first.
        self.assertNotRegex(self.sql, r"(?i)\balter table\s+(?!scan_logs\b)")


class TheBellWordingTests(unittest.TestCase):
    """core.notifications.scan_icon_and_detail is what a reader actually sees."""

    def _detail(self, **row):
        return _notifications.scan_icon_and_detail(row)[1]

    def test_a_run_that_added_nothing_says_so(self):
        # The reported case: twelve eligible, none of them new.
        self.assertEqual(self._detail(rfps_added=0, rfps_new=12, rfps_found=40),
                         "0 new · 12 eligible · 40 found")

    def test_a_run_that_added_something_leads_with_it(self):
        self.assertEqual(self._detail(rfps_added=3, rfps_new=12, rfps_found=40),
                         "3 new · 12 eligible · 40 found")

    def test_an_unrecorded_count_is_not_reported_as_zero(self):
        # A pre-094 row, or an extract-only crawl. Saying "0 new" here would be inventing
        # a fact; the line simply does not claim one.
        detail = self._detail(rfps_new=12, rfps_found=40)
        self.assertEqual(detail, "12 eligible · 40 found")
        self.assertNotIn("new", detail)

    def test_errors_still_win(self):
        self.assertEqual(self._detail(rfps_added=0, rfps_new=0, rfps_found=0,
                                      errors="boom"), "completed with errors")


class TheUncollapsedStatsTests(unittest.TestCase):
    """ingest_candidates counts inserted and updated separately and then adds them
    together on the way out — which is why rfps_new could never answer "how many did this
    run create". The stats out-param hands back the breakdown without widening the return
    tuple that several callers unpack."""

    def test_the_empty_run_still_fills_every_key(self):
        # A half-filled dict would read as "no inserts" at the call site rather than
        # "nothing ran", so every return path has to write it.
        from core.scan_pipeline import ingest_candidates
        stats: dict[str, int] = {}
        self.assertEqual(ingest_candidates([], stats=stats), (0, 0, 0, 0))
        for key in ("inserted", "updated", "extracted", "duplicate_unchanged",
                    "suppressed_seen", "rejected", "store_errors"):
            with self.subTest(key=key):
                self.assertEqual(stats.get(key), 0)

    def test_the_out_param_is_optional(self):
        # Existing callers pass no stats and must be untouched.
        from core.scan_pipeline import ingest_candidates
        self.assertEqual(ingest_candidates([]), (0, 0, 0, 0))


class TheScanLoggerTests(unittest.TestCase):
    """scripts.run_scan._log_scan writes the row. A None must be OMITTED, not written."""

    def _capture(self, **kw):
        import scripts.run_scan as rs

        captured = {}

        class _Tbl:
            def insert(self, row):
                captured.update(row)
                return self

            def execute(self):        # safe_execute calls this
                return self

        class _SB:
            def table(self, _name):
                return _Tbl()

        rs._log_scan(_SB(), source="s", triggered_by="cron", found=40, new=12,
                     dup=0, rejected=28, duration=1.0, **kw)
        return captured

    def test_a_real_count_is_written(self):
        self.assertEqual(self._capture(added=3).get("rfps_added"), 3)

    def test_zero_is_a_real_count_and_is_written(self):
        # "added nothing" is a fact worth recording; only "not measured" is omitted.
        self.assertEqual(self._capture(added=0).get("rfps_added"), 0)

    def test_an_unmeasured_run_omits_the_column(self):
        self.assertNotIn("rfps_added", self._capture(added=None))

    def test_omitting_the_argument_omits_the_column(self):
        self.assertNotIn("rfps_added", self._capture())


if __name__ == "__main__":
    unittest.main()
