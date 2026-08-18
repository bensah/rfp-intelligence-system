"""Build the report as a PDF DOCUMENT, instead of printing the app page.

WHY THIS EXISTS. Print CSS cannot produce a shareable file from this page, and the reason is
structural rather than a missing rule:

  * every Streamlit ancestor is a flex container, and Chrome does not honour `break-inside`
    inside flexbox — so charts split across page boundaries whatever CSS is added.
  * Plotly bakes a pixel width into its SVG at render time. Fitting that to paper afterwards
    either scales it (11px axis text lands at ~5pt, which reads as "blurry" on a PDF that is
    vector throughout) or re-lays it out asynchronously while the print dialog is opening.
  * the page carries app furniture a reader should never receive, and the browser names the file
    after the document title.

So this builds our own HTML, in normal block flow, and renders it with headless Chromium.

LANDSCAPE. The report is charts and wide tables; portrait forced both into a column narrower
than they were designed for, which is what pushed labels into each other. Landscape gives about
1030px of content width — close enough to the on-screen width that a chart needs no reflowing and
a ten-column table fits without wrapping.

The content is COLLECTED from the page as it renders, so the aggregations are not duplicated —
one source of truth for the numbers, two presentations.
"""
from __future__ import annotations

import html as _html
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

# A4 LANDSCAPE at 96dpi, minus the margins below.
PAGE_W_MM = 297
PAGE_H_MM = 210
MARGIN_MM = 12
CONTENT_PX = int((PAGE_W_MM - 2 * MARGIN_MM) / 25.4 * 96)      # ≈ 1032px
CONTENT_H_PX = int((PAGE_H_MM - 2 * MARGIN_MM - 12) / 25.4 * 96)   # ≈ 665px, less header/footer

# A chart taller than the page cannot be kept whole, and Chrome then splits it regardless of
# `break-inside` — which is what left pages opening with bare axis numbers. Clamped so every
# chart fits under its own heading with room to spare.
CHART_MIN_PX = 200
CHART_MAX_PX = 470

# Rows beyond this are dropped with a stated count. A 200-row table is a data export, not
# something a reader takes in on paper, and silently printing 40 pages of it is worse.
TABLE_MAX_ROWS = 18


@dataclass
class Block:
    kind: str   # "intro"|"section"|"sub"|"kpis"|"chart"|"table"|"image"|"note"
    title: str = ""
    body: str = ""
    items: list = field(default_factory=list)
    fig_json: str = ""
    height: int = 320
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    total_rows: int = 0


