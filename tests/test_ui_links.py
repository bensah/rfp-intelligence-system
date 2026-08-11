"""In-app navigation stays in the tab; only the funder's own links leave it.

Streamlit renders EVERY markdown link with `target="_blank"`. So
`st.markdown("[Open in Review](/pipelines?uid=…)")` — a move from one page of this app to
another — opened a new browser tab, and a reviewer walking a pipeline collected a tab per
click. Nothing in the code asked for that; it was the default for the markup being used.

The split is about what a link MEANS: an internal link continues a task (same tab, back button
intact), an external one hands the reader to the funder (new tab, so the app isn't lost).
"""
from __future__ import annotations

import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import ui_links as links          # noqa: E402


class TheHrefTests(unittest.TestCase):
    def test_a_bare_path_needs_no_query_string(self):
        self.assertEqual(links.internal_href("pipelines"), "/pipelines")

    def test_a_leading_slash_is_not_doubled(self):
        self.assertEqual(links.internal_href("/pipelines"), "/pipelines")

    def test_params_are_appended(self):
        self.assertEqual(links.internal_href("opportunity", uid="AS-1"),
                         "/opportunity?uid=AS-1")

    def test_a_uid_is_escaped(self):
        # A uid is generated, but the escaping is what stops any value breaking the URL.
        self.assertEqual(links.internal_href("opportunity", uid="a b&c=d"),
                         "/opportunity?uid=a%20b%26c%3Dd")

    def test_blank_params_are_dropped_rather_than_sent_empty(self):
        self.assertEqual(links.internal_href("opportunity", uid=None, other=""),
                         "/opportunity")


class TheLinkOpensInTheSameTabTests(unittest.TestCase):
    def test_an_internal_link_targets_self(self):
        html = links.internal_link("Open in Review", "pipelines", uid="AS-1")
        self.assertIn("target='_self'", html)
        self.assertNotIn("_blank", html)

    def test_an_internal_button_targets_self(self):
        html = links.internal_button("View opportunity", "opportunity", uid="AS-1")
        self.assertIn("target='_self'", html)
        self.assertNotIn("_blank", html)

    def test_the_label_is_escaped(self):
        html = links.internal_link("<script>x</script>", "pipelines")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_bold_wraps_the_label_not_the_anchor(self):
        html = links.internal_link("A Call", "opportunity", bold=True, uid="X")
        self.assertIn("<strong>A Call</strong>", html)
        self.assertTrue(html.startswith("<a "))


class NoInternalLinkUsesMarkdownAnyMoreTests(unittest.TestCase):
    """The regression guard. A markdown link to an app path is the defect itself, so it is
    cheaper to forbid the pattern than to re-find it later."""

    _MD_INTERNAL = re.compile(r"\]\(/(opportunity|pipelines|entity|report|home)\b")

    def _sources(self):
        for folder in ("views", "app_pages"):
            base = os.path.join(_ROOT, folder)
            for name in os.listdir(base):
                if name.endswith(".py") and "sync-conflict" not in name:
                    path = os.path.join(base, name)
                    with open(path, encoding="utf-8") as fh:
                        yield path, fh.read()

    def test_no_markdown_link_points_at_an_app_page(self):
        offenders = []
        for path, src in self._sources():
            for m in self._MD_INTERNAL.finditer(src):
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{os.path.basename(path)}:{line}")
        self.assertEqual(offenders, [],
                         "internal links must use core.ui_links, not markdown: "
                         + ", ".join(offenders))

    def test_the_funders_own_links_still_leave_the_app(self):
        # The other half of the rule: an external link SHOULD open a new tab, so the reader
        # does not lose the review they are in the middle of.
        with open(os.path.join(_ROOT, "app_pages", "opportunity.py"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("Open the call", page)          # rendered as plain markdown → new tab
        self.assertNotIn("internal_link(\"Open the call", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
