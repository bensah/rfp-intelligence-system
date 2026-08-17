"""Every name `migrate()` uses must actually exist in its scope.

THE BUG THIS CATCHES. A sync failed in production with

    NameError: name 'args' is not defined

because a guard added to `migrate()` read `args.trust_sheet_decisions` - the argparse
namespace, which lives in `main()` and is not in scope inside `migrate()`. The line sits deep
inside the update loop, so nothing raised until a real workbook with existing rows was synced.

The tests written alongside that guard exercised its HELPERS (`_blankish`, `_fetch_app_owned`)
and never called `migrate()` itself, which is why they all passed. `migrate()` is ~400 lines
that talk to Supabase and read a spreadsheet, so calling it in a unit test is not the answer -
but a NameError needs no execution to find. This walks the function's syntax tree instead and
checks every name it READS resolves to a parameter, a local, a module-level global, a builtin,
or a comprehension/except binding.

Run:  python -m unittest tests.test_migrate_excel_names_resolve
"""
import ast
import builtins
import io
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SCRIPT = os.path.join(_ROOT, "scripts", "migrate_excel.py")


def _bound_names(fn: ast.AST) -> set[str]:
    """Every name the function binds: params, assignments, imports, with/for/except, walrus."""
    out: set[str] = set()
    a = fn.args
    for arg in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
                + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else [])):
        out.add(arg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, ast.arg):
            out.add(node.arg)                       # nested def / lambda parameters
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                out.add((al.asname or al.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            out.update(node.names)
    return out


def unresolved_names(source: str, func: str) -> set[str]:
    """Names `func` reads that are bound nowhere it can see."""
    tree = ast.parse(source)
    module_level = _bound_names_module(tree)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func)
    known = _bound_names(fn) | module_level | set(dir(builtins))
    read = {n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return read - known


def _bound_names_module(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                out.add((al.asname or al.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out.update(n.id for n in ast.walk(t)
                           if isinstance(n, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            out.update(n.id for n in ast.walk(node.target)
                       if isinstance(n, ast.Name))
        elif isinstance(node, ast.If):                # names defined under a top-level if
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    out.add(sub.id)
    return out


class TheCheckerItselfWorksTests(unittest.TestCase):
    """A checker that cannot fail is worthless — prove it catches the real shape of the bug."""

    def test_it_flags_the_exact_mistake_that_broke_the_sync(self):
        bad = (
            "import argparse\n"
            "def migrate(path, dry_run=False):\n"
            "    for r in []:\n"
            "        if not args.trust_sheet_decisions:\n"
            "            pass\n"
            "def main():\n"
            "    args = argparse.ArgumentParser().parse_args()\n"
            "    migrate(args.xlsx)\n")
        self.assertEqual(unresolved_names(bad, "migrate"), {"args"})

    def test_it_does_not_flag_a_parameter(self):
        ok = ("def migrate(path, trust_sheet_decisions=False):\n"
              "    if not trust_sheet_decisions:\n"
              "        pass\n")
        self.assertEqual(unresolved_names(ok, "migrate"), set())

    def test_it_does_not_flag_module_globals_locals_or_builtins(self):
        ok = ("TS = 1\n"
              "def helper():\n"
              "    return 2\n"
              "def migrate(path):\n"
              "    total = len([TS, helper()])\n"
              "    return print(total)\n")
        self.assertEqual(unresolved_names(ok, "migrate"), set())

    def test_it_understands_comprehensions_and_except_bindings(self):
        ok = ("def migrate(rows):\n"
              "    keep = [r for r in rows if r]\n"
              "    try:\n"
              "        pass\n"
              "    except ValueError as exc:\n"
              "        print(exc)\n"
              "    return keep\n")
        self.assertEqual(unresolved_names(ok, "migrate"), set())


class TheRealScriptResolvesTests(unittest.TestCase):
    def setUp(self):
        with io.open(_SCRIPT, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_migrate_uses_no_name_it_cannot_see(self):
        missing = unresolved_names(self.src, "migrate")
        self.assertEqual(missing, set(),
                         "migrate() reads names bound nowhere in scope: %r" % (missing,))

    def test_main_too(self):
        self.assertEqual(unresolved_names(self.src, "main"), set())

    def test_the_flag_is_a_parameter_not_a_smuggled_namespace(self):
        tree = ast.parse(self.src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "migrate")
        params = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
        self.assertIn("trust_sheet_decisions", params)

    def test_the_cli_passes_it_through(self):
        self.assertIn("trust_sheet_decisions=args.trust_sheet_decisions", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