@dataclass
class Document:
    """What the page collected, in the order it rendered.

    KPI tiles arrive one `st.metric` at a time, in groups the page lays out as a row. They are
    buffered and flushed when the next heading, chart or table arrives, which recovers the
    grouping without the page having to declare it.
    """

    blocks: list[Block] = field(default_factory=list)
    _pending: list = field(default_factory=list)

    def _flush(self) -> None:
        if self._pending:
            self.blocks.append(Block("kpis", items=list(self._pending)))
            self._pending.clear()

    def row_break(self) -> None:
        """End the current KPI row.

        Tiles are buffered until the next heading or chart, so two consecutive `st.columns` rows
        of metrics arrive as one long run and wrap wherever the page width happens to fall. The
        page calls this between them when the grouping carries meaning — counts and the ratio they
        produce on one row, the money on the next.
        """
        self._flush()

    def section(self, title: str, caption: str = "") -> None:
        self._flush()
        self.blocks.append(Block("section", title=title, body=caption))

    def sub(self, title: str) -> None:
        self._flush()
        self.blocks.append(Block("sub", title=title))

    def metric(self, label: str, value) -> None:
        text = str(label or "").strip()
        if text:
            self._pending.append((text, "—" if value is None else str(value)))

    def note(self, text: str) -> None:
        self._flush()
        if text:
            self.blocks.append(Block("note", body=str(text)))

    def intro(self, text: str) -> None:
        """The opening summary, before the first section. Reads as prose, not a caption."""
        self._flush()
        if text:
            self.blocks.append(Block("intro", body=str(text)))

    def chart(self, fig, height: int | None = None) -> None:
        """Store the figure as JSON, sized and re-typed for paper.

        The figure's own title is LIFTED OUT and rendered as a caption beneath the chart, the way
        a figure is labelled in a document. Inside the plot it was a third piece of text
        competing with the axis and the value labels; below it, numbered, a reader can refer to
        it ("Figure 4") and the chart itself keeps the whole frame.
        """
        self._flush()
        try:
            import plotly.io as pio
            spec = json.loads(pio.to_json(fig))
        except Exception:
            return
        layout = spec.setdefault("layout", {})
        h = int(height or layout.get("height") or 320)
        h = max(CHART_MIN_PX, min(CHART_MAX_PX, h))
        layout["width"] = CONTENT_PX
        layout["height"] = h
        layout["autosize"] = False
        # Screen type is too large once a chart is one of several on a printed page: labels ran
        # into each other and into the plot area. Stepped down here rather than on the figure the
        # page shows, so the two are independent.
        font = layout.setdefault("font", {})
        font["size"] = 10
        # Lift the title out of the plot; it becomes the caption below.
        _title_obj = layout.get("title") or {}
        caption = ""
        if isinstance(_title_obj, dict):
            caption = str(_title_obj.get("text") or "")
        elif isinstance(_title_obj, str):
            caption = _title_obj
        layout["title"] = {"text": ""}
        for axis in ("xaxis", "yaxis"):
            ax = layout.setdefault(axis, {})
            ax.setdefault("tickfont", {})["size"] = 9
            # AUTOMARGIN is the fix for clipped labels. A fixed left margin cannot know how long
            # "Bill & Melinda Gates Foundation — UNICEF" is, so donor names were cut off and the
            # axis title sat on top of the category labels; on the requested-vs-secured chart the
            # x labels disappeared entirely. Plotly measures the rendered text and reserves the
            # room itself, and it also pushes the axis TITLE clear of the tick labels — which is
            # the separation asked for.
            ax["automargin"] = True
            if isinstance(ax.get("title"), dict):
                ax["title"].setdefault("font", {})["size"] = 10
                ax["title"]["standoff"] = 12
        legend = layout.setdefault("legend", {})
        legend.setdefault("font", {})["size"] = 9
        # Small fixed margins; automargin grows them where the labels need it.
        layout["margin"] = {"t": 12, "b": 20, "l": 20, "r": 16, "pad": 4}
        # Em dashes in a chart title read as a gap at caption size; an en dash is enough.
        self.blocks.append(Block("chart", fig_json=json.dumps(spec), height=h,
                                 title=caption.strip().replace(" — ", " – ")))

    def image(self, fig, title: str = "", height: int = 300) -> None:
        """A matplotlib figure, embedded as a PNG data URI.

        The word cloud is the ONE raster thing in the document, and unavoidably so: it is a
        bitmap by nature. Everything else stays vector. Rendered at 2x for print, so it does not
        look soft next to type that is.
        """
        self._flush()
        try:
            import base64
            import io as _io
            buf = _io.BytesIO()
            fig.savefig(buf, format="png", dpi=200, bbox_inches="tight",
                        facecolor="white", edgecolor="none")
            buf.seek(0)
            data = base64.b64encode(buf.read()).decode("ascii")
        except Exception:
            return
        self.blocks.append(Block("image", title=title, body=data, height=height))

    def table(self, df, title: str = "") -> None:
        """Store a table. Truncated to a readable length, with the count kept."""
        self._flush()
        try:
            frame = df.copy()
            total = int(len(frame))
            cols = [str(c) for c in frame.columns][:12]
            frame = frame[[c for c in frame.columns][:12]].head(TABLE_MAX_ROWS)
            rows = [[("" if v is None else str(v)) for v in rec]
                    for rec in frame.itertuples(index=False, name=None)]
        except Exception:
            return
        if not cols:
            return
        self.blocks.append(Block("table", title=title, columns=cols, rows=rows,
                                 total_rows=total))

    _CONTENT = ("kpis", "chart", "table", "image", "note", "intro")

    def finish(self) -> "Document":
        """Flush pending tiles, then DROP HEADINGS THAT LABEL NOTHING.

        A subsection heading is emitted by the page whether or not the block beneath it has data
        — a metric switched off in the filter, a chart with an empty frame, a table with no rows.
        On screen that costs a line of whitespace nobody notices. In a document it produced
        "Team Touchpoints" immediately followed by "Donor Touchpoints" with nothing between, and
        a "Conversion rates" heading over blank space: labels that promise content and then
        point at nothing.
        """
        self._flush()
        kept: list[Block] = []
        for i, b in enumerate(self.blocks):
            if b.kind == "sub":
                # keep only if real content follows before the next heading
                has_content = False
                for nxt in self.blocks[i + 1:]:
                    if nxt.kind in ("sub", "section"):
                        break
                    if nxt.kind in self._CONTENT:
                        has_content = True
                        break
                if not has_content:
                    continue
            kept.append(b)
        # and a section that ended up with nothing at all
        out: list[Block] = []
        for i, b in enumerate(kept):
            if b.kind == "section":
                has_content = False
                for nxt in kept[i + 1:]:
                    if nxt.kind == "section":
                        break
                    if nxt.kind in self._CONTENT or nxt.kind == "sub":
                        has_content = True
                        break
                if not has_content:
                    continue
            out.append(b)
        self.blocks = out
        return self

    def __len__(self) -> int:
        return len(self.blocks)

    @property
    def chart_count(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "chart")

    @property
    def table_count(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "table")

    @property
    def kpi_count(self) -> int:
        return sum(len(b.items) for b in self.blocks if b.kind == "kpis")


