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
             mock.patch.object(ps, "launch_probe",
                               return_value=(True, "launches", None)), \
             mock.patch.object(ps.subprocess, "run",
                               side_effect=AssertionError("must not install")):
            ok, msg = ps.ensure_chromium()
        self.assertTrue(ok)
        self.assertIn("/browsers/chrome", msg)

    def test_missing_browser_is_installed_once_then_remembered(self):
        answers = [(False, "/browsers/chrome"), (True, "/browsers/chrome")]
        with mock.patch.object(ps, "probe", side_effect=lambda: answers.pop(0)), \
             mock.patch.object(ps, "launch_probe",
                               return_value=(True, "launches", None)), \
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


class MissingLibraryTests(_Reset):
    """A downloaded browser that cannot link its system libraries is a THIRD failure, with
    a different fix (packages.txt + reboot) from "not installed" (wait for the download)."""

    def test_the_library_name_is_extracted_from_the_launch_error(self):
        err = ("/root/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-"
               "shell: error while loading shared libraries: libglib-2.0.so.0: cannot "
               "open shared object file: No such file or directory")
        self.assertEqual(ps.missing_library(err), "libglib-2.0.so.0")

    def test_unrelated_errors_do_not_produce_a_library_name(self):
        self.assertIsNone(ps.missing_library("Target page crashed"))
        self.assertIsNone(ps.missing_library(None))

    def test_launch_probe_names_the_library_and_points_at_packages_txt(self):
        err = "error while loading shared libraries: libnss3.so: cannot open shared object"
        with mock.patch.object(ps.subprocess, "run",
                               return_value=_completed(stderr=err, code=3)):
            ok, why, lib = ps.launch_probe()
        self.assertFalse(ok)
        self.assertEqual(lib, "libnss3.so")
        self.assertIn("libnss3.so", why)
        self.assertIn("packages.txt", why)

    def test_a_browser_that_exists_but_cannot_launch_is_not_ready(self):
        # And it must NOT be re-downloaded: reinstalling never supplies a system library.
        with mock.patch.object(ps, "probe", return_value=(True, "/browsers/chrome")), \
             mock.patch.object(ps, "launch_probe",
                               return_value=(False, "cannot start",
                                             "libglib-2.0.so.0")), \
             mock.patch.object(ps.subprocess, "run",
                               side_effect=AssertionError("must not install")):
            ok, msg = ps.ensure_chromium()
        self.assertFalse(ok)
        self.assertIn("cannot start", msg)

    def test_status_separates_installed_from_launches(self):
        with mock.patch.object(ps, "probe", return_value=(True, "/browsers/chrome")), \
             mock.patch.object(ps, "launch_probe",
                               return_value=(False, "cannot start",
                                             "libglib-2.0.so.0")):
            st = ps.status()
        self.assertTrue(st["chromium_installed"])
        self.assertFalse(st["chromium_launches"])
        self.assertFalse(st["chromium_ready"])
        self.assertEqual(st["missing_library"], "libglib-2.0.so.0")

    def test_a_healthy_engine_reports_ready(self):
        with mock.patch.object(ps, "probe", return_value=(True, "/browsers/chrome")), \
             mock.patch.object(ps, "launch_probe",
                               return_value=(True, "Chromium launches.", None)):
            st = ps.status()
        self.assertTrue(st["chromium_ready"])


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

    def test_a_launch_failure_mid_render_still_names_the_library(self):
        from core import report_pdf as rp
        err = ("chrome-headless-shell: error while loading shared libraries: "
               "libglib-2.0.so.0: cannot open shared object file")
        with mock.patch.object(ps, "ensure_chromium", return_value=(True, "ok")), \
             mock.patch.object(rp.subprocess, "run",
                               return_value=_completed(stderr=err, code=1)):
            with self.assertRaises(RuntimeError) as caught:
                rp.render_pdf("<html></html>", chart_count=0, header_text="h",
                              footer_text="f")
        msg = str(caught.exception)
        self.assertIn("libglib-2.0.so.0", msg)
        self.assertIn("packages.txt", msg)
        self.assertNotIn("ScreenshotNewSurface", msg)      # not the raw command line

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


class HostLibraryTests(_Reset):
    """Distinguishing "the apt step did nothing" from "one package name is wrong" — the two
    causes that produce an identical `libglib-2.0.so.0` error."""

    def test_non_linux_hosts_are_not_interrogated(self):
        with mock.patch.object(ps.sys, "platform", "win32"):
            self.assertEqual(ps.host_libraries()["checked"], 0)

    def test_libraries_present_in_the_loader_index_are_not_reported_missing(self):
        index = set(ps._REQUIRED_SONAMES)
        with mock.patch.object(ps.sys, "platform", "linux"), \
             mock.patch.object(ps, "_loader_index", return_value=index), \
             mock.patch.object(ps, "_os_release", return_value={"ID": "debian"}):
            out = ps.host_libraries()
        self.assertEqual(out["missing"], [])
        self.assertEqual(out["checked"], len(ps._REQUIRED_SONAMES))

    def test_an_empty_loader_index_reports_every_library_missing(self):
        # The signature of an apt step that never ran: not one or two names, all of them.
        with mock.patch.object(ps.sys, "platform", "linux"), \
             mock.patch.object(ps, "_loader_index", return_value=set()), \
             mock.patch.object(ps, "_os_release", return_value={"ID": "debian"}), \
             mock.patch("ctypes.util.find_library", return_value=None):
            out = ps.host_libraries()
        self.assertEqual(len(out["missing"]), len(ps._REQUIRED_SONAMES))
        self.assertIn("libglib-2.0.so.0", out["missing"])

    def test_packages_txt_is_read_from_the_deployed_tree(self):
        out = ps.packages_txt()
        self.assertTrue(out["present"])
        self.assertIn("libglib2.0-0", out["packages"])
