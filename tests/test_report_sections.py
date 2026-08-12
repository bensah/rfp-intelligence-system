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
import re
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
    def test_the_running_order_is_scan_team_insights_reviews_results(self):
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
        self.assertIn("Scan", by_id["1"])
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
        i_donor = src.index("_h5(\"Donor decisions")
        i_results = src.index("# SECTION 5 — Our Results")
        self.assertLess(i_reviews, i_donor)
        self.assertLess(i_donor, i_results)

    def test_the_headings_appear_in_the_file_in_display_order(self):
        src = _source()
        positions = [src.index(h) for h in (
            'st.subheader("1 · Scan activity")',
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
        self.assertIn("maxW / natW >= 1) return;", _source())

    def test_printing_re_lays_out_rather_than_shrinking_the_svg(self):
        # Shrinking the SVG scales EVERYTHING, so 11px axis text printed at about 5pt — which is
        # what "blurry" was. The PDF was vector throughout; the type was just too small.
        # Plotly.relayout recomputes the chart for the narrower box and keeps font sizes.
        src = _source()
        self.assertIn("window.Plotly.relayout(plot, {width: maxW})", src)
        i_relayout = src.index("window.Plotly.relayout(plot, {width: maxW})")
        i_fallback = src.index("svg.setAttribute('viewBox'", i_relayout)
        self.assertLess(i_relayout, i_fallback, "re-layout must be tried before scaling")

    def test_the_print_call_waits_for_the_re_layout(self):
        # Plotly.relayout is async; printing before it settles captures the old width.
        self.assertIn("if (p && p.then) { p.then(function () { window.print(); }); }", _source())

    def test_there_is_no_print_pdf_button(self):
        # Removed: it was the browser's own print dialog, which cannot paginate a flexbox
        # layout, cannot size a Plotly chart to paper, and names the file after the document
        # title. Export Report replaces it.
        src = _source()
        # The BUTTON, not any mention: the comment explaining why it went is worth keeping.
        self.assertNotIn('button("🖨 Print / PDF"', src)
        self.assertNotIn('key="report_print_btn"', src)
        self.assertNotIn("_rfpis_print_now", src)

    def test_the_export_button_says_export_not_build(self):
        # Whether we "build" anything is our concern; the user is exporting a report.
        src = _source()
        self.assertIn('ac_pdf.button("\U0001F4C4 Export Report"', src)
        # The BUTTON, not any mention — the comment recording why it was renamed stays.
        self.assertNotIn('button("\U0001F4C4 Build PDF"', src)

    def test_the_download_appears_beside_the_button_that_asked_for_it(self):
        # THE reported bug. The document is only complete once the page has drawn, so the
        # download button used to render at the END of a five-section page — several screens
        # below the button just pressed. From the top, Export Report looked like it reran the
        # page and did nothing. A placeholder reserves the spot and is filled at the end.
        src = _source()
        i_button = src.index('ac_pdf.button("\U0001F4C4 Export Report"')
        i_slot = src.index("_pdf_slot = ac_pdf.empty()")
        i_fill = src.index("_pdf_slot.download_button(")
        self.assertLess(i_button, i_slot, "the slot must sit beside the button")
        self.assertLess(i_slot, i_fill, "the slot is filled after the page has rendered")

    def test_ctrl_p_still_gets_the_print_hook(self):
        # The button is gone but the beforeprint hook stays, so anyone reaching for Ctrl+P out
        # of habit still gets charts fitted rather than cut off.
        src = _source()
        self.assertIn("beforeprint", src)
        self.assertIn("rfpis-print-hook", src)

    def test_the_hook_id_is_versioned_and_replaces_older_ones(self):
        # THE reason the button was dead. The previous guard was
        # `if (getElementById('rfpis-print-hook')) return;`, so once a browser had loaded an
        # older build the parent kept that older hook forever and the newer one never installed.
        # The button then posted a message nothing listened for, and clicking it did nothing.
        src = _source()
        self.assertRegex(src, r"RFPIS_HOOK_ID = 'rfpis-print-hook-v\d+'")
        self.assertIn("""querySelectorAll('[id^="rfpis-print-hook"]')""", src)
        self.assertIn("stale[i].remove()", src)


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
        wanted = ["1 · Scan activity",
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

    def test_the_charts_actually_build(self):
        # The probe supplies non-empty tables precisely so the figure-building code runs. Every
        # chart sits inside an `if not frame.empty` branch, so on empty tables the headings
        # render and every figure is skipped — a run that proves almost nothing about the
        # palette, category orders and column references.
        self.assertGreater(self.result["n_charts"], 10, "charts were not built")

    def test_donor_decisions_renders_when_there_is_data(self):
        # The heading now distinguishes the funder's response from the team's own decision — the
        # two were both called "decisions" and the section read ambiguously.
        self.assertIn("Donor decisions", self.result["markdown"])
        self.assertIn("funder's response", self.result["markdown"])

    def test_the_section_that_moved_renders_its_own_subsections(self):
        # The block move is the risky change: a name defined by a section it jumped over would
        # raise, and a raised exception renders as blank space rather than an error.
        body = self.result["markdown"]
        self.assertIn("Team Touchpoints", body)
        self.assertIn("Lead & Sub Applicant partners", body)


class TheDecisionRowTests(unittest.TestCase):
    """Decision Distribution and the Proceed-decisions time series share one row."""

    def test_the_time_series_gets_at_least_three_quarters_of_the_row(self):
        # 1:3 -> the monthly series takes 75%. It is a run across the whole period and its
        # buckets crowd at half width; the four-bar distribution does not need the space.
        self.assertIn("st.columns([1, 3], gap=\"medium\")", _source())

    def test_the_distribution_is_vertical_now(self):
        src = _source()
        block = src[src.index("fig_dec = px.bar("):src.index("if _show(\"s3_dectime\")")]
        self.assertIn('x="decision"', block)
        self.assertIn('y="count"', block)
        self.assertNotIn('orientation="h"', block)

    def test_either_chart_alone_takes_the_full_width(self):
        # Otherwise turning one off leaves a quarter-row chart with a gap beside it.
        src = _source()
        self.assertIn("elif fig_dec is not None:", src)
        self.assertIn("elif fig_time is not None:", src)

    def test_the_distribution_is_shaded_in_a_fixed_semantic_order(self):
        # Frequency order would move the darkest shade onto whichever decision dominates, so the
        # same colour would mean something different from one report to the next.
        src = _source()
        self.assertIn('category_orders={"decision": _theme.DECISION_ORDER}', src)


class EveryChartIsFramedTests(unittest.TestCase):
    def test_nothing_renders_a_chart_outside_the_frame_helper(self):
        # One bare `st.plotly_chart` would sit unframed beside framed neighbours.
        src = _source()
        calls = [ln for ln in src.splitlines() if "st.plotly_chart(" in ln]
        self.assertEqual(len(calls), 1, f"unframed chart calls: {calls}")
        self.assertIn("def _boxed(", src)

    def test_the_frame_helper_applies_the_shared_style(self):
        src = _source()
        boxed = src[src.index("def _boxed("):src.index("def _show_sec(")]
        self.assertIn("_theme.style(fig)", boxed)
        self.assertIn("st.container(border=True)", boxed)


class OnePaletteTests(unittest.TestCase):
    """The old report used a different palette per chart. Any raw hex in a colour argument means
    one has crept back."""

    def test_no_chart_names_its_own_colours(self):
        src = _source()
        offenders = []
        for ln in src.splitlines():
            if ("color_discrete_map" in ln or "color_discrete_sequence" in ln
                    or "marker_color" in ln) and re.search(r"#[0-9a-fA-F]{6}", ln):
                offenders.append(ln.strip())
        self.assertEqual(offenders, [], f"hard-coded chart colours: {offenders}")

    def test_the_deep_blue_is_gone_from_the_page(self):
        # It survived only as the Print button's background, and it was the single deep-blue
        # thing left in the app — the header is greens and neutrals — so it read as a stray.
        self.assertNotIn("003366", _source())


class TheExportButtonTests(unittest.TestCase):
    def test_it_says_export_data(self):
        src = _source()
        self.assertIn('"📥 Export Data",', src)
        self.assertNotIn('"📥 Excel",', src)


class Section1DoesNotDependOnScanLogsTests(unittest.TestCase):
    """The reported disappearance of the keyword cloud.

    Everything after the scan KPIs used to sit inside the `else:` of `if scans.empty:`, so a
    period with no scan_logs rows for the tenant silently took the keyword cloud, the discovery
    timeline, funding-by-donor and cycle time with it — all of which are built from
    rfp_submissions and have nothing to do with whether a scan ran.
    """

    @classmethod
    def setUpClass(cls):
        probe = os.path.join(_ROOT, "tests", "report_page_probe.py")
        env = dict(os.environ, RFPIS_PROBE_EMPTY="scan_logs")
        proc = subprocess.run([sys.executable, probe], cwd=_ROOT, env=env,
                              capture_output=True, text=True, timeout=600)
        marker = "---PROBE---"
        if marker not in proc.stdout:
            raise AssertionError(
                f"probe produced no result (exit {proc.returncode})\n"
                f"stdout tail:\n{proc.stdout[-1500:]}\n"
                f"stderr tail:\n{proc.stderr[-1500:]}")
        cls.result = json.loads(proc.stdout.split(marker, 1)[1].strip().splitlines()[-1])

    def test_the_page_does_not_crash_without_scan_logs(self):
        # Top-sources IS scan-log-derived, and de-nesting it raised KeyError('source') until it
        # got its own guard.
        self.assertEqual(self.result["exceptions"], [])

    def test_the_focus_area_cloud_still_renders(self):
        # The owner renamed this heading from "Focus areas" to "Focus areas"; assert
        # the heading the page actually renders.
        self.assertIn("Focus areas", self.result["markdown"])

    def test_the_charts_still_build(self):
        self.assertGreater(self.result["n_charts"], 10)

    def test_all_five_sections_still_arrive(self):
        self.assertEqual(len(self.result["subheaders"]), 5)


class TheExportFilenameTests(unittest.TestCase):
    """One name for every tenant and period meant downloads collided and a file on disk could
    not be traced back to the report that produced it."""

    def test_it_carries_product_period_tenant_report_id_and_year(self):
        src = _source()
        self.assertIn('def _export_filename(', src)
        self.assertIn('file_name=_export_filename("xlsx")', src)
        block = src[src.index("def _export_filename("):src.index("def _boxed(")]
        for part in ('"RFPIS"', "_period_slug()", "_org", "_url_rid", "year_override"):
            self.assertIn(part, block)

    def test_each_period_selection_gets_its_own_token(self):
        src = _source()
        block = src[src.index("def _period_slug("):src.index("def _slug(")]
        for token in ('"ytd"', '"last90d"', '"last12m"', '"alltime"'):
            self.assertIn(token, block)


class TheReportIdentityTests(unittest.TestCase):
    def test_the_document_title_names_the_tenant_and_period(self):
        # Chrome stamps document.title into the printed page header, which read
        # "RFP Intelligence System - RFPIS" on every tenant's PDF.
        src = _source()
        # The name now carries the CADENCE it was cut at ("Fund-raising Monthly Activity
        # Report"), which is what distinguishes two exports of the same period.
        self.assertIn('_doc_title = f"{_report_name()} · {_period_phrase()}"', src)
        self.assertIn('Fund-raising {_cadence_word()} Activity Report', src)
        self.assertIn("window.RFPIS_DOC_TITLE", src)
        self.assertIn("window.parent.document.title = window.RFPIS_DOC_TITLE", src)

    def test_the_footer_signs_the_report_with_a_name_and_email(self):
        src = _source()
        self.assertIn("Generated by", src)
        block = src[src.index("_gen_user = st.session_state"):src.index("with st.container(key=\"report_footer\")")]
        self.assertIn('_gen_user.get("name")', block)
        self.assertIn('_gen_user.get("email")', block)

    def test_the_signature_comes_from_the_session_not_a_field(self):
        # A typed value could name somebody else.
        src = _source()
        self.assertIn('st.session_state.get("app_user")', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RestoringASnapshotDoesNotHideNewMetricsTests(unittest.TestCase):
    """Why charts "went missing".

    A snapshot records the metrics that were ON, not which metrics EXISTED. Every metric added
    to the report after a snapshot was saved was therefore absent from it, and reopening that
    snapshot brought the checkbox back unticked and the chart quietly gone. Six of the live
    snapshots were in exactly that state.
    """

    def test_a_metric_the_snapshot_never_knew_about_is_shown(self):
        from core.report_snapshots import restore_items
        got = restore_items(["a", "b"], {"a", "b", "c"})
        self.assertEqual(got, {"a", "b", "c"})

    def test_a_deliberate_de_selection_is_respected_when_the_universe_is_known(self):
        from core.report_snapshots import restore_items
        got = restore_items(["a", "c"], {"a", "b", "c"}, ["a", "b", "c"])
        self.assertEqual(got, {"a", "c"})

    def test_nothing_saved_means_everything_on(self):
        from core.report_snapshots import restore_items
        self.assertEqual(restore_items(None, {"a", "b"}), {"a", "b"})

    def test_keys_that_no_longer_exist_are_dropped(self):
        from core.report_snapshots import restore_items
        self.assertEqual(restore_items(["a", "gone"], {"a", "b"}, ["a", "b"]), {"a"})

    def test_new_snapshots_record_their_key_universe(self):
        # Without this the ambiguity is permanent.
        src = _source()
        self.assertIn('"all_items": sorted(_ALL_KEYS)', src)
        self.assertIn("restore_items(_items_saved, _ALL_KEYS, _sel(\"all_items\"))", src)


class ChartWordingTests(unittest.TestCase):
    """"RFPs" excludes calls for proposals and every other solicitation type we track."""

    def test_no_chart_title_calls_them_RFPs(self):
        src = _source()
        offenders = []
        for line in src.splitlines():
            if re.search(r"title\s*=\s*f?\"[^\"]*\bRFPs?\b", line):
                offenders.append(line.strip()[:90])
        self.assertEqual(offenders, [], f"chart titles still say RFPs: {offenders}")

    def test_the_member_chart_gives_each_person_their_own_colour(self):
        src = _source()
        self.assertIn("_theme.categorical(", src)
        block = src[src.index("Funding calls discovered by member"):]
        self.assertIn("categorical(", block[:600])


class TheIntakeCardsCloseTheArithmeticTests(unittest.TestCase):
    """"Excel imported 52" could not be reconciled against the 63 records actually imported.

    The tile counted unique rows only, so the 11 the dedupe caught were invisible and anyone
    comparing the report with the workbook found a shortfall with no explanation. Unique stays
    the headline — it is what every other figure in the report counts — with the duplicates
    stated beside it so total = unique + duplicates is visible on the card.
    """

    def _block(self) -> str:
        src = _source()
        return src[src.index("# EVERY RECORD, THE DUPLICATES"):src.index("if scans.empty:")]

    def test_it_counts_all_rows_not_only_the_unique_ones(self):
        block = self._block()
        self.assertIn('agg(total=("dup", "size"), dups=("dup", "sum"))', block)
        # the old version filtered duplicates out before counting anything
        self.assertNotIn('rfps_all[~rfps_all["is_duplicate"]]["source"]', block)

    def test_every_route_reports_its_duplicates(self):
        block = self._block()
        self.assertIn("duplicate{'s' if _d != 1 else ''}", block)
        self.assertIn('delta_color="off"', block)   # a count, not a trend

    def test_a_total_card_leads_the_row(self):
        block = self._block()
        i_total = block.index('"Records ingested"')
        i_routes = block.index("_labels.get(str(_src)")
        self.assertLess(i_total, i_routes)

    def test_unique_remains_the_headline_number(self):
        # Every other figure in the report counts unique rows; leading with the total would make
        # the card disagree with the funnel directly below it.
        block = self._block()
        self.assertIn('_labels.get(str(_src), str(_src).title()), f"{_u:,}"', block)

    def test_the_caption_says_which_number_is_which(self):
        self.assertIn("is the UNIQUE calls kept", _source())
        self.assertIn("duplicates the dedupe removed", _source())

    def test_a_route_with_no_duplicates_shows_no_delta(self):
        # "0 duplicates" on a clean route is noise.
        self.assertIn("if _d else None", self._block())