# ── the LIVE document ─────────────────────────────────────────────────────────────────
# Streamlit re-executes the page with a fresh namespace on every rerun, so a hook installed
# once cannot close over "the document" — it would keep appending to the FIRST run's object
# while the page built and rendered a new one. That is exactly what happened: the metric hook
# was installed on the first run, and every later render produced a PDF with no KPI cards at
# all, because the tiles were going into a dead Document. The hook asks for the live one
# instead, every call.
_CURRENT: Document | None = None


def new_document() -> Document:
    """Start a document for this run and make it the live one."""
    global _CURRENT
    _CURRENT = Document()
    return _CURRENT


def current() -> Document | None:
    return _CURRENT


_CSS = """
  @page { size: A4 landscape; margin: %(margin)dmm; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: "Segoe UI", "Source Sans Pro", system-ui, sans-serif;
    color: #1f2a24; font-size: 9.5pt; line-height: 1.45;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
  /* Normal block flow, deliberately: the app page could not paginate because every ancestor
     was a flex container. */
  .doc { display: block; }

  /* ── cover ──────────────────────────────────────────────────────────────────────── */
  .cover { page-break-after: always; break-after: page; }
  /* NO negative margins. Pulling the band outside the page box put the first characters of the
     title in the unprintable edge, so an organisation name lost its first letter or two. It is a
     normal block that spans the content width instead. */
  .cover .band { background: #0E5A70; color: #fff; padding: 24px 26px 20px;
                 margin: 0 0 24px; border-radius: 4px; }
  .cover .eyebrow { font-size: 8.5pt; letter-spacing: .18em; text-transform: uppercase;
                    color: #A5C8D6; margin-bottom: 8px; }
  .cover h1 { font-size: 23pt; margin: 0; font-weight: 700; letter-spacing: -0.01em;
              line-height: 1.18; overflow-wrap: break-word; }
  .cover .sub { font-size: 12pt; color: #D5E7EF; margin-top: 6px; }
  .cover .facts { display: table; width: 100%%; border-spacing: 10px 0; margin-top: 6px; }
  .cover .fact { display: table-cell; width: 25%%; border-top: 2px solid #117996;
                 padding: 8px 2px 0; vertical-align: top; }
  .cover .fact .k { font-size: 7.5pt; letter-spacing: .1em; text-transform: uppercase;
                    color: #64748b; }
  .cover .fact .v { font-size: 11pt; font-weight: 600; margin-top: 3px; color: #1f2a24; }
  .cover .contents { margin-top: 30px; }
  .cover .contents .h { font-size: 8pt; letter-spacing: .14em; text-transform: uppercase;
                        color: #64748b; margin-bottom: 8px; }
  /* No list marker: each section name already carries its own number ("1 · Scan activity"),
     and those are the numbers that match the headings in the body. An <ol> marker on top of
     them reads as "1. 1 · Scan activity", and it also drifts — finish() prunes sections that
     came out empty, so the body can run 1, 2, 4 while the marker keeps counting 1, 2, 3. */
  .cover .contents ol { margin: 0; padding-left: 0; font-size: 10.5pt; list-style: none; }
  .cover .contents li { margin: 3px 0; }
  .cover .foot { margin-top: 30px; font-size: 8pt; color: #64748b; }

  /* ── sections ───────────────────────────────────────────────────────────────────── */
  h2.section { font-size: 14pt; color: #0E5A70; margin: 0 0 4px; font-weight: 700;
               border-bottom: 2px solid #D5E7EF; padding-bottom: 4px;
               page-break-after: avoid; break-after: avoid; }
  .section-wrap { page-break-before: always; break-before: page; }
  .section-wrap:first-of-type { page-break-before: avoid; break-before: avoid; }
  p.caption { color: #5d6b63; font-size: 8.5pt; margin: 4px 0 10px; max-width: 118em; }
  .intro { background: #F6FAFC; border-left: 3px solid #117996; border-radius: 5px;
           padding: 12px 16px; margin: 0 0 14px; font-size: 9.5pt; line-height: 1.55;
           color: #24352c; page-break-inside: avoid; break-inside: avoid; }
  .intro .h { font-size: 8pt; letter-spacing: .12em; text-transform: uppercase;
              color: #0E5A70; font-weight: 700; margin-bottom: 5px; }
  /* Figure label BELOW the chart, as a document labels a figure. */
  p.figcap { font-size: 8.5pt; color: #24352c; margin: 5px 0 0; font-weight: 600; }
  p.figcap .n { color: #117996; }
  h3.sub { font-size: 10.5pt; margin: 12px 0 5px; color: #24352c; font-weight: 700;
           page-break-after: avoid; break-after: avoid; }

  /* Each block is atomic; in block flow Chrome honours this. */
  .block { page-break-inside: avoid; break-inside: avoid; margin: 0 0 10px; }

  /* ── KPI cards ──────────────────────────────────────────────────────────────────── */
  .kpis { display: table; width: 100%%; border-spacing: 6px 0; margin-bottom: 2px; }
  .kpis .row { display: table-row; }
  .kpi { display: table-cell; border: 1px solid #E3EAEE; border-left: 3px solid #117996;
         border-radius: 5px; padding: 6px 9px; vertical-align: top; background: #FAFCFD; }
  .kpi .k { font-size: 7.5pt; color: #5d6b63; line-height: 1.2; }
  .kpi .v { font-size: 14pt; font-weight: 700; color: #0E5A70; margin-top: 1px;
            line-height: 1.1; }

  /* ── charts ─────────────────────────────────────────────────────────────────────── */
  .chart { border: 1px solid #E3EAEE; border-radius: 5px; padding: 4px 6px; background: #fff; }
  img.img { border: 1px solid #E3EAEE; border-radius: 5px; display: block; }

  /* ── tables ─────────────────────────────────────────────────────────────────────── */
  table.data { width: 100%%; border-collapse: collapse; font-size: 8.5pt; }
  table.data caption { text-align: left; font-size: 10.5pt; font-weight: 700; color: #24352c;
                       padding-bottom: 4px; caption-side: top; }
  table.data th { background: #EDF4F7; color: #0E5A70; text-align: left; font-size: 7.5pt;
                  letter-spacing: .04em; text-transform: uppercase; padding: 5px 6px;
                  border-bottom: 1px solid #C9DCE5; }
  table.data td { padding: 4px 6px; border-bottom: 1px solid #EEF3F5; vertical-align: top;
                  word-break: break-word; }
  table.data tr:nth-child(even) td { background: #FAFCFD; }
  .truncated { font-size: 7.5pt; color: #64748b; margin: 3px 0 0; }
  .note { color: #5d6b63; font-size: 8pt; margin: -2px 0 8px; }
""" % {"margin": MARGIN_MM}


