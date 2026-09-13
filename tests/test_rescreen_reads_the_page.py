"""Re-screening must gate on the PAGE, not on an LLM summary of it.

THE REPORTED CASE (again). The Fondation Pierre Fabre / ODESS "2026 call for project
proposals" kept arriving in the pipeline — 2026-08-14 into one tenant, 2026-09-11 into
another — with a blank deadline, blank window and blank value, nearly a year after its
application window closed on 2025-11-07. Every expiry rule in tests.test_expiry_no_deadline
was in place and passing. They were starved, not wrong.

WHY THE EVIDENCE NEVER REACHED THEM.

1. THE GATES READ A PARAPHRASE. `_candidate_from_extracted` built `_page_text` as
   `brief_description or raw_text[:3000]` — and `brief_description` is the LLM synthesis,
   present on nearly every row, so `raw_text` was unreachable in practice. The expiry rules
   work on evidence a paraphrase does not preserve: the posting date under the headline,
   a window stated in prose, the latest year actually printed. The stored row held
   "17/10/2025" in `raw_text` and a brief asserting "Applications are open now" — a
   sentence the page never contains. The gates read the brief and admitted the call.
   The live-crawl path (scraper.py, deep_read.py) puts the FULL page text in this field,
   so re-screening was silently gating on weaker evidence than the crawl it stands in for.

2. THE DEADLINE BACKSTOP WAS SWITCHED OFF FOR STORED ROWS. It was guarded by
   `not cand.get("extraction_uid")`, on the reasoning that a stored row is already
   extracted. But a row is only as good as the day it was stored, and this one was stored
   with the deadline dropped for low confidence — so re-screening could never acquire one.
   It is pure regex over text already in hand; there is no cost reason to skip it.

3. OUR OWN CRAWLING KEPT THE ROW LOOKING FRESH. `mark_closed_stale_undated` ages a row by
   `updated_at` / `scraped_at`, which every weekly re-crawl resets — so the rows it can
   close are the ones we have STOPPED looking at, the opposite of the problem. Measured on
   the live store: 32 undated rows Open, 0 ever closed by that sweep.
   `mark_closed_stale_posted` ages on `date_posted`, the funder's own publication date,
   which re-crawling cannot move. It closed 13 rows on the first run.

Measured after the fix, on the live store row es_b0a85fcebb0010c4c9dd:
    _page_text 1000 -> 4121 chars · date_posted NULL -> 2025-10-17
    is_eligible -> (False, 'deadline: the application window stated on the page closed')

Run:  python -m unittest tests.test_rescreen_reads_the_page
"""
import io
import os
import sys
import unittest
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import auto_scorer as A                                     # noqa: E402
from core import deadline_extract as DE                               # noqa: E402
from core import extracted_store as ES                                # noqa: E402
from core import scan_pipeline as SP                                  # noqa: E402

# The real page, trimmed: the headline carries the 2026 that made it look current, and the
# posting date sits unlabelled underneath it — exactly as on fondationpierrefabre.org.
_TITLE = ("The Global South E-Health Observatory launches its 2026 call for "
          "project proposals!")
_RAW = (f"{_TITLE} - Fondation Pierre Fabre Skip to content {_TITLE} 17/10/2025 "
        "Join the community of ODESS award winners and contribute to the digital "
        "transformation of healthcare in the low-middle-income countries! The Global "
        "South e-Health Observatory (ODESS) is launching its 2026 call for projects to "
        "support initiatives using information and communication technologies to improve "
        "access to healthcare and quality medicines in African and Asian countries.")
# The synthesis that was standing in for it — note the closing sentence, which is not on
# the page and is the opposite of true.
_BRIEF = ("The Global South e-Health Observatory (ODESS), supported by the Fondation "
          "Pierre Fabre, is inviting proposals for its 2026 call to accelerate digital "
          "transformation of health services in low- and middle-income countries. "
          "Applications are open now and will be evaluated by a panel of experts.")


def _store_row(**kw):
    row = {"uid": "es_test", "opportunity_name": _TITLE,
           "opportunity_url": "https://www.fondationpierrefabre.org/en/call-for-project/x/",
           "funder_name": "Fondation Pierre Fabre", "brief_description": _BRIEF,
           "raw_text": _RAW, "deadline": None, "date_posted": None,
           "funding_status": "Open", "call_geographic_scope": ["Global South"]}
    row.update(kw)
    return row


