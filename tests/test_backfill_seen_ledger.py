"""The tombstone ledger is what stops a decided call coming back.

A row that still exists is caught by the live deduplicator. A DELETED one is
remembered only in rfp_seen. So a missing tombstone is the one thing that lets a
call a human already declined return for review.

The ingest path is fine and does tombstone every insert - verified on the live
database, every auto row from the recent scans is tombstoned (4/4, 5/5, 12/12).
The gap is historical: migrations 056 and 059 renamed two columns on
rfp_submissions but not on rfp_seen, so for a long stretch every ledger read AND
write raised Postgres 42703 while both call sites caught it and carried on.
221 of 314 pipeline rows have no tombstone as a result.

This covers the decision the backfill makes, plus a static check that the script
cannot die on an unresolved name - which is how a previous change to a long
script shipped broken.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_seen_ledger import rows_needing_a_tombstone as needing


def _row(uid, **kw):
    base = {"uid": uid, "opportunity_title": f"Call {uid}",
            "opportunity_link": f"https://donor.example/{uid}",
            "funding_agency": "A Funder", "donor_decision": "Not submitted"}
    base.update(kw)
    return base


class WhichRowsGetATombstone(unittest.TestCase):
    def test_an_untombstoned_row_is_selected(self):
        out = needing([_row("A"), _row("B")], set())
        self.assertEqual([r["uid"] for r in out], ["A", "B"])

    def test_an_already_tombstoned_row_is_skipped(self):
        """Not an optimisation: an upsert would overwrite `reason`, and a
        'human_decline' tombstone must not become a bookkeeping one."""
        out = needing([_row("A"), _row("B")], {"A"})
        self.assertEqual([r["uid"] for r in out], ["B"])

    def test_every_row_already_tombstoned_means_nothing_to_do(self):
        self.assertEqual(needing([_row("A"), _row("B")], {"A", "B"}), [])

    def test_a_row_with_no_uid_is_skipped(self):
        # uid is the ledger's conflict key; a row without one cannot be recorded.
        out = needing([_row(None), _row(""), _row("C")], set())
        self.assertEqual([r["uid"] for r in out], ["C"])

    def test_duplicate_uids_in_one_batch_are_collapsed(self):
        out = needing([_row("A"), _row("A")], set())
        self.assertEqual(len(out), 1)

    def test_the_row_is_passed_through_unchanged(self):
        # seen_ledger.signature() takes the projection off the row itself, so the
        # backfilled tombstone matches a freshly-written one by construction.
        r = _row("A", call_submission_deadline="2027-01-31")
        out = needing([r], set())
        self.assertIs(out[0], r)

    def test_none_and_empty_inputs(self):
        self.assertEqual(needing(None, None), [])
        self.assertEqual(needing([], set()), [])
        self.assertEqual(needing([_row("A")], None), [{**_row("A")}])


class ItDoesNotDecideAnything(unittest.TestCase):
    """A backfill must not re-decide, close or rescore. Decisions are carried
    through untouched so a later human_decline tombstone still means what it says."""

    def test_rows_of_every_decision_are_treated_alike(self):
        rows = [_row("A", donor_decision="Not submitted"),
                _row("B", donor_decision="Not Approved"),
                _row("C", donor_decision="Approved"),
                _row("D", donor_decision="Under Review"),
                _row("E", donor_decision=None)]
        out = needing(rows, set())
        self.assertEqual(len(out), 5)
        self.assertEqual([r.get("donor_decision") for r in out],
                         ["Not submitted", "Not Approved", "Approved",
                          "Under Review", None])

    def test_the_script_calls_no_mutating_database_method(self):
        """Checked on the SYNTAX TREE, not on the text.

        A substring scan of the source also reads the docstring, which says the
        script does not rescore anything - so the promise tripped the test that
        was meant to verify it.
        """
        tree = ast.parse(Path("scripts/backfill_seen_ledger.py")
                         .read_text(encoding="utf-8"))
        forbidden = {"delete", "rescore", "rescore_submission", "upsert"}
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        self.assertEqual(called & forbidden, set(),
                         f"the backfill must only ADD tombstones, but calls {called & forbidden}")

    def test_it_writes_through_the_ledger_and_nothing_else(self):
        tree = ast.parse(Path("scripts/backfill_seen_ledger.py")
                         .read_text(encoding="utf-8"))
        def on_a_table_chain(call: ast.Call) -> bool:
            """True when this call hangs off a `.table(...)` builder chain.

            Needed because a bare name match also catches `sys.path.insert`,
            which is how this test first failed - on its own import boilerplate.
            """
            node = call.func
            while isinstance(node, (ast.Attribute, ast.Call)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr == "table":
                    return True
                node = node.func if isinstance(node, ast.Call) else node.value
            return False

        table_methods = {n.func.attr for n in ast.walk(tree)
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and on_a_table_chain(n)}
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        # The only write is seen_ledger.record (an upsert on uid); every direct
        # table call is a read.
        self.assertIn("record", called)
        self.assertEqual(table_methods - {"table", "select", "range"}, set(),
                         f"direct table calls must be reads only, got {table_methods}")


class EveryNameResolves(unittest.TestCase):
    """Static guard: a NameError in a script is not worth a production run.

    The Excel sync shipped exactly that - a reference to argparse's namespace from
    inside a function that never received it - and the tests of the day covered
    only the helpers.
    """

    @staticmethod
    def _unresolved(func: ast.FunctionDef, module: ast.Module) -> set[str]:
        import builtins
        bound = {a.arg for a in func.args.args}
        bound |= {a.arg for a in func.args.kwonlyargs}
        if func.args.vararg:
            bound.add(func.args.vararg.arg)
        if func.args.kwarg:
            bound.add(func.args.kwarg.arg)
        for node in ast.walk(func):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, ast.Lambda):
                # A lambda's own parameters are in scope inside its body. The
                # version of this checker in test_migrate_excel_names_resolve
                # misses these; it passed there only because that function has no
                # lambda. `key=lambda kv: -kv[1]` reported `kv` as unresolved.
                bound |= {a.arg for a in node.args.args}
                bound |= {a.arg for a in node.args.kwonlyargs}
                if node.args.vararg:
                    bound.add(node.args.vararg.arg)
                if node.args.kwarg:
                    bound.add(node.args.kwarg.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    bound.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name):
                        bound.add(item.optional_vars.id)
        module_globals = set(dir(builtins))
        for node in module.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_globals.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    module_globals.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for t in ([node.target] if isinstance(node, ast.AnnAssign)
                          else node.targets):
                    if isinstance(t, ast.Name):
                        module_globals.add(t.id)
        read = {n.id for n in ast.walk(func)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        return read - bound - module_globals

    def test_no_function_reads_a_name_it_cannot_see(self):
        src = Path("scripts/backfill_seen_ledger.py").read_text(encoding="utf-8")
        module = ast.parse(src)
        checked = 0
        for node in module.body:
            if isinstance(node, ast.FunctionDef):
                checked += 1
                self.assertEqual(
                    self._unresolved(node, module), set(),
                    f"{node.name}() reads a name that is not in scope")
        self.assertGreaterEqual(checked, 3, "expected to check every function")


if __name__ == "__main__":
    unittest.main()
