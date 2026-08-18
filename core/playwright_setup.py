"""Make sure a Chromium actually exists on disk before anything tries to launch one.

`pip install playwright` installs the PYTHON package; the browser is a separate ~150MB
download that `playwright install chromium` fetches. Locally that is a documented setup
step. On Streamlit Community Cloud nothing runs it — the host installs requirements.txt and
starts the app — so the first PDF export dies inside Playwright with

    BrowserType.launch: Executable doesn't exist at
    /home/appuser/.cache/ms-playwright/chromium_headless_shell-XXXX/...

Two things are wrong in that message and this module fixes both.

1. NOBODY EVER DOWNLOADED THE BROWSER. `ensure_chromium()` does it on demand, once, and
   only when something actually needs a browser — an export the user asked for is worth a
   one-minute wait; making every cold start pay for it is not.

2. THE PATH IS NOT WHERE THIS PROCESS CAN WRITE. Playwright resolves its cache from
   `$HOME`, and on that host the resolved home (`/home/appuser`) is not the account the app
   runs as (its venv lives under `/home/adminuser`). Installing into a home the process
   cannot write, or reading from one that was never populated, fail the same silent way.
   So the cache directory is RESOLVED — Playwright's own platform default whenever that
   works, a writable fallback only when it doesn't — then exported and handed to every
   child, which makes install and launch agree by construction rather than by luck.
   Overriding the default unconditionally would be its own bug: it strands the browser a
   developer already installed and re-downloads 150MB on every machine.

Everything is best-effort and returns a reason instead of raising: a missing browser must
degrade to "this export is unavailable, here is why", never take a page down.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_state: dict[str, object] = {"ready": None, "path": None, "message": ""}

# The download is large and the host is not fast; a hard failure is better than a hang.
_INSTALL_TIMEOUT = 900
_PROBE_TIMEOUT = 60


def _writable(path: str) -> bool:
    """True when we can create `path` and write inside it."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _default_root() -> str | None:
    """Where Playwright ITSELF would look, asked with the ambient environment and nothing
    forced. Platform-specific (`%LOCALAPPDATA%/ms-playwright` on Windows,
    `~/.cache/ms-playwright` on Linux, `~/Library/Caches/ms-playwright` on macOS), which is
    precisely why it must be read rather than guessed: hard-coding the Linux convention
    would move a developer's working install out from under them and force a redundant
    150MB download. Derived from the executable path Playwright reports —
    `<root>/<browser>-<build>/<platform-dir>/<exe>`. None when it can't be determined."""
    try:
        out = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True,
                             timeout=_PROBE_TIMEOUT)
    except Exception:
        return None
    text = (out.stdout or "").strip()
    if not (text.startswith("YES ") or text.startswith("NO ")):
        return None
    exe = text.split(" ", 1)[1].strip()
    root = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
    return root or None


def browsers_path() -> str:
    """The directory browsers live in — resolved ONCE, then exported as
    `PLAYWRIGHT_BROWSERS_PATH` so the installer and every launcher agree.

    Precedence, most specific first:
      1. an explicit `PLAYWRIGHT_BROWSERS_PATH` that works — the operator's choice wins;
      2. Playwright's OWN default for this platform, whenever it already holds a browser or
         could (i.e. it is writable). Respecting the default is the point: overriding it
         would strand an existing local install and re-download on every developer machine;
      3. a temp directory, only when the default cannot be written. That is the deployed
         case in the reported bug — a `$HOME` belonging to a different account than the one
         running the app. Temp is a real fallback, not a failure: it lives as long as the
         container, which is as long as the process that needs it.

    Note "0" is meaningful to Playwright (browsers stored beside the package), so an
    operator who set it that way is left alone."""
    cached = _state.get("path")
    if cached:
        return str(cached)
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env == "0":
        _state["path"] = env
        return env
    chosen = None
    if env and _writable(env):
        chosen = env
    if chosen is None:
        default = _default_root()
        if default and (os.path.exists(default) or _writable(default)):
            chosen = default
    if chosen is None:
        chosen = os.path.join(tempfile.gettempdir(), "ms-playwright")
        _writable(chosen)                # best-effort create; install reports if it can't
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = chosen
    _state["path"] = chosen
    return chosen


def child_env() -> dict[str, str]:
    """Environment for a subprocess that will launch a browser. Without this the child
    re-resolves the default `$HOME` path and looks in the directory we deliberately did not
    install into — the exact mismatch in the reported traceback."""
    env = dict(os.environ)
    env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path()
    return env


_PROBE = (
    "import os, sys\n"
    "try:\n"
    "    from playwright.sync_api import sync_playwright\n"
    "    with sync_playwright() as p:\n"
    "        path = p.chromium.executable_path\n"
    "except Exception as exc:\n"
    "    print('ERR', type(exc).__name__, exc)\n"
    "    sys.exit(2)\n"
    "print('YES' if os.path.exists(path) else 'NO', path)\n"
)


def probe() -> tuple[bool, str]:
    """`(exists, executable_path_or_reason)` — asked of Playwright itself, in a subprocess.

    Globbing the cache directory would be cheaper and wrong: only the installed Playwright
    version knows which build number it will demand, and that number is what changes when
    a `pip install -U` silently invalidates a cached browser. The subprocess also keeps the
    sync API out of a Streamlit thread that may own an asyncio loop."""
    try:
        out = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True,
                             timeout=_PROBE_TIMEOUT, env=child_env())
    except Exception as exc:
        return False, f"could not probe Playwright ({type(exc).__name__}: {exc})"
    text = (out.stdout or "").strip()
    if text.startswith("YES "):
        return True, text[4:]
    if text.startswith("NO "):
        return False, text[3:]
    return False, (text or (out.stderr or "").strip()[-500:] or "unknown probe failure")


def chromium_ready() -> bool:
    return probe()[0]


def ensure_chromium(*, force: bool = False) -> tuple[bool, str]:
    """Guarantee a launchable Chromium, downloading it once if needed.

    Returns `(ok, message)`; the message is for a human, and on failure carries the tail of
    the installer's own output — a browser that cannot be installed is usually a missing
    system library, and hiding the installer's reason turns a solvable problem into a
    mystery. Serialised by a lock so two exports at once download once, and remembered for
    the life of the process so the common case costs nothing."""
    if _state.get("ready") is True and not force:
        return True, str(_state.get("message") or "Chromium already installed.")
    with _LOCK:
        if _state.get("ready") is True and not force:
            return True, str(_state.get("message") or "Chromium already installed.")
        ok, detail = probe()
        if ok:
            _state.update(ready=True, message=f"Chromium present at {detail}")
            return True, str(_state["message"])
        log.info("playwright: chromium missing (%s) — installing into %s",
                 detail, browsers_path())
        try:
            out = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, text=True, timeout=_INSTALL_TIMEOUT, env=child_env())
        except Exception as exc:
            msg = f"Chromium download failed to start ({type(exc).__name__}: {exc})."
            _state.update(ready=False, message=msg)
            return False, msg
        ok, detail = probe()
        if ok:
            _state.update(ready=True, message=f"Chromium installed at {detail}")
            return True, str(_state["message"])
        tail = ((out.stderr or out.stdout or "").strip()[-800:]
                or f"installer exited {out.returncode} with no output")
        msg = (f"Chromium is still not launchable after installing into "
               f"{browsers_path()}.\n{tail}")
        _state.update(ready=False, message=msg)
        return False, msg


def status() -> dict[str, object]:
    """A snapshot for the deployment diagnostics — no side effects, no download."""
    ok, detail = probe()
    return {"browsers_path": browsers_path(), "chromium_ready": ok, "detail": detail}
