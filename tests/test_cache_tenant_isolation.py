"""A process-global cache that reads tenant-scoped data must carry the tenant in its key.

WHAT HAPPENED. The report's loaders took a scope argument precisely to keep tenants apart, and
still served one tenant's rows to another — because the parameter was named `_scope`, and
Streamlit EXCLUDES underscore-prefixed arguments from a cache key. The safeguard was disabled by
its own name, silently, and the docstring above it described protection that was not happening.

It surfaced as a chart: 161 rows from a single "auto-scan" submitter across two months, where the
viewing tenant's own data spanned seven months and thirteen people. Nothing errored. The only
symptom was numbers that were somebody else's.

`st.cache_data` caches are shared by every session in the process, so this applies to every page,
not just the report. These tests enforce two mechanical rules across the app:

  1. No cached function takes an underscore-prefixed parameter. If it is in the signature it is
     meant to affect the result; if it should not affect the key, it should not be a parameter.
  2. A cached function whose body reads through `get_client()` (the tenant-scoped client) takes a
     scope parameter.

Rule 1 is the one that actually bit, and it is the one a reviewer cannot see by reading.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SEARCH_DIRS = ("app_pages", "views", "core")
_CACHE_DECORATORS = {"cache_data", "cache_resource"}
# Names that count as a tenant discriminator.
_SCOPE_NAMES = {"scope", "scope_key", "scope_tid", "tenant", "tenant_id"}


def _python_files():
    for d in _SEARCH_DIRS:
        root = os.path.join(_ROOT, d)
        for dirpath, _dirs, files in os.walk(root):
            if "__pycache__" in dirpath or "sync-conflict" in dirpath:
                continue
            for f in files:
                if f.endswith(".py") and "sync-conflict" not in f:
                    yield os.path.join(dirpath, f)


def _is_cache_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr in _CACHE_DECORATORS


def _cached_functions():
    """(relative path, function node) for every @st.cache_data / cache_resource function."""
    for path in _python_files():
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(_is_cache_decorator(d) for d in node.decorator_list):
                    yield os.path.relpath(path, _ROOT).replace("\\", "/"), node


def _param_names(fn) -> list[str]:
    a = fn.args
    return [p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)]


def _reads_tenant_client(fn) -> bool:
    """True when the body reads a table and does NOT go through `service_client`.

    `service_client()` bypasses tenant scoping deliberately, and the only caches using it here
    read the org-agnostic opportunity catalogue — identical for every tenant, so one shared entry
    is correct. Anything else that touches `.table(...)` is treated as tenant-scoped, including
    the report's loaders, which reach the client through a module-level `sb = get_client()` and
    so never name it inside the function.
    """
    dumped = ast.dump(fn)
    if "service_client" in dumped:
        return False
    return "'table'" in dumped


class NoCachedFunctionHidesAParameterFromItsKeyTests(unittest.TestCase):
    """Rule 1 — the bug that shipped.

    Streamlit drops underscore-prefixed arguments from the cache key. A parameter that cannot
    affect the result has no business being a parameter, and one that CAN must be hashed.
    """

    def test_no_cached_function_has_an_underscore_prefixed_parameter(self):
        offenders = []
        for rel, fn in _cached_functions():
            for name in _param_names(fn):
                if name.startswith("_"):
                    offenders.append(f"{rel}:{fn.lineno} {fn.name}({name})")
        self.assertEqual(
            offenders, [],
            "these parameters are EXCLUDED from the cache key, so they do not separate "
            f"anything: {offenders}")

    def test_the_audit_actually_found_cached_functions(self):
        # Otherwise both rules pass by finding nothing.
        self.assertGreater(len(list(_cached_functions())), 10)


class TenantScopedReadsCarryTheTenantTests(unittest.TestCase):
    """Rule 2 — a cache shared across sessions must not be shared across tenants."""

    def test_every_cached_tenant_read_takes_a_scope_parameter(self):
        offenders = []
        for rel, fn in _cached_functions():
            if not _reads_tenant_client(fn):
                continue
            if not any(p in _SCOPE_NAMES for p in _param_names(fn)):
                offenders.append(f"{rel}:{fn.lineno} {fn.name}({', '.join(_param_names(fn))})")
        self.assertEqual(
            offenders, [],
            "these read tenant-scoped rows through get_client() into a PROCESS-GLOBAL cache "
            f"with no tenant in the key: {offenders}")

    def test_the_rule_examines_the_loaders_it_is_meant_to(self):
        names = {fn.name for rel, fn in _cached_functions()
                 if rel == "views/report.py" and _reads_tenant_client(fn)}
        self.assertIn("_load_rfps", names)


class TheScopeKeyItselfTests(unittest.TestCase):
    def test_it_never_raises(self):
        from core import cache_scope
        self.assertTrue(cache_scope.scope_key().startswith("t:"))

    def test_a_failure_gets_its_own_bucket_rather_than_sharing_one(self):
        # Falling back to a shared key would be the leak this module exists to prevent.
        from unittest import mock
        from core import cache_scope
        with mock.patch("db.supabase_client._tenant_scope_tid",
                        side_effect=RuntimeError("no session")):
            self.assertEqual(cache_scope.scope_key(), "t:unknown")

    def test_an_unscoped_session_is_explicitly_all(self):
        from unittest import mock
        from core import cache_scope
        with mock.patch("db.supabase_client._tenant_scope_tid", return_value=None):
            self.assertEqual(cache_scope.scope_key(), "t:all")

    def test_a_scoped_session_keys_on_its_tenant(self):
        from unittest import mock
        from core import cache_scope
        with mock.patch("db.supabase_client._tenant_scope_tid", return_value="abc123"):
            self.assertEqual(cache_scope.scope_key(), "t:abc123")


class StreamlitReallyDoesIgnoreUnderscoredArgsTests(unittest.TestCase):
    """The premise of rule 1, asserted rather than assumed — if Streamlit ever changes this,
    the reason for the rule should be re-read rather than the rule silently kept."""

    def test_changing_an_underscored_argument_does_not_re_execute(self):
        # In a SUBPROCESS: another module in this suite installs a stub `streamlit` into
        # sys.modules, so `st.cache_data` is missing when this runs in-process. The claim under
        # test is about the real Streamlit, so it needs the real one.
        import subprocess
        import textwrap
        code = textwrap.dedent("""
            import streamlit as st
            calls = {'n': 0}

            @st.cache_data
            def probe(_scope):
                calls['n'] += 1
                return 'data-' + _scope

            a = probe('tenant-A')
            b = probe('tenant-B')
            print('EXECUTIONS', calls['n'])
            print('SAME', a == b)
        """)
        proc = subprocess.run([sys.executable, "-c", code], cwd=_ROOT,
                              capture_output=True, text=True, timeout=300)
        detail = f"{proc.stdout}\n{proc.stderr[-800:]}"
        self.assertIn("EXECUTIONS 1", proc.stdout,
                      "Streamlit no longer ignores underscored args — re-read why the rule "
                      "exists before keeping it.\n" + detail)
        self.assertIn("SAME True", proc.stdout, "tenant-B did not receive tenant-A's data")


if __name__ == "__main__":
    unittest.main(verbosity=2)
