"""The shareable PDF is BUILT, not printed.

Three rounds of print CSS could not produce a document fit to send to anyone, and the reason was
structural rather than a missing rule:

  * every Streamlit ancestor is a flex container, and Chrome will not honour `break-inside`
    inside flexbox — so charts split across page boundaries whatever CSS was added. The selector
    used for the last attempt, `stVerticalBlockBorderWrapper`, does not even exist in this
    Streamlit version, so it matched nothing.
  * Plotly bakes a pixel width into its SVG at render time; fitting that to paper afterwards
    either scales it (11px axis text lands at ~5pt — the "blurry" report, on a PDF that was
    vector throughout) or re-lays it out asynchronously as the print dialog opens.
  * the page carries app furniture a reader should never receive.

So the document is our own HTML, in normal block flow, rendered by headless Chromium. These
tests cover the collection (which must not duplicate the page's aggregations), the document
structure, and — once — a real render.
"""
from __future__ import annotations

import os
import shutil
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import report_pdf as rp                          # noqa: E402


def _fig(title="A chart", height=300):
    import plotly.express as px
    f = px.bar(x=[3, 2, 1], y=["a", "b", "c"], orientation="h", title=title)
    f.update_layout(height=height)
    return f


class CollectingWhatThePageRenderedTests(unittest.TestCase):
    """The PDF must not recompute anything — one source of truth for the numbers."""

    def test_a_chart_is_stored_at_page_width_not_browser_width(self):
        doc = rp.Document()
        doc.chart(_fig())
        import json
        spec = json.loads(doc.blocks[0].fig_json)
        self.assertEqual(spec["layout"]["width"], rp.CONTENT_PX)
        self.assertFalse(spec["layout"]["autosize"],
                         "autosize would let Plotly re-measure against a viewport")

    def test_the_page_width_fits_a4_landscape_inside_its_margins(self):
        # Landscape: portrait forced charts and wide tables into a column narrower than they
        # were designed for, which is what pushed labels into each other.
        self.assertLess(rp.CONTENT_PX, 1123)         # A4 landscape at 96dpi
        self.assertGreater(rp.CONTENT_PX, 900)

    def test_a_figure_that_cannot_be_serialised_is_skipped_not_fatal(self):
        doc = rp.Document()
        doc.chart(object())                          # not a figure
        self.assertEqual(doc.chart_count, 0)

    def test_kpi_tiles_are_grouped_into_the_row_they_were_rendered_as(self):
        doc = rp.Document()
        doc.section("S")
        for i in range(4):
            doc.metric(f"K{i}", i)
        doc.chart(_fig())
        kinds = [b.kind for b in doc.blocks]
        self.assertEqual(kinds, ["section", "kpis", "chart"])
        self.assertEqual(len(doc.blocks[1].items), 4)

    def test_a_trailing_row_is_not_lost(self):
        # Metrics at the very end have no following block to flush them.
        doc = rp.Document()
        doc.metric("Only", 1)
        self.assertEqual([b.kind for b in doc.finish().blocks], ["kpis"])

    def test_a_blank_label_is_not_a_tile(self):
        doc = rp.Document()
        doc.metric("", 5)
        doc.metric(None, 5)
        self.assertEqual(len(doc.finish().blocks), 0)

    def test_a_missing_value_reads_as_a_dash(self):
        doc = rp.Document()
        doc.metric("Median days", None)
        self.assertEqual(doc.finish().blocks[0].items[0], ("Median days", "—"))