class TheGateReadsThePageTests(unittest.TestCase):
    def test_page_text_is_the_raw_page_not_the_brief(self):
        cand = SP._candidate_from_extracted(_store_row())
        self.assertIn("17/10/2025", cand["_page_text"],
                      "the posting date is the only expiry evidence on this page")
        self.assertNotIn("Applications are open now", cand["_page_text"],
                         "the synthesis must not stand in for the page at the gate")

    def test_the_brief_is_still_used_when_there_is_no_raw_text(self):
        # 55 live rows have an empty raw_text; they must not lose their description.
        cand = SP._candidate_from_extracted(_store_row(raw_text=""))
        self.assertEqual(cand["_page_text"], _BRIEF)

    def test_the_posting_date_is_recoverable_from_the_page_but_not_the_brief(self):
        self.assertEqual(DE.extract_posted_date(_BRIEF, title="")[0], None)
        self.assertEqual(DE.extract_posted_date(_RAW, title=_TITLE)[0], "2025-10-17")

    def test_page_text_is_bounded(self):
        cand = SP._candidate_from_extracted(_store_row(raw_text="x" * 40_000))
        self.assertEqual(len(cand["_page_text"]), 12_000,
                         "unbounded raw_text over the whole Open set is what the "
                         "brief was introduced to avoid")


class TheBackstopsRunOnStoredRowsTests(unittest.TestCase):
    def test_the_deadline_backstop_is_not_gated_on_extraction_uid(self):
        with io.open(os.path.join(_ROOT, "core", "scan_pipeline.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        head = src.index("# Deadline backstop")
        guard = src.index("if not cand.get(\"call_submission_deadline\")", head)
        line_end = src.index("\n", guard)
        self.assertNotIn("extraction_uid", src[guard:line_end],
                         "a stored row is the one that most needs the backstop")

    def test_a_stale_undated_stored_row_is_now_blocked(self):
        cand = SP._candidate_from_extracted(_store_row())
        # What the pipeline's posting-date backstop does, on the text it now receives.
        cand["date_posted"] = DE.extract_posted_date(
            cand["_page_text"], title=cand["opportunity_title"])[0]
        blocked, why = A.insufficient_data_reject(cand)
        self.assertTrue(blocked, "posted 2025-10-17 with no deadline is a closed window")
        self.assertIn("2025-10-17", why)

    def test_a_recently_posted_undated_row_is_still_kept(self):
        # The rule that protects genuinely new undated calls must survive all of this.
        recent = (date.today() - timedelta(days=10)).isoformat()
        cand = SP._candidate_from_extracted(_store_row())
        cand["date_posted"] = recent
        self.assertFalse(A.insufficient_data_reject(cand)[0])


class RollingCallsSurviveTests(unittest.TestCase):
    def test_a_rolling_call_is_not_expired_by_a_past_date_on_its_page(self):
        # `deadline_in_future` consults `_expired_window` before it asks whether the call
        # is rolling, so the signal must not be set for one in the first place.
        with io.open(os.path.join(_ROOT, "core", "scan_pipeline.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        branch = src.index('cand["_expired_window"]')
        guard = src.rindex("elif (", 0, branch)
        self.assertIn("is_rolling_call", src[guard:branch],
                      "an open-ended fund must not be retired for being open")

    def test_the_posted_sweep_exempts_rolling_and_shares_the_window(self):
        self.assertTrue(callable(ES.mark_closed_stale_posted))
        self.assertEqual(ES._STALE_UNDATED_DAYS, A._STALE_POSTING_DAYS)
        with io.open(os.path.join(_ROOT, "core", "extracted_store.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def mark_closed_stale_posted"):]
        self.assertIn("funding_window.is.null", body,
                      "a bare .neq() on a NULL column exempts almost every row")

    def test_a_bad_date_is_not_a_crash(self):
        self.assertEqual(ES.mark_closed_stale_posted("not-a-date"), 0)


class TheCronAgesOnThePostingDateTests(unittest.TestCase):
    def test_the_posted_sweep_runs_before_screening(self):
        with io.open(os.path.join(_ROOT, "scripts", "run_scan.py"),
                     encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("mark_closed_stale_posted", src)
        self.assertLess(src.index("mark_closed_stale_posted"),
                        src.index("screen_all_tenants"),
                        "the store must be aged BEFORE screening reads the Open set")


if __name__ == "__main__":
    unittest.main(verbosity=2)