def _kpi_rows(items: list, per_row: int = 6) -> list[list]:
    """KPI tiles wrap at `per_row`. Landscape fits six comfortably."""
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


def build_html(doc: Document, *, title: str, subtitle: str, meta: dict[str, str]) -> str:
    """The whole document as one self-contained HTML string."""
    try:
        from plotly.offline import get_plotlyjs
        plotly_js = get_plotlyjs()
    except Exception:
        plotly_js = ""

    sections = [b.title for b in doc.blocks if b.kind == "section"]

    out = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{_html.escape(title)}</title>",
        f"<style>{_CSS}</style>",
        f"<script>{plotly_js}</script>",
        "</head><body><div class='doc'>",
        # cover
        "<section class='cover'><div class='band'>",
        "<div class='eyebrow'>Fund-raising activity report</div>",
        f"<h1>{_html.escape(title)}</h1>",
        f"<div class='sub'>{_html.escape(subtitle)}</div>",
        "</div><div class='facts'>",
    ]
    for k, v in list(meta.items())[:4]:
        out.append(f"<div class='fact'><div class='k'>{_html.escape(k)}</div>"
                   f"<div class='v'>{_html.escape(str(v))}</div></div>")
    out.append("</div>")
    if sections:
        out.append("<div class='contents'><div class='h'>Contents</div><ol>")
        for name in sections:
            out.append(f"<li>{_html.escape(name)}</li>")
        out.append("</ol></div>")
    extra = list(meta.items())[4:]
    if extra:
        out.append("<div class='foot'>"
                   + " &nbsp;·&nbsp; ".join(f"{_html.escape(k)}: {_html.escape(str(v))}"
                                            for k, v in extra)
                   + "</div>")
    out.append("</section>")

    open_section = False
    figs: list[str] = []
    for b in doc.blocks:
        if b.kind == "section":
            if open_section:
                out.append("</div>")
            out.append("<div class='section-wrap'>")
            open_section = True
            out.append(f"<h2 class='section'>{_html.escape(b.title)}</h2>")
            if b.body:
                out.append(f"<p class='caption'>{_html.escape(b.body)}</p>")
        elif b.kind == "sub":
            out.append(f"<h3 class='sub'>{_html.escape(b.title)}</h3>")
        elif b.kind == "note":
            out.append(f"<p class='note'>{_html.escape(b.body)}</p>")
        elif b.kind == "intro":
            out.append("<div class='intro'><div class='h'>At a glance</div>"
                       f"{_html.escape(b.body)}</div>")
        elif b.kind == "kpis":
            for row in _kpi_rows(b.items):
                out.append("<div class='block'><div class='kpis'><div class='row'>")
                for label, value in row:
                    out.append(f"<div class='kpi'><div class='k'>{_html.escape(str(label))}</div>"
                               f"<div class='v'>{_html.escape(str(value))}</div></div>")
                out.append("</div></div></div>")
        elif b.kind == "chart":
            i = len(figs)
            figs.append(b.fig_json)
            out.append(f"<div class='block'><div class='chart' id='fig{i}' "
                       f"style='height:{b.height}px'></div>")
            if b.title:
                out.append(f"<p class='figcap'><span class='n'>Figure {i + 1}:</span> "
                           f"{_html.escape(b.title)}</p>")
            out.append("</div>")
        elif b.kind == "image":
            out.append("<div class='block'>")
            if b.title:
                out.append(f"<h3 class='sub'>{_html.escape(b.title)}</h3>")
            out.append(f"<img class='img' src='data:image/png;base64,{b.body}' "
                       f"style='width:100%;height:auto' />")
            out.append("</div>")
        elif b.kind == "table":
            out.append("<div class='block'><table class='data'>")
            if b.title:
                out.append(f"<caption>{_html.escape(b.title)}</caption>")
            out.append("<thead><tr>"
                       + "".join(f"<th>{_html.escape(c)}</th>" for c in b.columns)
                       + "</tr></thead><tbody>")
            for row in b.rows:
                out.append("<tr>" + "".join(f"<td>{_html.escape(v)}</td>" for v in row) + "</tr>")
            out.append("</tbody></table>")
            if b.total_rows > len(b.rows):
                out.append(f"<p class='truncated'>Showing {len(b.rows)} of {b.total_rows} rows — "
                           f"the full set is in the Export Data workbook.</p>")
            out.append("</div>")

    if open_section:
        out.append("</div>")
    out.append("</div>")

    out.append("<script>window.__figsDone = 0;")
    out.append(f"var FIGS = [{','.join(figs)}];")
    out.append(
        "(function () {"
        "  var opts = {staticPlot: true, displayModeBar: false, responsive: false};"
        "  FIGS.forEach(function (spec, i) {"
        "    Plotly.newPlot('fig' + i, spec.data, spec.layout, opts)"
        "      .then(function () { window.__figsDone++; });"
        "  });"
        "  if (!FIGS.length) { window.__figsDone = -1; }"
        "})();")
    out.append("</script></body></html>")
    return "".join(out)


