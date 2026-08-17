"""One tenant's private work records must not appear in another tenant's read scope.

THE REPORTED CASE. An organisation opened its weekly review and found 32 opportunities
where only ONE was its own. The other 31 belonged to an unrelated `kind='individual'`
tenant - a different pipeline entirely, complete with road-resurfacing tenders and a
Finnish trade-promotion scheme.

The wrapper was not broken; it was doing what it had been told. `rfp_submissions` was listed
in `_PUBLIC_VISIBLE_TABLES`, so `_ScopedTable.select` widened every read of it to

    tenant_id IN (<my tenant>, <every public tenant>)

Measured on the live database: 32 rows with the broadening, 1 without it.

That instruction was wrong on three counts. These are private WORK RECORDS, not community
content - a pipeline is a work list, a meeting log is who met whom, donor_contacts is named
people at a funder. It corrupted every per-tenant number silently, because counts, the
monthly report and exports all read through this client. And it leaked an individual's
private records to every organisation while giving that individual nothing back.

Nothing read public rows deliberately - `public_tenant_ids` had exactly one caller, the
broadening itself - so no feature depended on it.

Run:  python -m unittest tests.test_no_cross_tenant_read
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

import db.supabase_client as SC                                       # noqa: E402

MINE = "28b17088-4d52-45e8-8f7a-000000000001"
OTHER = "890f4e2f-8c6a-45e8-8f7a-000000000002"


class _Builder:
    """Records the filters applied so the test can assert HOW the read was scoped."""

    def __init__(self):
        self.calls = []

    def select(self, *a, **k):
        self.calls.append(("select", a))
        return self

    def eq(self, col, val):
        self.calls.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self.calls.append(("in_", col, list(vals)))
        return self

    def insert(self, rows, *a, **k):
        self.calls.append(("insert", rows))
        return self

    def update(self, values, *a, **k):
        self.calls.append(("update", values))
        return self

    def delete(self, *a, **k):
        self.calls.append(("delete",))
        return self


class ThePipelineIsNotSharedTests(unittest.TestCase):
    def setUp(self):
        self._orig = SC._PUBLIC_VISIBLE_TABLES
        self.addCleanup(lambda: setattr(SC, "_PUBLIC_VISIBLE_TABLES", self._orig))

    def test_a_pipeline_read_is_scoped_to_one_tenant(self):
        b = _Builder()
        SC._ScopedTable(b, MINE, "rfp_submissions").select("*")
        self.assertIn(("eq", "tenant_id", MINE), b.calls)
        self.assertFalse([c for c in b.calls if c[0] == "in_"],
                         "a pipeline read must not span tenants")

    def test_no_table_is_publicly_broadened_any_more(self):
        self.assertEqual(SC._PUBLIC_VISIBLE_TABLES, set())

    def test_the_private_record_tables_are_all_scoped(self):
        # Each of these was broadened before: a work list, meetings, funding applications,
        # narrative history and named contacts at a funder.
        for table in ("rfp_submissions", "meeting_logs", "meeting_schedule",
                      "engagement_logs", "applied_funding", "narrative_logs",
                      "donor_contacts"):
            b = _Builder()
            SC._ScopedTable(b, MINE, table).select("*")
            self.assertIn(("eq", "tenant_id", MINE), b.calls, table)

    def test_the_old_behaviour_is_what_produced_the_leak(self):
        # Pins the mechanism, so a future edit that re-adds the table fails loudly here
        # rather than quietly merging two tenants' rows in someone's review week.
        SC._PUBLIC_VISIBLE_TABLES = {"rfp_submissions"}
        b = _Builder()
        orig = SC.public_tenant_ids if hasattr(SC, "public_tenant_ids") else None
        import auth.tenant_context as TC
        _real = TC.public_tenant_ids
        TC.public_tenant_ids = lambda: [OTHER]
        try:
            SC._ScopedTable(b, MINE, "rfp_submissions").select("*")
        finally:
            TC.public_tenant_ids = _real
            if orig is not None:
                SC.public_tenant_ids = orig
        self.assertIn(("in_", "tenant_id", [MINE, OTHER]), b.calls)


class WhatMustNotChangeTests(unittest.TestCase):
    def test_writes_are_still_stamped_to_my_own_tenant(self):
        self.assertEqual(SC._stamp_tenant([{"uid": "X"}], MINE)[0]["tenant_id"], MINE)

    def test_an_unresolved_tenant_still_fails_closed(self):
        b = _Builder()
        SC._ScopedTable(b, SC._NO_TENANT_SENTINEL, "rfp_submissions").select("*")
        self.assertIn(("eq", "tenant_id", SC._NO_TENANT_SENTINEL), b.calls)

    def test_update_and_delete_stay_scoped(self):
        b = _Builder()
        t = SC._ScopedTable(b, MINE, "rfp_submissions")
        t.update({"decision": "Decline"})
        t.delete()
        self.assertEqual(len([c for c in b.calls if c == ("eq", "tenant_id", MINE)]), 2)

    def test_an_unscoped_table_is_left_alone(self):
        # extracted_solicitations is the SHARED store - deliberately not tenant-scoped.
        self.assertNotIn("extracted_solicitations", SC._TENANT_SCOPED_TABLES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