class TheDocumentStructureTests(unittest.TestCase):
    def _html(self):
        doc = rp.Document()
        doc.section("1 · Scan activity", "A caption.")
        doc.metric("Runs", 11)
        doc.sub("Intake")
        doc.chart(_fig("Intake by source"))
        doc.section("2 · Team")
        doc.chart(_fig("Second"))
        return rp.build_html(doc.finish(), title="T · Activity Report", subtitle="YTD 2026",
                             meta={"Tenant": "T", "Report id": "r1"})

    def test_it_opens_with_a_cover_page(self):
        h = self._html()
        self.assertIn("class='cover'", h)
        self.assertIn("page-break-after: always", h)

    def test_each_section_starts_on_a_fresh_page_except_the_first(self):
        h = self._html()
        self.assertIn(".section-wrap { page-break-before: always", h)
        self.assertIn(".section-wrap:first-of-type { page-break-before: avoid", h)

    def test_blocks_are_atomic_in_normal_block_flow(self):
        # The whole point: no flexbox between the page box and the block.
        h = self._html()
        self.assertIn(".block { page-break-inside: avoid", h)
        self.assertIn(".doc { display: block; }", h)
        self.assertNotIn("display: flex", h)

    def test_a_heading_is_never_the_last_thing_on_a_page(self):
        h = self._html()
        self.assertIn("h2.section", h)
        self.assertRegex(h, r"h2\.section \{[^}]*page-break-after: avoid")
        self.assertRegex(h, r"h3\.sub \{[^}]*page-break-after: avoid")

    def test_every_chart_gets_its_own_container_and_figure(self):
        h = self._html()
        self.assertEqual(h.count("class='chart'"), 2)
        self.assertIn("Plotly.newPlot", h)

    def test_the_renderer_waits_for_the_charts_rather_than_sleeping(self):
        h = self._html()
        self.assertIn("window.__figsDone", h)

    def test_plotly_is_embedded_so_the_document_needs_no_network(self):
        h = self._html()
        self.assertGreater(len(h), 500_000, "plotly.js does not appear to be inlined")

    def test_the_cover_carries_the_metadata_it_was_given(self):
        h = self._html()
        self.assertIn("Tenant", h)
        self.assertIn("Report id", h)

    def test_content_is_escaped(self):
        doc = rp.Document()
        doc.section("<script>x</script>", "&caption")
        h = rp.build_html(doc.finish(), title="<b>t</b>", subtitle="s", meta={})
        self.assertNotIn("<script>x</script>", h)
        self.assertIn("&lt;script&gt;", h)


class ARealRenderTests(unittest.TestCase):
    """One end-to-end render. Slow (headless Chromium), so it is one test, not many."""

    @classmethod
    def setUpClass(cls):
        try:
            import playwright  # noqa: F401
        except Exception:
            raise unittest.SkipTest("playwright not installed")
        cls.doc = rp.Document()
        cls.doc.section("1 · First", "Caption.")
        for i in range(3):
            cls.doc.metric(f"K{i}", i * 10)
        cls.doc.chart(_fig("Chart one"))
        cls.doc.section("2 · Second")
        cls.doc.chart(_fig("Chart two"))
        cls.doc.finish()
        html = rp.build_html(cls.doc, title="Tenant · Activity Report",
                             subtitle="YTD 2026", meta={"Tenant": "Tenant"})
        try:
            cls.pdf = rp.render_pdf(html, chart_count=cls.doc.chart_count,
                                    header_text="Tenant · Activity Report",
                                    footer_text="Generated by tests")
        except Exception as exc:
            raise unittest.SkipTest(f"chromium unavailable: {exc}")

    def test_it_produces_a_pdf(self):
        self.assertTrue(self.pdf.startswith(b"%PDF"))

    def test_it_is_a4_landscape(self):
        from pypdf import PdfReader
        import io as _io
        page = PdfReader(_io.BytesIO(self.pdf)).pages[0]
        self.assertGreater(float(page.mediabox.width), float(page.mediabox.height))
        self.assertAlmostEqual(float(page.mediabox.width), 842, delta=3)

    def test_nothing_is_rasterised_so_nothing_can_look_blurry(self):
        # The old complaint was blur on a PDF that was already vector — undersized type, not
        # resolution. This asserts the new one has no images at all to be blurry.
        from pypdf import PdfReader
        import io as _io
        for page in PdfReader(_io.BytesIO(self.pdf)).pages:
            xo = (page.get("/Resources", {}) or {}).get("/XObject", {}) or {}
            try:
                xo = xo.get_object()
            except Exception:
                continue
            for _n, ref in xo.items():
                self.assertNotEqual(str(ref.get_object().get("/Subtype")), "/Image")

    def test_every_chart_keeps_its_title_on_its_own_page(self):
        # A title without its chart (or the reverse) is the split this replaces.
        from pypdf import PdfReader
        import io as _io
        import re
        text = " ".join((p.extract_text() or "") for p in PdfReader(_io.BytesIO(self.pdf)).pages)
        squished = re.sub(r"\s+", "", text)
        for title in ("Chartone", "Charttwo"):
            self.assertIn(title, squished)

    def test_the_cover_is_its_own_page(self):
        from pypdf import PdfReader
        import io as _io
        pages = PdfReader(_io.BytesIO(self.pdf)).pages
        self.assertGreaterEqual(len(pages), 3)          # cover + 2 sections
        first = (pages[0].extract_text() or "")
        self.assertNotIn("Chart one", first)


