"""The report's section ORDER and NUMBERING, and that the page still runs after being reordered.

Two things are pinned here.

**The ids are not the display numbers.** Saved reports persist section ids and `sN_` metric keys,
so a shared or refreshed report restores what it was generated with. Renumbering the ids to match
a new running order would silently change what an existing report shows. Team & partners is
therefore still id "4" while displaying as section 2.

**The page must survive the reorder.** Moving Team & Partnership Activity above Insights meant
physically moving ~300 lines of a script-scope Streamlit page. If that block referenced anything
defined by the sections it jumped over, the page raises — and with error details suppressed a
raised exception renders as BLANK SPACE, so the report would look merely empty from the failure
point down. Nothing in the suite would see it unless something executes the page, which is what
the second half of this file does.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

_REPORT_PY = os.path.join(_ROOT, "views", "report.py")


def _source() -> str:
    with open(_REPORT_PY, encoding="utf-8") as fh:
        return fh.read()


def _registry() -> list:
    """`_REPORT_SECTIONS` read from source — the page is script-scope, so importing it would
    execute the whole report."""
    tree = ast.parse(_source())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_REPORT_SECTIONS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("_REPORT_SECTIONS not found")


class TheDisplayOrderTests(unittest.TestCase):
    def test_the_running_order_is_search_team_insights_reviews_results(self):
        self.assertEqual([sid for sid, _, _ in _registry()], ["1", "4", "2", "3", "5"])

    def test_the_display_numbers_run_one_to_five_without_a_gap(self):
        nums = [lbl.split("·")[0].strip() for _, lbl, _ in _registry()]
        self.assertEqual(nums, ["1", "2", "3", "4", "5"])

    def test_team_and_partners_is_displayed_second(self):
        sid, label, _ = _registry()[1]
        self.assertEqual(sid, "4")                       # id unchanged
        self.assertTrue(label.startswith("2 · "), label)  # display number changed

    def test_the_ids_are_unchanged_so_saved_reports_still_resolve(self):
        # A report saved before the reorder stores these ids. They must still mean the same
        # sections, or reopening it would show something else.
        by_id = {sid: label for sid, label, _ in _registry()}
        self.assertIn("Search", by_id["1"])
        self.assertIn("Insights", by_id["2"])
        self.assertIn("Reviews", by_id["3"])
        self.assertIn("Team", by_id["4"])
        self.assertIn("results", by_id["5"])

    def test_metric_keys_keep_their_historical_prefixes(self):
        # `s4_partners` is persisted in saved reports; its prefix is the section ID, not the
        # display number, and renaming it would drop the metric from existing reports.
        keys = {k for _, _, items in _registry() for k, _ in items}
        self.assertIn("s4_partners", keys)
        self.assertIn("s2_funnel", keys)
        self.assertIn("s3_decdist", keys)


class TheRenamedAndAddedItemsTests(unittest.TestCase):
    def test_insights_names_status_as_well_as_the_funnel(self):
        self.assertIn('st.subheader("3 · Insights — Status & Eligibility Funnel")', _source())

    def test_donor_decisions_is_a_metric_of_the_reviews_section(self):
        items = dict((sid, [k for k, _ in its]) for sid, _, its in _registry())
        self.assertIn("s3_donordec", items["3"])

    def test_donor_decisions_renders_under_reviews_and_decisions(self):
        src = _source()
        i_reviews = src.index('st.subheader("4 · Reviews & Decisions")')
        i_donor = src.index('st.markdown("##### Donor Decisions")')
        i_results = src.index("# SECTION 5 — Our Results")
        self.assertLess(i_reviews, i_donor)
        self.assertLess(i_donor, i_results)

    def test_the_headings_appear_in_the_file_in_display_order(self):
        src = _source()
        positions = [src.index(h) for h in (
            'st.subheader("1 · Search activity")',
            'st.subheader("2 · Team & Partnership Activity")',
            'st.subheader("3 · Insights — Status & Eligibility Funnel")',
            'st.subheader("4 · Reviews & Decisions")',
            'st.subheader("5 · Our Results',
        )]
        self.assertEqual(positions, sorted(positions))


class ThePrintLayoutTests(unittest.TestCase):
    """The print rules are CSS/JS in a Python string, so what can be checked here is that the
    pieces the fix depends on are present. The scaling behaviour itself was verified in a
    browser against real Plotly output."""

    def test_the_page_is_portrait_with_margins(self):
        self.assertRegex(_source(), r"@page\s*\{[^}]*size:\s*portrait")
        self.assertRegex(_source(), r"@page\s*\{[^}]*margin:\s*12mm")

    def test_columns_stack_in_print_so_charts_get_the_full_width(self):
        src = _source()
        self.assertIn('[data-testid="stHorizontalBlock"] { display: block !important; }', src)
        self.assertIn('[data-testid="stColumn"] {', src)

    def test_subsection_headings_cannot_break_away_from_their_chart(self):
        # h4/h5/h6 were missing from the break rules, which is why "Lead & Sub Applicant
        # partners" ended up drawn over the charts.
        self.assertRegex(_source(), r"h1, h2, h3, h4, h5, h6 \{\s*\n?\s*page-break-after: avoid")

    def test_the_print_width_is_derived_from_paper_not_measured(self):
        # `beforeprint` fires before print layout, so measuring at that point returns screen
        # widths — the reason this is a constant.
        self.assertRegex(_source(), r"RFPIS_PRINT_W\s*=\s*7\d\d")

    def test_the_hook_is_injected_into_the_parent_document(self):
        # Registering the listener from inside the component iframe leaves it pointing at a
        # torn-down realm after any Streamlit rerun.
        src = _source()
        self.assertIn("rfpis-print-hook", src)
        self.assertIn("pdoc.head.appendChild(el)", src)

    def test_it_restores_the_screen_layout_afterwards(self):
        self.assertIn("afterprint", _source())
        self.assertIn("rfpisRestorePlots", _source())

    def test_charts_are_never_enlarged_only_shrunk(self):
        self.assertIn("if (k >= 1) return;", _source())


class ThePageStillRunsTests(unittest.TestCase):
    """Drives the REAL page, in a subprocess.

    The subprocess is not fastidiousness. Run in-process after this suite's other AppTest
    module, the report page renders nothing at all — no elements and no exception — because
    Streamlit holds global runtime state and our own modules stay bound in sys.modules to
    whichever `streamlit` was live when they were first imported. Purging and re-importing both
    was not enough, and the failure mode (a page that renders nothing) is indistinguishable from
    the regression this test exists to catch. A fresh interpreter is the isolation that holds.

    One subprocess, several assertions, so the cost is paid once.
    """

    @classmethod
    def setUpClass(cls):
        probe = os.path.join(_ROOT, "tests", "report_page_probe.py")
        proc = subprocess.run([sys.executable, probe], cwd=_ROOT,
                              capture_output=True, text=True, timeout=600)
        marker = "---PROBE---"
        if marker not in proc.stdout:
            raise AssertionError(
                f"probe produced no result (exit {proc.returncode})\n"
                f"stdout tail:\n{proc.stdout[-2000:]}\n"
                f"stderr tail:\n{proc.stderr[-2000:]}")
        cls.result = json.loads(proc.stdout.split(marker, 1)[1].strip().splitlines()[-1])

    def test_the_page_raises_nothing(self):
        self.assertEqual(self.result["exceptions"], [])

    def test_all_five_sections_arrive_in_display_order(self):
        heads = self.result["subheaders"]
        wanted = ["1 · Search activity",
                  "2 · Team & Partnership Activity",
                  "3 · Insights — Status & Eligibility Funnel",
                  "4 · Reviews & Decisions"]
        for w in wanted:
            self.assertIn(w, heads, f"missing section heading: {w}")
        self.assertEqual([heads.index(w) for w in wanted],
                         sorted(heads.index(w) for w in wanted))

    def test_our_results_is_last(self):
        heads = self.result["subheaders"]
        self.assertTrue(heads[-1].startswith("5 · Our Results"), heads[-1])

    def test_the_section_that_moved_renders_its_own_subsections(self):
        # The block move is the risky change: a name defined by a section it jumped over would
        # raise, and a raised exception renders as blank space rather than an error.
        body = self.result["markdown"]
        self.assertIn("Team Touchpoints", body)
        self.assertIn("Lead & Sub Applicant partners", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
