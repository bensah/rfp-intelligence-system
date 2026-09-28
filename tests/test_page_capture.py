"""Read the call's own page before judging it.

Both enrichment steps in the scan loop were gated on the candidate looking THIN
(no description) or UNDATED. So a feed item that arrived with a snippet AND a
deadline looked complete and its page was never fetched - which is exactly the
case where the page carries the evidence that decides the verdict. Three gates
read `_full_text` (title + brief + scope + funder, ~1000 chars) and were ruling
on a summary.

Measured, then fixed, then measured again:

    AS-260918-111124   403 chars of RSS "Calendar of Events" + a deadline.
                       Recovered page 3794 chars -> "funded by a US STATE
                       government agency" and it rejects.
    AS-260918-111157   882 chars of search snippet.
                       Recovered page 4649 chars -> "latest year on page is 2022
                       (past)" and it rejects.

Both had sat in a review week as rows a person opened and dismissed.
"""
from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path

from core import live_check
from core.auto_scorer import PAGE_TEXT_FLOOR, have_call_page, _page_was_fetched


class TheBudgetsAreSeparate(unittest.TestCase):
    """Sharing one counter would let list order starve the other case.

    The liveness budget serves thin / undated candidates. Page capture serves the
    ones that look complete because a feed handed us a snippet and a deadline. If
    they shared a counter, whichever candidates came first would spend it.
    """

    def test_page_capture_has_its_own_cap(self):
        self.assertNotEqual(live_check.max_page_captures.__name__,
                            live_check.max_checks.__name__)
        self.assertGreater(live_check.max_page_captures(), 0)

    def test_the_page_budget_is_the_larger_one(self):
        # ~250 candidates survive the cheap first gate in a full run; only the
        # thin/undated subset needs a liveness check.
        self.assertGreater(live_check.max_page_captures(), live_check.max_checks())

    def test_both_are_env_tunable(self):
        for var, fn, default in (("RFPIS_PAGE_CAPTURE_MAX", live_check.max_page_captures, 250),
                                 ("RFPIS_LIVE_CHECK_MAX", live_check.max_checks, 80)):
            with self.subTest(var=var):
                prev = os.environ.get(var)
                os.environ[var] = "7"
                try:
                    self.assertEqual(fn(), 7)
                finally:
                    if prev is None:
                        os.environ.pop(var, None)
                    else:
                        os.environ[var] = prev
                self.assertEqual(fn(), default)

    def test_a_junk_value_falls_back_to_the_default(self):
        prev = os.environ.get("RFPIS_PAGE_CAPTURE_MAX")
        os.environ["RFPIS_PAGE_CAPTURE_MAX"] = "not-a-number"
        try:
            self.assertEqual(live_check.max_page_captures(), 250)
        finally:
            if prev is None:
                os.environ.pop("RFPIS_PAGE_CAPTURE_MAX", None)
            else:
                os.environ["RFPIS_PAGE_CAPTURE_MAX"] = prev


class TheTriggerIsAboutThePageNotTheSummary(unittest.TestCase):
    def test_a_feed_snippet_or_fragment_is_not_the_call_page(self):
        # The two real leaks, at their real lengths.
        self.assertFalse(have_call_page({"_page_text": "x" * 403}))
        self.assertFalse(have_call_page({"raw_text": "x" * 882}))

    def test_a_real_page_is(self):
        self.assertTrue(have_call_page({"_page_text": "x" * 3794}))
        self.assertTrue(have_call_page({"raw_text": "x" * 4649}))

    def test_the_two_questions_are_deliberately_different(self):
        """403 chars is MORE than a search snippet and FAR LESS than a page.

        `_page_was_fetched` asks "is a snippet all we ever got?" for the
        never-read gate. `have_call_page` asks "do we hold the call's page?" for
        the capture trigger. Collapsing them would either stop capturing the
        403-char row or start rejecting rows the never-read gate should not touch.
        """
        frag = {"_page_text": "x" * 403}
        self.assertTrue(_page_was_fetched(frag))
        self.assertFalse(have_call_page(frag))

    def test_a_synthesised_brief_is_never_evidence(self):
        # The whole failure in one assertion: generated prose must not answer
        # "did we manage to read this call?".
        for fn in (_page_was_fetched, have_call_page):
            with self.subTest(fn=fn.__name__):
                self.assertFalse(fn({"brief_description": "y" * 5000,
                                     "_page_text": ""}))


