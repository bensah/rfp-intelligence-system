"""Helper for rendering a view module inside a Streamlit tab context.

Wrapper pages (e.g. `pages/01_RFPs.py`) call `render_view("screened_rfp")`
inside each `with tab:` block. The view file's body executes inside that tab,
so its `st.*` calls become the tab's contents.

Why exec() rather than `import view; view.render()`:
  * Python caches imports — module body runs ONCE per process. Streamlit needs
    page bodies to run every rerun (so widgets re-register).
  * View files were imperative scripts before this refactor; exec keeps them
    drop-in compatible.

Tracebacks point to the source `views/<name>.py` file (we compile() with the
real path), not "<string>".

Widget-ID collision fix
-----------------------
When two views in the same Streamlit page both call e.g.
`st.selectbox("Review week", ...)` without an explicit `key=`, Streamlit
auto-generates element IDs from (widget_type, label) and they collide,
raising `StreamlitDuplicateElementId`. Putting them in different tabs
doesn't help — tabs share the same page-level ID scope.

The fix monkey-patches every keyed widget for the duration of the exec:
if the caller didn't pass `key=`, we synthesise one from view name +
widget type + label + per-(view, type, label) counter. This is
transparent — widgets with explicit `key=` are passed through unchanged.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VIEWS_DIR = _PROJECT_ROOT / "views"

# Widgets that take an element-id-generating label + optional `key=`.
# Container-style helpers (columns, tabs, container, expander, dialog,
# form, sidebar) intentionally NOT wrapped — they have different semantics.
_WRAPPED_WIDGETS = (
    "selectbox", "multiselect", "text_input", "text_area",
    "number_input", "date_input", "time_input",
    "checkbox", "toggle", "radio",
    "button", "download_button", "link_button",
    "slider", "select_slider", "color_picker",
    "file_uploader", "camera_input",
    "data_editor", "dataframe", "json", "metric",
    "form_submit_button",
)


@lru_cache(maxsize=64)
def _compiled_at(view_name: str, mtime_ns: int):
    """Cache compiled bytecode per (view, mtime). Editing the source file
    changes mtime, which invalidates the cache and forces a recompile."""
    path = _VIEWS_DIR / f"{view_name}.py"
    return compile(path.read_text(encoding="utf-8"), str(path), "exec")


def _compiled(view_name: str):
    """Get compiled bytecode for a view, recompiling automatically on edit."""
    path = _VIEWS_DIR / f"{view_name}.py"
    mtime_ns = path.stat().st_mtime_ns
    return _compiled_at(view_name, mtime_ns)


def _safe_label(args, kwargs) -> str:
    """Best-effort label extractor. First positional arg is the label for
    nearly all widgets; some (like st.metric) put it in `label=`."""
    if args:
        v = args[0]
        if isinstance(v, str):
            return v[:80]
    v = kwargs.get("label")
    if isinstance(v, str):
        return v[:80]
    return ""


def _make_wrapper(original, view_name: str, widget_name: str, counters: dict):
    """Return a wrapper that injects a stable `key=` when caller omits one.

    If the underlying widget doesn't support `key=` (some Streamlit versions
    have it on certain widgets only), we silently retry without the key —
    falling back to Streamlit's native behaviour for that widget.
    """
    def wrapper(*args, **kwargs):
        if "key" in kwargs and kwargs["key"] is not None:
            return original(*args, **kwargs)
        label = _safe_label(args, kwargs)
        ck = (widget_name, label)
        n = counters.get(ck, 0)
        counters[ck] = n + 1
        kwargs["key"] = f"_vw__{view_name}__{widget_name}__{label}__{n}"
        try:
            return original(*args, **kwargs)
        except TypeError as exc:
            # Some widgets (older versions of metric, dataframe, json) may
            # reject `key=`. Retry without it.
            if "key" in str(exc) or "unexpected keyword" in str(exc):
                kwargs.pop("key", None)
                return original(*args, **kwargs)
            raise
    return wrapper


class _ViewStop(Exception):
    """Raised when a view calls (monkey-patched) `st.stop()`. Caught by
    `render_view` so the view aborts gracefully without halting the parent
    page (which would blank out subsequent tabs on the same page)."""
    pass


# ---------------------------------------------------------------------------
# Permanent st.stop patch — bug-resistant against rerun leaks
# ---------------------------------------------------------------------------
# Previously we patched `st.stop` *inside* render_view() and restored it
# in a `finally` block. That broke when `st.rerun()` fired mid-view —
# the rerun aborts the script and Python's finally interaction with
# Streamlit's RerunException occasionally leaves the patch in place. On
# the NEXT navigation, any page that called `st.stop()` (e.g. an Admin
# page rejecting an anonymous visitor) would raise a stray _ViewStop
# that nothing catches → page crash.
#
# Fix: patch `st.stop` ONCE at module load and have it check whether
# we're actually inside a render_view via a depth counter. Outside a
# view context the patched stop delegates to the real one, so behaviour
# matches stock Streamlit. Inside a view it raises _ViewStop so the
# view exits cleanly without halting the parent page.
_REAL_ST_STOP = st.stop
_render_depth = 0


def _view_stop() -> None:
    """Smart st.stop replacement — view-scoped when inside render_view,
    delegates to the real st.stop everywhere else."""
    if _render_depth > 0:
        raise _ViewStop()
    return _REAL_ST_STOP()


# Apply the patch once. Subsequent module imports are no-ops (Python
# caches imports), so this runs exactly one time per process.
st.stop = _view_stop  # type: ignore[assignment]


def render_view(view_name: str) -> None:
    """Execute views/<view_name>.py in the current Streamlit context.

    Each call gets a fresh namespace so variables from one view don't leak
    into another. Widgets without explicit `key=` are auto-keyed under a
    namespace derived from `view_name` to prevent cross-view ID collisions.
    `st.stop()` is intercepted (via the module-level patch above) so a
    view's early-exit doesn't blank out other tabs on the parent page.
    """
    global _render_depth
    code = _compiled(view_name)

    counters: dict = {}
    originals = {}
    for w in _WRAPPED_WIDGETS:
        if not hasattr(st, w):
            continue
        orig = getattr(st, w)
        originals[w] = orig
        setattr(st, w, _make_wrapper(orig, view_name, w, counters))

    _render_depth += 1
    try:
        ns: dict = {
            "__name__": f"views.{view_name}",
            "__file__": str(_VIEWS_DIR / f"{view_name}.py"),
        }
        try:
            exec(code, ns)
        except _ViewStop:
            # View aborted gracefully (empty state, no selection, etc.).
            # Continue executing the rest of the parent page.
            pass
    finally:
        _render_depth -= 1
        # Restore widget wrappers. If a rerun interrupts before this
        # finally runs, the widget wrappers leak — but they only auto-
        # generate `key=` arguments, which is harmless outside a view.
        for w, orig in originals.items():
            setattr(st, w, orig)
