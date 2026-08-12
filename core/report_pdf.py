"""Build the report as a PDF DOCUMENT, instead of printing the app page.

WHY THIS EXISTS. Three rounds of print CSS failed to produce a shareable file, and the reason is
structural rather than a missing rule:

  * A Streamlit page is nested FLEXBOX all the way down. Chrome's fragmentation support inside
    flex containers is unreliable, so `break-inside: avoid` does not keep a chart whole. Charts
    split across page boundaries — a title on one page, its bars and value labels on the next.
  * Plotly bakes a pixel width into its SVG at render time. Fitting it to paper afterwards means
    either scaling it (which shrinks 11px axis text to about 5pt — the "blurry" complaint; the
    PDF was vector throughout) or re-laying it out asynchronously while the print dialog is
    already opening.
  * The page carries app furniture — banner, sidebar, toolbars, iframes — that a document handed
    to a reader should not contain.

So this does not print the page. It builds our own HTML document, in normal block flow, at a
known paper size, and renders it with headless Chromium. Page breaks work because there is no
flexbox in the way; charts are laid out AT the page width, so their type is full size and
sharp; and the file gets a real header, footer, page numbers and filename.

The content is collected from the page as it renders, so the aggregations are not duplicated —
one source of truth for the numbers, two presentations.
"""
from __future__ import annotations

import base64
import html as _html
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

# A4 portrait at 96dpi, minus the margins below — the width charts are laid out to, so nothing
# needs scaling afterwards.
PAGE_W_MM = 210
MARGIN_MM = 14
CONTENT_PX = int((PAGE_W_MM - 2 * MARGIN_MM) / 25.4 * 96)      # ≈ 688px


@dataclass
class Block:
    kind: str                       # "section" | "sub" | "kpis" | "chart" | "note"
    title: str = ""
    body: str = ""
    items: list = field(default_factory=list)
    fig_json: str = ""
    height: int = 320


@dataclass
class Document:
    """What the page collected, in the order it rendered.

    KPI tiles arrive one `st.metric` at a time, in groups that the page lays out as a row. They
    are buffered and flushed as one block when the next heading or chart arrives, which recovers
    the grouping without the page having to declare it.
    """

    blocks: list[Block] = field(default_factory=list)
    _pending: list = field(default_factory=list)

    def _flush(self) -> None:
        if self._pending:
            self.blocks.append(Block("kpis", items=list(self._pending)))
            self._pending.clear()

    def section(self, title: str, caption: str = "") -> None:
        self._flush()
        self.blocks.append(Block("section", title=title, body=caption))

    def sub(self, title: str) -> None:
        self._flush()
        self.blocks.append(Block("sub", title=title))

    def metric(self, label: str, value) -> None:
        """One KPI tile. Buffered — see the class docstring."""
        text = str(label or "").strip()
        if text:
            self._pending.append((text, "—" if value is None else str(value)))

    def kpis(self, items: list[tuple[str, str]]) -> None:
        self._flush()
        if items:
            self.blocks.append(Block("kpis", items=list(items)))

    def note(self, text: str) -> None:
        self._flush()
        if text:
            self.blocks.append(Block("note", body=text))

    def chart(self, fig, height: int | None = None) -> None:
        """Store the figure as JSON, sized to the page rather than to the browser window."""
        self._flush()
        try:
            import plotly.io as pio
            spec = json.loads(pio.to_json(fig))
        except Exception:
            return
        layout = spec.setdefault("layout", {})
        h = int(height or layout.get("height") or 320)
        layout["width"] = CONTENT_PX
        layout["height"] = h
        # Autosize would let Plotly re-measure against the viewport; the point here is that the
        # figure is already the right size for the paper.
        layout["autosize"] = False
        self.blocks.append(Block("chart", fig_json=json.dumps(spec), height=h))

    def finish(self) -> "Document":
        """Flush any trailing KPI row. Call before building the HTML."""
        self._flush()
        return self

    def __len__(self) -> int:
        return len(self.blocks)

    @property
    def chart_count(self) -> int:
        return sum(1 for b in self.blocks if b.kind == "chart")


