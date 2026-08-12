"""The inline JS must be valid AS PYTHON EMITS IT, not as the file reads.

This exists because of a bug that took three attempts to find, and that nothing else in the
suite could have caught.

The `components.html(...)` payload was a NON-RAW triple-quoted string. The JS contained

    ].join('\\n');

which the file shows intact and which `node --check` on the file text parses happily. But Python
consumes the escape, so the browser received

    ].join('
    ');

an unterminated string literal. A SyntaxError anywhere in a script stops the WHOLE block from
executing, so nothing in it was ever defined.

The symptoms pointed away from the cause: the Print button rendered (it was plain HTML), showed
no hover (it was not a Streamlit button), and did nothing when clicked (its onclick handler had
never been defined). Nothing in the page could report the error, and reading the source showed
correct code. Two rounds of fixes went into the wrong layer before the emitted text was compared
against the file text.

Payloads are raw strings now, and this checks both that they stay raw and that what Python emits
actually parses. String literals are found through the AST rather than by regex, so wrapping a
payload in concatenation (as the document-title injection does) cannot hide it from this test.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Pages that hand JavaScript to the browser.
_PAGES = ("views/report.py",)


def _script_literals(rel_path: str) -> list[tuple[str, str]]:
    """Every string literal in the file that carries a <script> block.

    Returns (source_text_of_the_literal, runtime_value). The source text is what reveals whether
    the literal was written raw; the runtime value is what the browser receives.
    """
    path = os.path.join(_ROOT, rel_path)
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "<script>" in node.value:
                segment = ast.get_source_segment(src, node) or ""
                out.append((segment, node.value))
    return out


class ThePayloadsAreRawStringsTests(unittest.TestCase):
    """A non-raw payload silently eats JS escapes. A raw-string prefix costs one character and
    removes the entire failure mode.

    (Note for the next editor: do not write a raw-string prefix followed by triple quotes inside
    a docstring — it terminates the docstring. That is the same family of mistake this file is
    about, and it happened while writing it.)
    """

    def test_every_multiline_script_literal_is_raw(self):
        for page in _PAGES:
            found = _script_literals(page)
            self.assertTrue(found, f"no script literal found in {page} — this test would pass "
                                   f"vacuously")
            for n, (segment, value) in enumerate(found):
                # Single-line helpers (the one-line title injection) carry no escapes worth
                # protecting; the multi-line blocks are the risk.
                if value.count("\n") < 3:
                    continue
                with self.subTest(page=page, literal=n):
                    self.assertTrue(segment.lstrip().startswith(("r\"\"\"", "r'''")),
                                    f"{page} literal {n} is not raw; Python will consume any JS "
                                    f"escape inside it. Starts: {segment[:24]!r}")


class WhatPythonEmitsIsValidJavaScriptTests(unittest.TestCase):
    def _emitted_scripts(self):
        for page in _PAGES:
            for n, (_segment, value) in enumerate(_script_literals(page)):
                for m, block in enumerate(re.findall(r"<script>(.*?)</script>", value, re.S)):
                    if block.strip():
                        yield f"{page}#{n}.{m}", block

    def test_no_javascript_string_literal_spans_a_line_break(self):
        # The precise shape of the bug: a real newline inside '...'.
        for name, block in self._emitted_scripts():
            for ln, line in enumerate(block.split("\n"), 1):
                if line.strip().startswith("//"):
                    continue
                with self.subTest(script=name, line=ln):
                    self.assertEqual(len(re.findall(r"(?<!\\)'", line)) % 2, 0,
                                     f"unbalanced quote in {name} line {ln}: {line.strip()!r}")

    def test_the_emitted_javascript_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        checked = 0
        for name, block in self._emitted_scripts():
            with self.subTest(script=name):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                                 encoding="utf-8") as fh:
                    fh.write(block)
                    path = fh.name
                try:
                    proc = subprocess.run([node, "--check", path],
                                          capture_output=True, text=True, timeout=60)
                    self.assertEqual(proc.returncode, 0,
                                     f"{name} does not parse as emitted:\n{proc.stderr}")
                    checked += 1
                finally:
                    os.unlink(path)
        self.assertGreater(checked, 0, "no script was actually parsed")

    def test_something_was_actually_checked(self):
        self.assertGreater(len(list(self._emitted_scripts())), 0)


class NoLiteralScriptTagInsideInlineScriptTests(unittest.TestCase):
    """A literal script tag in script text — even inside a JS comment — ends the element as far
    as the HTML parser is concerned, so the rest stops being script. This was a SECOND, separate
    defect found while chasing the first."""

    def test_no_script_tag_appears_inside_a_script_body(self):
        for name, block in WhatPythonEmitsIsValidJavaScriptTests()._emitted_scripts():
            with self.subTest(script=name):
                self.assertNotRegex(block, r"<\s*/?\s*script",
                                    f"{name} contains a literal script tag")


if __name__ == "__main__":
    unittest.main(verbosity=2)
