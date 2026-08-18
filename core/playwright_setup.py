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

3. A DOWNLOADED BROWSER IS NOT A WORKING BROWSER. Chromium links against ~20 system
   libraries a slim container does not ship, and the failure arrives as `error while
   loading shared libraries: libglib-2.0.so.0: cannot open shared object file` buried under
   a 40-line command line — a message that never mentions Playwright. So the bootstrap
   TEST-LAUNCHES the browser, not merely checks the file exists, and names the missing
   library. The libraries themselves come from `packages.txt` at the repository root, which
   is how Streamlit Community Cloud installs apt packages; the app must be rebooted after
   that file changes.

Everything is best-effort and returns a reason instead of raising: a missing browser must
degrade to "this export is unavailable, here is why", never take a page down.
"""
from __future__ import annotations

import logging
import os
import re
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


_LAUNCH_PROBE = """
import sys
from playwright.sync_api import sync_playwright
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        browser.close()
except Exception as exc:
    print("ERR", exc)
    sys.exit(3)
print("OK")
"""


def missing_library(text: str | None) -> str | None:
    """The shared object a failed launch complained about, if that is what went wrong.

    A downloaded browser is not a working browser: Chromium links against ~20 system
    libraries that a slim container does not ship, and the failure surfaces as
    `error while loading shared libraries: libglib-2.0.so.0: cannot open shared object
    file` — a message that says nothing about Playwright, so it reads like a mystery
    crash unless it is named."""
    if not text:
        return None
    match = re.search(r"error while loading shared libraries: ([^:]+): cannot open", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"([\w.+-]+\.so[\w.]*): cannot open shared object file", text)
    return match.group(1).strip() if match else None


def launch_probe() -> tuple[bool, str, str | None]:
    """Actually start and stop a browser: `(ok, reason, missing_library)`.

    The library is returned SEPARATELY rather than left for callers to re-parse out of the
    reason: the reason is a sentence written for a human, and re-extracting a filename from
    prose is the kind of round trip that silently yields None.

    Existence is not readiness. This costs a second or two, runs once per process, and is
    the difference between "your export failed, here is a 40-line Chromium command line"
    and "this host is missing libglib-2.0.so.0"."""
    try:
        out = subprocess.run([sys.executable, "-c", _LAUNCH_PROBE], capture_output=True,
                             text=True, timeout=_PROBE_TIMEOUT, env=child_env())
    except Exception as exc:
        return False, f"could not test-launch Chromium ({type(exc).__name__}: {exc})", None
    text = ((out.stdout or "") + "\n" + (out.stderr or "")).strip()
    if out.returncode == 0 and text.startswith("OK"):
        return True, "Chromium launches.", None
    lib = missing_library(text)
    if lib:
        return False, (
            f"Chromium is installed but cannot start: this host is missing the system "
            f"library {lib}. System packages come from `packages.txt` at the repository "
            f"root (this repo ships one with Chromium's dependencies) — if you are seeing "
            f"this, the app has not been rebooted since that file was added, or the host "
            f"could not install every package in it."), lib
    return False, f"Chromium failed to launch: {text[-600:] or 'no output'}", None


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
            # Present on disk — but a downloaded browser that cannot link its system
            # libraries is not a usable one, and finding that out inside the render (as a
            # wall of Chromium command line) helps nobody. Test-launch it here.
            launched, why, _lib = launch_probe()
            if launched:
                _state.update(ready=True, message=f"Chromium present at {detail}")
                return True, str(_state["message"])
            _state.update(ready=False, message=why)
            return False, why
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
            launched, why, _lib = launch_probe()
            if launched:
                _state.update(ready=True, message=f"Chromium installed at {detail}")
                return True, str(_state["message"])
            # Downloaded fine, still cannot run — the missing-library case, which no
            # amount of re-downloading fixes. Say which library.
            _state.update(ready=False, message=why)
            return False, why
        tail = ((out.stderr or out.stdout or "").strip()[-800:]
                or f"installer exited {out.returncode} with no output")
        msg = (f"Chromium is still not launchable after installing into "
               f"{browsers_path()}.\n{tail}")
        _state.update(ready=False, message=msg)
        return False, msg


# The shared objects chrome-headless-shell links against, in the form the dynamic loader
# names them. Package names differ across base images (Ubuntu 24.04 and Debian 13 renamed
# five of them with a `t64` suffix); SONAMEs do not, which is why the check is done at this
# level — it answers "can the loader find it" without needing to know the distro.
_REQUIRED_SONAMES = (
    "libglib-2.0.so.0", "libnss3.so", "libnspr4.so", "libatk-1.0.so.0",
    "libatk-bridge-2.0.so.0", "libatspi.so.0", "libcups.so.2", "libdbus-1.so.3",
    "libdrm.so.2", "libgbm.so.1", "libxkbcommon.so.0", "libpango-1.0.so.0",
    "libcairo.so.2", "libasound.so.2", "libX11.so.6", "libxcb.so.1",
    "libXcomposite.so.1", "libXdamage.so.1", "libXext.so.6", "libXfixes.so.3",
    "libXrandr.so.2",
)


def _os_release() -> dict[str, str]:
    """`/etc/os-release` as a dict — the base image, which decides package NAMES."""
    out: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    key, _, value = line.partition("=")
                    out[key.strip()] = value.strip().strip('"')
    except Exception:
        pass
    return {k: out[k] for k in ("ID", "VERSION_ID", "VERSION_CODENAME", "PRETTY_NAME")
            if k in out}


def _loader_index() -> set[str]:
    """Every SONAME the dynamic loader knows about, from `ldconfig -p`."""
    try:
        out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, timeout=30)
    except Exception:
        return set()
    names = set()
    for line in (out.stdout or "").splitlines():
        head = line.strip().split(" ", 1)[0]
        if head.startswith("lib"):
            names.add(head)
    return names


def host_libraries() -> dict[str, object]:
    """Which of Chromium's libraries this host can actually load.

    The point is to separate two indistinguishable-from-the-error causes:
      * EVERY library missing — the apt step never ran, or aborted (apt installs nothing if
        one name in the list is unresolvable), so `packages.txt` had no effect;
      * ONE OR TWO missing — the list is nearly right and needs those names for this image.
    Non-Linux hosts report `checked: 0`; the question is meaningless there."""
    if not sys.platform.startswith("linux"):
        return {"platform": sys.platform, "checked": 0}
    index = _loader_index()
    missing = []
    for soname in _REQUIRED_SONAMES:
        if soname in index:
            continue
        try:
            import ctypes.util
            if ctypes.util.find_library(soname.split(".so")[0][3:]):
                continue                      # loader index unavailable but the lib is there
        except Exception:
            pass
        missing.append(soname)
    return {"os_release": _os_release(),
            "checked": len(_REQUIRED_SONAMES),
            "missing": missing,
            "loader_index_read": bool(index)}


def packages_txt() -> dict[str, object]:
    """Is `packages.txt` actually in the DEPLOYED tree, and what does it ask for?

    A file that exists in the repository but not in the running deployment explains an apt
    step that appears to have done nothing — and is a fact only the deployment can report."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "packages.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            entries = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
        return {"present": True, "entries": len(entries), "packages": entries}
    except Exception:
        return {"present": False, "entries": 0, "packages": []}


def status() -> dict[str, object]:
    """A snapshot for the deployment diagnostics — no side effects, no download. Reports
    both halves separately, because "the file is there" and "it runs" fail for completely
    different reasons and have completely different fixes."""
    ok, detail = probe()
    out: dict[str, object] = {"browsers_path": browsers_path(),
                              "chromium_installed": ok, "detail": detail}
    if ok:
        launched, why, lib = launch_probe()
        out["chromium_launches"] = launched
        out["launch_detail"] = why
        out["missing_library"] = lib
    else:
        out["chromium_launches"] = False
        out["launch_detail"] = "not installed yet"
    out["chromium_ready"] = bool(ok and out["chromium_launches"])
    out["host_libraries"] = host_libraries()
    out["packages_txt"] = packages_txt()
    return out