class ScanLoopWiring(unittest.TestCase):
    """Static checks on the pipeline, which cannot be exercised without a scan."""

    @staticmethod
    def _source() -> str:
        return Path("core/scan_pipeline.py").read_text(encoding="utf-8")

    def test_capture_runs_before_the_liveness_check(self):
        src = self._source()
        capture = src.index("have_call_page(cand)")
        liveness = src.index("and (_thin or not cand.get(\"call_submission_deadline\"))")
        self.assertLess(capture, liveness,
                        "page capture must come first; it is the broader trigger")

    def test_the_gate_is_re_run_after_capture(self):
        # Capturing the page is pointless if nothing re-judges on it.
        src = self._source()
        block = src[src.index("have_call_page(cand)"):]
        block = block[:block.index("# Cheap liveness")]
        self.assertIn("is_eligible(", block)
        self.assertIn("post page-capture", block)

    def test_an_exhausted_budget_is_logged(self):
        # A budget that silently runs out is how this became invisible.
        src = self._source()
        self.assertIn("_page_capture_skipped", src)
        self.assertIn("RFPIS_PAGE_CAPTURE_MAX", src)

    def test_store_sourced_candidates_are_not_re_fetched(self):
        src = self._source()
        block = src[src.index("# PAGE CAPTURE"):]
        block = block[:block.index("# Cheap liveness")]
        self.assertIn("extraction_uid", block,
                      "a candidate rebuilt from the store already has its text")


class TheBackfillNeverShortens(unittest.TestCase):
    """A bot wall or an error template must not erase evidence we already hold.

    Not hypothetical: grantplus.unops.org returns SIX characters to a plain fetch
    (it is a JS app) against 1800 already stored, and three rows hit that in the
    live dry run.
    """

    @staticmethod
    def _source() -> str:
        return Path("scripts/backfill_page_text.py").read_text(encoding="utf-8")

    def test_it_compares_lengths_before_writing(self):
        src = self._source()
        self.assertIn("if len(text) <= had:", src)

    def test_it_writes_only_raw_text(self):
        tree = ast.parse(self._source())
        updated_keys = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "update"):
                for a in node.args:
                    if isinstance(a, ast.Dict):
                        updated_keys |= {k.value for k in a.keys
                                         if isinstance(k, ast.Constant)}
        self.assertEqual(updated_keys, {"raw_text"},
                         "a page backfill must not touch deadlines or decisions")

    def test_it_is_dry_run_by_default(self):
        src = self._source()
        self.assertIn('"--apply", action="store_true"', src)
        self.assertIn("DRY RUN", src)

    def test_the_floor_sits_in_the_valley_of_the_measured_distribution(self):
        # 315 rows under 500 chars, 301 over 6000, a clear valley around 2000.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bpt", Path("scripts/backfill_page_text.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertGreater(mod.SNIPPET_FLOOR, 882,   # the larger real snippet
                           "must be above the snippets it exists to catch")
        self.assertLess(mod.SNIPPET_FLOOR, 3794,     # the smaller real page
                        "must be below the pages it exists to keep")


class ThePruneWillNotDeleteOnAnUnreadVerdict(unittest.TestCase):
    """It listed two genuine Wellcome awards for deletion.

    `is_eligible` was called with no adjudicator, so the theme gate ruled on
    ~1100 characters while 12,000 characters of page said "health" twenty times.
    A leak costs minutes; this costs the opportunity.
    """

    @staticmethod
    def _source() -> str:
        return Path("scripts/prune_ineligible_screened.py").read_text(encoding="utf-8")

    def test_the_gate_is_called_with_the_adjudicator(self):
        src = self._source()
        self.assertIn("llm_adjudicate=True", src)
        self.assertIn("llm_theme=True", src)

    def test_a_theme_reject_is_held_when_the_judge_is_unavailable(self):
        src = self._source()
        self.assertIn("judge_ok", src)
        self.assertIn('reason.startswith("theme:")', src)
        self.assertIn("held", src)

    def test_it_also_runs_the_data_sufficiency_gate(self):
        self.assertIn("insufficient_data_reject", self._source())

    def test_it_tombstones_before_deleting(self):
        src = self._source()
        tomb = src.index("seen_ledger")
        delete = src.index('.delete().eq("id"')
        self.assertLess(tomb, delete,
                        "a row deleted without a tombstone comes straight back")

    def test_a_row_it_cannot_tombstone_is_not_deleted(self):
        self.assertIn("untombstoned", self._source())


if __name__ == "__main__":
    unittest.main()