class TheFilenameTests(unittest.TestCase):
    """A file that cannot be traced back to its report is not shareable."""

    def test_the_page_names_the_pdf_with_the_agreed_structure(self):
        with open(os.path.join(_ROOT, "views", "report.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('_export_filename("pdf")', src)
        block = src[src.index("def _export_filename("):src.index("def _boxed(")]
        for part in ('"RFPIS"', "_period_slug()", "_url_rid", "year_override"):
            self.assertIn(part, block)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TheLiveDocumentTests(unittest.TestCase):
    """The bug that emptied every PDF of its KPI cards.

    Streamlit re-executes the page with a fresh namespace on every rerun. The metric hook was
    installed once and closed over the FIRST run's Document, so every later render collected its
    tiles into a dead object and produced a PDF with no cards at all. The first run in a process
    looked perfect, which is why it survived an end-to-end test.
    """

    def test_starting_a_document_makes_it_the_live_one(self):
        first = rp.new_document()
        self.assertIs(rp.current(), first)
        second = rp.new_document()
        self.assertIs(rp.current(), second)
        self.assertIsNot(second, first)

    def test_a_hook_that_asks_for_the_live_document_writes_to_the_new_one(self):
        rp.new_document()

        def hook(label, value):                 # what the page's wrapper does
            doc = rp.current()
            if doc is not None:
                doc.metric(label, value)

        hook("First run", 1)
        second = rp.new_document()              # the rerun
        hook("Second run", 2)
        labels = [k for b in second.finish().blocks for k, _ in b.items]
        self.assertEqual(labels, ["Second run"],
                         "the hook wrote into a stale document — this is the cards bug")


class TablesAndFigureLabelsTests(unittest.TestCase):
    def test_a_table_keeps_its_title_above_it(self):
        import pandas as pd
        doc = rp.Document()
        doc.table(pd.DataFrame({"Grant": ["A"], "USD": [1]}), title="Applied grants")
        h = rp.build_html(doc.finish(), title="t", subtitle="s", meta={})
        self.assertIn("<caption>Applied grants</caption>", h)
        self.assertIn("caption-side: top", h)

    def test_a_long_table_is_truncated_with_the_count_kept(self):
        import pandas as pd
        doc = rp.Document()
        doc.table(pd.DataFrame({"n": list(range(100))}), title="Big")
        block = doc.finish().blocks[0]
        self.assertEqual(len(block.rows), rp.TABLE_MAX_ROWS)
        self.assertEqual(block.total_rows, 100)
        h = rp.build_html(doc, title="t", subtitle="s", meta={})
        self.assertIn(f"Showing {rp.TABLE_MAX_ROWS} of 100 rows", h)

    def test_a_figure_title_becomes_a_numbered_caption_below_the_chart(self):
        doc = rp.Document()
        doc.chart(_fig("Intake by source"))
        block = doc.finish().blocks[0]
        self.assertEqual(block.title, "Intake by source")
        import json
        # and it is NOT left inside the plot, competing with the axis labels
        self.assertEqual(json.loads(block.fig_json)["layout"]["title"], {"text": ""})
        h = rp.build_html(doc, title="t", subtitle="s", meta={})
        self.assertIn("Figure 1", h)
        self.assertIn("Intake by source", h)
        self.assertLess(h.index("id='fig0'"), h.index("Figure 1"), "caption must sit BELOW")

    def test_a_chart_is_never_taller_than_a_page(self):
        # Chrome splits an over-tall block regardless of break-inside, which is what left pages
        # opening with bare axis numbers.
        doc = rp.Document()
        doc.chart(_fig(height=1200))
        self.assertLessEqual(doc.blocks[0].height, rp.CHART_MAX_PX)
        self.assertLess(rp.CHART_MAX_PX, rp.CONTENT_H_PX)


class TheOpeningSummaryTests(unittest.TestCase):
    def test_an_intro_leads_the_document(self):
        doc = rp.Document()
        doc.intro("Screened 243 calls, 30 Proceed.")
        doc.section("1 · Scan activity")
        h = rp.build_html(doc.finish(), title="t", subtitle="s", meta={})
        self.assertIn("At a glance", h)
        # Against the section HEADING, not the first mention: the cover's Contents list names
        # every section, so a plain index() finds that instead.
        self.assertLess(h.index("At a glance"),
                        h.index("<h2 class='section'>1 · Scan activity"))

    def test_an_empty_intro_is_not_an_empty_box(self):
        doc = rp.Document()
        doc.intro("")
        self.assertEqual(len(doc.finish().blocks), 0)


class ChartLabelsAreNotClippedTests(unittest.TestCase):
    """Truncated axis labels were the most-reported visual fault, and a fixed margin cannot fix
    them: nothing in the code knows how wide "Bill & Melinda Gates Foundation — UNICEF" renders.
    Plotly measures the text itself when automargin is on, and it also pushes the axis TITLE
    clear of the category labels — the separation that was asked for."""

    def _layout(self, fig):
        import json
        doc = rp.Document()
        doc.chart(fig)
        return json.loads(doc.blocks[0].fig_json)["layout"]

    def test_both_axes_reserve_room_for_their_labels(self):
        layout = self._layout(_fig())
        self.assertTrue(layout["xaxis"]["automargin"])
        self.assertTrue(layout["yaxis"]["automargin"])

    def test_the_fixed_margins_are_small_so_automargin_can_grow_them(self):
        layout = self._layout(_fig())
        self.assertLessEqual(layout["margin"]["l"], 24)

    def test_an_axis_title_stands_off_its_tick_labels(self):
        import plotly.express as px
        fig = px.bar(x=[1, 2], y=["a", "b"], orientation="h",
                     labels={"x": "Funding calls", "y": "Donor"}, title="t")
        layout = self._layout(fig)
        for axis in ("xaxis", "yaxis"):
            title = layout[axis].get("title")
            if isinstance(title, dict) and title.get("text"):
                self.assertGreaterEqual(title.get("standoff", 0), 8)

    def test_print_type_is_readable_rather_than_minimal(self):
        layout = self._layout(_fig())
        self.assertGreaterEqual(layout["font"]["size"], 10)
        self.assertGreaterEqual(layout["xaxis"]["tickfont"]["size"], 9)


class TheCoverFitsThePageTests(unittest.TestCase):
    def test_the_band_does_not_reach_outside_the_page_box(self):
        # Negative margins pulled it into the unprintable edge, so the first characters of the
        # organisation name were cut off.
        doc = rp.Document()
        doc.section("S")
        h = rp.build_html(doc.finish(), title="An Organisation With A Long Name",
                          subtitle="Year-to-date 2026", meta={"Organization": "X"})
        band = h[h.index(".cover .band {"):h.index("}", h.index(".cover .band {"))]
        self.assertNotIn("-12mm", band)
        self.assertNotIn("margin: -", band)

    def test_a_long_name_wraps_instead_of_overflowing(self):
        doc = rp.Document()
        h = rp.build_html(doc.finish(), title="x", subtitle="y", meta={})
        self.assertIn("overflow-wrap: break-word", h)
