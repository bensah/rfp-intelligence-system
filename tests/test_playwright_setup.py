"""The headless-browser bootstrap behind the PDF export.

`pip install playwright` installs the package, not the browser, and Streamlit Community
Cloud never runs `playwright install` — so the first PDF export on a fresh deployment died
inside Playwright with "Executable doesn't exist at /home/appuser/.cache/ms-playwright/...".
Two failures in one message: nothing had downloaded the browser, and the path it looked in
was a home directory that is not the account the app runs as.

These tests pin both halves: the cache directory is resolved to somewhere PROVEN writable
and handed to every child process, and the download happens once, on demand, reporting why
rather than raising when it can't. No test here downloads anything — every subprocess is
faked.

Run:  python -m unittest tests.test_playwright_setup
"""
import os
import sys
import types
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import playwright_setup as ps      # noqa: E402


def _completed(stdout="", stderr="", code=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


_FAKE_DEFAULT = os.path.join(os.sep, "pw-default")


class _Reset(unittest.TestCase):
    def setUp(self):
        ps._state.update(ready=None, path=None, message="")
        self._env = os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        # Playwright's real platform default is asked for in a subprocess; stub it so no
        # test shells out (or depends on what this machine happens to have installed).
        self._dr = mock.patch.object(ps, "_default_root", return_value=_FAKE_DEFAULT)
        self._dr.start()

    def tearDown(self):
        self._dr.stop()
        ps._state.update(ready=None, path=None, message="")
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        if self._env is not None:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self._env


class BrowsersPathTests(_Reset):
    def test_an_explicit_writable_env_var_wins(self):
        with mock.patch.object(ps, "_writable", return_value=True):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/tmp/operator-choice"
            self.assertEqual(ps.browsers_path(), "/tmp/operator-choice")

    def test_playwrights_own_default_is_respected(self):
        # Overriding a working default would strand the browser a developer already has
        # and force a redundant 150MB download on every machine.
        with mock.patch.object(ps, "_writable", return_value=True), \
             mock.patch.object(ps.os.path, "exists", return_value=True):
            self.assertEqual(ps.browsers_path(), _FAKE_DEFAULT)
        self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], _FAKE_DEFAULT)

    def test_falls_through_to_temp_when_the_default_is_unwritable(self):
        # THE REPORTED BUG: the resolved $HOME belongs to another account, so the default
        # cache can be neither read nor written. Pick somewhere that works.
        import tempfile
        with mock.patch.object(ps, "_writable",
                               side_effect=lambda p: p != _FAKE_DEFAULT), \
             mock.patch.object(ps.os.path, "exists", return_value=False):
            chosen = ps.browsers_path()
        self.assertEqual(chosen, os.path.join(tempfile.gettempdir(), "ms-playwright"))

    def test_operator_pinned_zero_is_left_alone(self):
        # "0" tells Playwright to keep browsers beside the package — a deliberate choice.
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
        with mock.patch.object(ps, "_default_root",
                               side_effect=AssertionError("must not probe")):
            self.assertEqual(ps.browsers_path(), "0")

    def test_resolved_path_is_exported_and_cached(self):
        with mock.patch.object(ps, "_writable", return_value=True):
            first = ps.browsers_path()
            second = ps.browsers_path()
        self.assertEqual(first, second)
        self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], first)
        self.assertEqual(ps._default_root.call_count, 1)   # resolved once, not per call

    def test_child_env_carries_the_path(self):
        with mock.patch.object(ps, "_writable", return_value=True):
            env = ps.child_env()
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], ps.browsers_path())


class ProbeTests(_Reset):
    def test_yes_reports_the_executable(self):
        with mock.patch.object(ps.subprocess, "run",
                               return_value=_completed("YES /browsers/chrome\n")):
            self.assertEqual(ps.probe(), (True, "/browsers/chrome"))

    def test_no_reports_the_missing_path_not_an_error(self):
        with mock.patch.object(ps.subprocess, "run",
                               return_value=_completed("NO /browsers/chrome\n")):
            ok, detail = ps.probe()
        self.assertFalse(ok)
        self.assertEqual(detail, "/browsers/chrome")

    def test_a_broken_probe_is_reported_not_raised(self):
        with mock.patch.object(ps.subprocess, "run", side_effect=OSError("no python")):
            ok, detail = ps.probe()
        self.assertFalse(ok)
        self.assertIn("could not probe", detail)