_CSS = """
  @page { size: A4 portrait; margin: %(margin)dmm; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: "Segoe UI", "Source Sans Pro", system-ui, sans-serif;
    color: #1f2a24; font-size: 10.5pt; line-height: 1.45;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
  /* Normal block flow, deliberately. This is what makes break-inside work at all: the app page
     could not paginate because every ancestor was a flex container. */
  .doc { display: block; }
  .cover { page-break-after: always; break-after: page; padding-top: 38mm; }
  .cover h1 { font-size: 26pt; margin: 0 0 6px; color: #0E5A70; letter-spacing: -0.01em; }
  .cover .sub { font-size: 13pt; color: #4A7A96; margin-bottom: 28px; }
  .cover dl { margin: 0; font-size: 10.5pt; }
  .cover dt { color: #64748b; margin-top: 10px; font-size: 9pt;
              text-transform: uppercase; letter-spacing: .06em; }
  .cover dd { margin: 2px 0 0; font-weight: 600; }

  h2.section {
    font-size: 15pt; color: #0E5A70; margin: 0 0 4px;
    border-bottom: 2px solid #D5E7EF; padding-bottom: 5px;
    page-break-after: avoid; break-after: avoid;
  }
  /* A section starts on a fresh page: it is the unit a reader navigates by, and it removes the
     half-empty pages that made the old output look accidental. */
  .section-wrap { page-break-before: always; break-before: page; }
  .section-wrap:first-of-type { page-break-before: avoid; break-before: avoid; }
  p.caption { color: #5d6b63; font-size: 9.5pt; margin: 4px 0 14px; max-width: 62em; }
  h3.sub { font-size: 11.5pt; margin: 16px 0 6px; color: #24352c;
           page-break-after: avoid; break-after: avoid; }

  /* Each block is atomic. In block flow Chrome honours this. */
  .block { page-break-inside: avoid; break-inside: avoid; margin: 0 0 12px; }

  .kpis { display: table; width: 100%%; border-spacing: 6px 0; }
  .kpis .row { display: table-row; }
  .kpi { display: table-cell; width: 25%%; border: 1px solid #E3EAEE; border-left: 3px solid #117996;
         border-radius: 6px; padding: 8px 10px; vertical-align: top; background: #FAFCFD; }
  .kpi .k { font-size: 8.5pt; color: #5d6b63; line-height: 1.25; }
  .kpi .v { font-size: 16pt; font-weight: 700; color: #0E5A70; margin-top: 2px; }

  .chart { border: 1px solid #E3EAEE; border-radius: 6px; padding: 6px 8px; background: #fff; }
  .note { color: #5d6b63; font-size: 9pt; margin: -4px 0 12px; }
""" % {"margin": MARGIN_MM}


