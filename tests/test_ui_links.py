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


class TheTenantRidesAlongTests(unittest.TestCase):
    """A URL is the one route into the app that outlives the session, and a cold load has
    no session to remember which tenant the link was made in. So the slug travels with it.

    It still cannot CHOOSE a tenant for an ordinary user — auth.tenant_context overwrites an
    incoming value rather than reading it (see core.app_header._apply_tenant_url). This is
    about the address staying truthful, not about access."""

    def test_no_session_tenant_means_no_tenant_param(self):
        # The default in every test process: nothing to name, so nothing is added.
        self.assertEqual(links.internal_href("opportunity", uid="AS-1"),
                         "/opportunity?uid=AS-1")

    def test_an_explicit_tenant_is_honoured(self):
        self.assertEqual(links.internal_href("opportunity", uid="AS-1", tenant="client"),
                         "/opportunity?uid=AS-1&tenant=client")

    def test_an_explicit_none_suppresses_it(self):
        self.assertEqual(links.internal_href("opportunity", uid="AS-1", tenant=None),
                         "/opportunity?uid=AS-1")


class TheHandoffTests(unittest.TestCase):
    """st.switch_page cannot carry a query string, so internal_nav parks the values in
    session state and the destination claims them. Claiming must be exactly once: leaving
    them behind would re-open the same opportunity the next time that page was visited."""

    class _SS(dict):
        pass

    def _with_session(self, payload):
        import types
        fake = types.SimpleNamespace(session_state=self._SS(payload))
        return fake

    def test_a_page_claims_only_its_own_handoff(self):
        import sys as _sys
        real = _sys.modules.get("streamlit")
        _sys.modules["streamlit"] = self._with_session(
            {links.NAV_HANDOFF_KEY: {"page": "pipelines", "params": {"uid": "AS-1"}}})
        try:
            self.assertEqual(links.take_handoff("opportunity"), {})
            self.assertEqual(links.take_handoff("pipelines"), {"uid": "AS-1"})
        finally:
            if real is not None:
                _sys.modules["streamlit"] = real

    def test_claiming_clears_it(self):
        import sys as _sys
        real = _sys.modules.get("streamlit")
        fake = self._with_session(
            {links.NAV_HANDOFF_KEY: {"page": "opportunity", "params": {"uid": "AS-1"}}})
        _sys.modules["streamlit"] = fake
        try:
            self.assertEqual(links.take_handoff("opportunity"), {"uid": "AS-1"})
            self.assertEqual(links.take_handoff("opportunity"), {},
                             "a claimed hand-off must not fire twice")
        finally:
            if real is not None:
                _sys.modules["streamlit"] = real

    def test_every_nav_destination_maps_to_a_real_script(self):
        # A typo in PAGE_SCRIPTS degrades internal_nav to an anchor silently, which is
        # exactly the behaviour it exists to replace.
        for slug, script in links.PAGE_SCRIPTS.items():
            with self.subTest(slug=slug):
                self.assertTrue(os.path.exists(os.path.join(_ROOT, script)),
                                f"{slug} -> {script} does not exist")


class TheBackTrailTests(unittest.TestCase):
    """Streamlit has no back button, so the app keeps its own trail. The rules that matter:
    a rerun is not a move, and arriving via Back is not a move — get either wrong and Back
    walks you through your own widget clicks instead of your pages."""

    class _SS(dict):
        pass

    def _install(self, state):
        import sys as _sys
        import types
        self._real = _sys.modules.get("streamlit")
        _sys.modules["streamlit"] = types.SimpleNamespace(session_state=self._SS(state))
        return _sys.modules["streamlit"]

    def tearDown(self):
        import sys as _sys
        if getattr(self, "_real", None) is not None:
            _sys.modules["streamlit"] = self._real

    def test_moving_between_pages_builds_a_trail(self):
        fake = self._install({})
        links.record_visit("report", {})
        links.record_visit("opportunity", {"uid": "AS-1"})
        trail = [e["page"] for e in fake.session_state[links.NAV_HISTORY_KEY]]
        self.assertEqual(trail, ["report", "opportunity"])

    def test_a_rerun_of_the_same_page_is_not_a_move(self):
        fake = self._install({})
        for _ in range(5):
            links.record_visit("report", {})
        self.assertEqual(len(fake.session_state[links.NAV_HISTORY_KEY]), 1)

    def test_the_same_page_with_a_different_uid_is_a_move(self):
        fake = self._install({})
        links.record_visit("opportunity", {"uid": "AS-1"})
        links.record_visit("opportunity", {"uid": "AS-2"})
        self.assertEqual(len(fake.session_state[links.NAV_HISTORY_KEY]), 2)

    def test_arriving_via_back_does_not_re_push(self):
        fake = self._install({links._NAV_BACK_FLAG: True,
                              links.NAV_HISTORY_KEY: [{"page": "report", "params": {}}]})
        links.record_visit("report", {})
        self.assertEqual(len(fake.session_state[links.NAV_HISTORY_KEY]), 1)
        self.assertNotIn(links._NAV_BACK_FLAG, fake.session_state,
                         "the flag must be consumed, or the next real move is swallowed")

    def test_the_tenant_param_is_not_part_of_identity(self):
        # ?tenant= is stamped on every page by the header, so including it would make an
        # ordinary rerun look like a move to a different place.
        fake = self._install({})
        links.record_visit("report", {})
        links.record_visit("report", {"tenant": "client"})
        self.assertEqual(len(fake.session_state[links.NAV_HISTORY_KEY]), 1)

    def test_the_trail_is_capped(self):
        fake = self._install({})
        for i in range(60):
            links.record_visit("opportunity", {"uid": f"AS-{i}"})
        self.assertLessEqual(len(fake.session_state[links.NAV_HISTORY_KEY]),
                             links._HISTORY_CAP)

    def test_every_titled_page_is_a_navigable_page(self):
        for slug in links.PAGE_TITLES:
            with self.subTest(slug=slug):
                self.assertIn(slug, links.PAGE_SCRIPTS)