_RENDER = r"""
import sys
from playwright.sync_api import sync_playwright

html_path, out_path, expected, header, footer = sys.argv[1:6]
expected = int(expected)

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    page.goto("file:///" + html_path.replace("\\", "/"))
    if expected:
        page.wait_for_function("window.__figsDone >= %d" % expected, timeout=180000)
    page.emulate_media(media="print")
    pdf = page.pdf(
        landscape=True, format="A4", print_background=True,
        margin={"top": "13mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
        display_header_footer=True,
        header_template=header,
        footer_template=footer,
        prefer_css_page_size=False,
    )
    browser.close()

open(out_path, "wb").write(pdf)
print("OK", len(pdf))
"""


def render_pdf(html: str, *, chart_count: int, header_text: str,
               footer_text: str, timeout: int = 240) -> bytes:
    """HTML -> PDF bytes, via headless Chromium in a SUBPROCESS.

    A subprocess, not the sync API in-process: Playwright's sync API refuses to run inside an
    asyncio loop, and whether a Streamlit script thread has one is not something a download
    button should depend on. It also means a browser crash cannot take the app with it.
    """
    header = (
        "<div style=\"font-family:'Segoe UI',sans-serif;font-size:7pt;color:#64748b;"
        "width:100%;padding:0 12mm;display:flex;justify-content:space-between\">"
        f"<span>{_html.escape(header_text)}</span><span></span></div>")
    footer = (
        "<div style=\"font-family:'Segoe UI',sans-serif;font-size:7pt;color:#64748b;"
        "width:100%;padding:0 12mm;display:flex;justify-content:space-between\">"
        f"<span>{_html.escape(footer_text)}</span>"
        "<span>Page <span class='pageNumber'></span> of <span class='totalPages'></span></span>"
        "</div>")

    tmpdir = tempfile.mkdtemp(prefix="rfpis_pdf_")
    html_path = os.path.join(tmpdir, "report.html")
    out_path = os.path.join(tmpdir, "report.pdf")
    script_path = os.path.join(tmpdir, "render.py")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(_RENDER)

    # The browser is a separate ~150MB download that pip does not perform, and no host
    # runs `playwright install` for us — so make sure one exists before launching it, and
    # hand the child the SAME browsers path we installed into (its own default resolves
    # from $HOME, which on some hosts is not the account the app runs as).
    from core import playwright_setup
    ready, detail = playwright_setup.ensure_chromium()
    if not ready:
        raise RuntimeError(
            "The PDF engine (headless Chromium) isn't available on this deployment, so the "
            "report can't be rendered here. Everything else on the page still works, and "
            "Export Data gives you the same numbers as a workbook.\n\n" + detail)

    proc = subprocess.run(
        [sys.executable, script_path, html_path, out_path, str(chart_count), header, footer],
        capture_output=True, text=True, timeout=timeout,
        env=playwright_setup.child_env())
    if (proc.returncode != 0 or not os.path.exists(out_path)) and (
            "Executable doesn't exist" in (proc.stderr or "")
            or "playwright install" in (proc.stderr or "")):
        # The browser went missing between the check and the launch — a host that wipes its
        # cache, or a Playwright upgrade demanding a newer build number. Reinstall once and
        # retry, rather than showing the user the same traceback twice.
        ready, detail = playwright_setup.ensure_chromium(force=True)
        if not ready:
            raise RuntimeError("The PDF engine (headless Chromium) could not be restored "
                               "on this deployment.\n\n" + detail)
        proc = subprocess.run(
            [sys.executable, script_path, html_path, out_path, str(chart_count), header,
             footer],
            capture_output=True, text=True, timeout=timeout,
            env=playwright_setup.child_env())
    if proc.returncode != 0 or not os.path.exists(out_path):
        lib = playwright_setup.missing_library((proc.stderr or "") + (proc.stdout or ""))
        if lib:
            raise RuntimeError(
                f"The PDF engine can't start on this deployment: the host is missing the "
                f"system library {lib}. Those come from `packages.txt` at the repository "
                f"root, which this repo ships — the app needs a reboot to pick it up if it "
                f"was added after the last deploy. Export Data gives you the same numbers "
                f"as a workbook in the meantime.")
        raise RuntimeError("PDF render failed.\n"
                           f"stdout: {proc.stdout[-1500:]}\nstderr: {proc.stderr[-1500:]}")
    with open(out_path, "rb") as fh:
        return fh.read()