def build_html(doc: Document, *, title: str, subtitle: str, meta: dict[str, str]) -> str:
    """The whole document as one self-contained HTML string."""
    try:
        from plotly.offline import get_plotlyjs
        plotly_js = get_plotlyjs()
    except Exception:
        plotly_js = ""

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{_html.escape(title)}</title>",
        f"<style>{_CSS}</style>",
        f"<script>{plotly_js}</script>",
        "</head><body><div class='doc'>",
        "<section class='cover'>",
        f"<h1>{_html.escape(title)}</h1>",
        f"<div class='sub'>{_html.escape(subtitle)}</div><dl>",
    ]
    for k, v in meta.items():
        parts.append(f"<dt>{_html.escape(k)}</dt><dd>{_html.escape(str(v))}</dd>")
    parts.append("</dl></section>")

    open_section = False
    figs: list[str] = []
    for b in doc.blocks:
        if b.kind == "section":
            if open_section:
                parts.append("</div>")
            parts.append("<div class='section-wrap'>")
            open_section = True
            parts.append(f"<h2 class='section'>{_html.escape(b.title)}</h2>")
            if b.body:
                parts.append(f"<p class='caption'>{_html.escape(b.body)}</p>")
        elif b.kind == "sub":
            parts.append(f"<h3 class='sub'>{_html.escape(b.title)}</h3>")
        elif b.kind == "note":
            parts.append(f"<p class='note'>{_html.escape(b.body)}</p>")
        elif b.kind == "kpis":
            parts.append("<div class='block'><div class='kpis'><div class='row'>")
            for label, value in b.items[:4]:
                parts.append(f"<div class='kpi'><div class='k'>{_html.escape(str(label))}</div>"
                             f"<div class='v'>{_html.escape(str(value))}</div></div>")
            parts.append("</div></div></div>")
            # A fifth-plus tile wraps onto its own row rather than being dropped.
            rest = b.items[4:]
            while rest:
                parts.append("<div class='block'><div class='kpis'><div class='row'>")
                for label, value in rest[:4]:
                    parts.append(f"<div class='kpi'><div class='k'>{_html.escape(str(label))}</div>"
                                 f"<div class='v'>{_html.escape(str(value))}</div></div>")
                parts.append("</div></div></div>")
                rest = rest[4:]
        elif b.kind == "chart":
            i = len(figs)
            figs.append(b.fig_json)
            parts.append(f"<div class='block'><div class='chart' id='fig{i}' "
                         f"style='height:{b.height}px'></div></div>")

    if open_section:
        parts.append("</div>")
    parts.append("</div>")

    # Render every figure, then flag readiness so the renderer waits for a settled page rather
    # than a fixed sleep.
    parts.append("<script>")
    parts.append("window.__figsDone = 0;")
    parts.append(f"var FIGS = [{','.join(figs)}];")
    parts.append(
        "(function () {"
        "  var opts = {staticPlot: true, displayModeBar: false, responsive: false};"
        "  FIGS.forEach(function (spec, i) {"
        "    Plotly.newPlot('fig' + i, spec.data, spec.layout, opts)"
        "      .then(function () { window.__figsDone++; });"
        "  });"
        "  if (!FIGS.length) { window.__figsDone = -1; }"
        "})();")
    parts.append("</script></body></html>")
    return "".join(parts)


_RENDER = r"""
import sys, json
from playwright.sync_api import sync_playwright

html_path, out_path, expected, header, footer = sys.argv[1:6]
expected = int(expected)

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 900, "height": 1200})
    page.goto("file:///" + html_path.replace("\\", "/"))
    if expected:
        # Wait for every figure to finish drawing. Printing a half-drawn page is how the old
        # output lost charts.
        page.wait_for_function("window.__figsDone >= %d" % expected, timeout=120000)
    page.emulate_media(media="print")
    pdf = page.pdf(
        format="A4", print_background=True,
        margin={"top": "16mm", "bottom": "14mm", "left": "14mm", "right": "14mm"},
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
               footer_text: str, timeout: int = 180) -> bytes:
    """HTML -> PDF bytes, via headless Chromium in a SUBPROCESS.

    A subprocess, not the sync API in-process: Playwright's sync API refuses to run inside an
    asyncio loop, and whether a Streamlit script thread has one is not something a download
    button should depend on. It also means a browser crash cannot take the app with it.
    """
    header = (
        "<div style=\"font-family:'Segoe UI',sans-serif;font-size:7.5pt;color:#64748b;"
        "width:100%;padding:0 14mm;display:flex;justify-content:space-between\">"
        f"<span>{_html.escape(header_text)}</span>"
        "<span class='title'></span></div>")
    footer = (
        "<div style=\"font-family:'Segoe UI',sans-serif;font-size:7.5pt;color:#64748b;"
        "width:100%;padding:0 14mm;display:flex;justify-content:space-between\">"
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

    proc = subprocess.run(
        [sys.executable, script_path, html_path, out_path, str(chart_count), header, footer],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(
            "PDF render failed.\n"
            f"stdout: {proc.stdout[-1500:]}\nstderr: {proc.stderr[-1500:]}")
    with open(out_path, "rb") as fh:
        return fh.read()