class EnsureChromiumTests(_Reset):
    def test_present_browser_short_circuits_without_installing(self):
        with mock.patch.object(ps, "probe", return_value=(True, "/browsers/chrome")), \
             mock.patch.object(ps.subprocess, "run",
                               side_effect=AssertionError("must not install")):
            ok, msg = ps.ensure_chromium()
        self.assertTrue(ok)
        self.assertIn("/browsers/chrome", msg)

    def test_missing_browser_is_installed_once_then_remembered(self):
        answers = [(False, "/browsers/chrome"), (True, "/browsers/chrome")]
        with mock.patch.object(ps, "probe", side_effect=lambda: answers.pop(0)), \
             mock.patch.object(ps.subprocess, "run", return_value=_completed()) as run:
            self.assertTrue(ps.ensure_chromium()[0])
            self.assertEqual(run.call_count, 1)
            cmd = run.call_args[0][0]
            self.assertEqual(cmd[1:], ["-m", "playwright", "install", "chromium"])
            self.assertIn("PLAYWRIGHT_BROWSERS_PATH", run.call_args.kwargs["env"])
        with mock.patch.object(ps, "probe",
                               side_effect=AssertionError("must not re-probe")):
            self.assertTrue(ps.ensure_chromium()[0])          # remembered

    def test_failed_install_returns_the_reason_instead_of_raising(self):
        # A browser that cannot be installed is usually a missing system library. Hiding
        # the installer's own words turns a solvable problem into a mystery.
        with mock.patch.object(ps, "probe", return_value=(False, "/browsers/chrome")), \
             mock.patch.object(ps.subprocess, "run",
                               return_value=_completed(stderr="libnss3.so: cannot open",
                                                       code=1)):
            ok, msg = ps.ensure_chromium()
        self.assertFalse(ok)
        self.assertIn("libnss3.so", msg)

    def test_install_that_cannot_start_is_also_reported(self):
        with mock.patch.object(ps, "probe", return_value=(False, "/browsers/chrome")), \
             mock.patch.object(ps.subprocess, "run", side_effect=OSError("denied")):
            ok, msg = ps.ensure_chromium()
        self.assertFalse(ok)
        self.assertIn("failed to start", msg)

    def test_status_never_downloads(self):
        with mock.patch.object(ps, "probe", return_value=(False, "/browsers/chrome")), \
             mock.patch.object(ps.subprocess, "run",
                               side_effect=AssertionError("must not install")):
            st = ps.status()
        self.assertFalse(st["chromium_ready"])
        self.assertIn("browsers_path", st)


class RenderPdfWiringTests(_Reset):
    """core.report_pdf must bootstrap before launching, and pass the path to the child."""

    def test_render_refuses_with_a_human_message_when_the_engine_is_unavailable(self):
        from core import report_pdf as rp
        with mock.patch.object(ps, "ensure_chromium",
                               return_value=(False, "libnss3.so: cannot open")), \
             mock.patch.object(rp.subprocess, "run",
                               side_effect=AssertionError("must not launch a browser")):
            with self.assertRaises(RuntimeError) as caught:
                rp.render_pdf("<html></html>", chart_count=0, header_text="h",
                              footer_text="f")
        msg = str(caught.exception)
        self.assertIn("headless Chromium", msg)
        self.assertIn("Export Data", msg)        # tells them what still works
        self.assertIn("libnss3.so", msg)         # and why it failed

    def test_render_hands_the_browsers_path_to_the_child(self):
        from core import report_pdf as rp
        with mock.patch.object(ps, "ensure_chromium", return_value=(True, "ok")), \
             mock.patch.object(ps, "child_env",
                               return_value={"PLAYWRIGHT_BROWSERS_PATH": "/browsers"}), \
             mock.patch.object(rp.subprocess, "run",
                               return_value=_completed(code=1, stderr="boom")):
            with self.assertRaises(RuntimeError):
                rp.render_pdf("<html></html>", chart_count=0, header_text="h",
                              footer_text="f")
            env = rp.subprocess.run.call_args.kwargs["env"]
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], "/browsers")


if __name__ == "__main__":
    unittest.main(verbosity=2)
