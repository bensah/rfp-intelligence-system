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

So the payloads are RAW strings now, and this test asserts both that they stay raw and that what
Python emits actually parses.
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

# Pages that hand JavaScript to the browser through components.html.
_PAGES = ("views/report.py",)

_PAYLOAD_RE = re.compile(r'components\.html\(\s*(r?""".*?""")', re.S)


def _payloads(rel_path: str) -> list[str]:
    with open(os.path.join(_ROOT, rel_path), encoding="utf-8") as fh:
        return _PAYLOAD_RE.findall(fh.read())


class ThePayloadsAreRawStringsTests(unittest.TestCase):
    """A non-raw payload silently eats JS escapes. A raw-string prefix costs one character and
    removes the entire failure mode.

    (Note for the next editor: do not write a raw-string prefix followed by triple quotes inside
    a docstring — it terminates the docstring. That is the same family of mistake this file is
    about, and it happened while writing it.)
    """

    def test_every_payload_is_raw(self):
        for page in _PAGES:
            found = _payloads(page)
            self.assertTrue(found, f"no components.html payload found in {page} — this test "
                                   f"would pass vacuously")
            for n, lit in enumerate(found):
                with self.subTest(page=page, payload=n):
                    self.assertTrue(lit.startswith('r"""'),
                                    f"{page} payload {n} is not a raw string; Python will "
                                    f"consume any JS escape inside it")


class WhatPythonEmitsIsValidJavaScriptTests(unittest.TestCase):
    def _emitted_scripts(self):
        for page in _PAGES:
            for n, lit in enumerate(_payloads(page)):
                runtime = ast.literal_eval(lit)          # exactly what the browser receives
                for m, block in enumerate(re.findall(r"<script>(.*?)</script>", runtime, re.S)):
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
                finally:
                    os.unlink(path)

    def test_something_was_actually_checked(self):
        self.assertGreater(len(list(self._emitted_scripts())), 0)


class NoLiteralScriptTagInsideInlineScriptTests(unittest.TestCase):
    """A literal script tag in script text — even inside a JS comment — ends the element as far
    as the HTML parser is concerned, so the rest stops being script. This was a SECOND, separate
    defect found while chasing the first."""

    def test_no_script_open_tag_appears_inside_the_script_body(self):
        for name, block in WhatPythonEmitsIsValidJavaScriptTests()._emitted_scripts():
            with self.subTest(script=name):
                self.assertNotRegex(block, r"<\s*script",
                                    f"{name} contains a literal script open tag")

    def test_no_script_close_tag_appears_inside_the_script_body(self):
        for name, block in WhatPythonEmitsIsValidJavaScriptTests()._emitted_scripts():
            with self.subTest(script=name):
                self.assertNotRegex(block, r"<\s*/\s*script")


if __name__ == "__main__":
    unittest.main(verbosity=2)
