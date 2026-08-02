"""Regression tests for the Active Grants overhaul helpers.

Covers the two PURE pieces:
  * core.pipeline.deadline_status — once SUBMITTED, a passed deadline reads as an outcome
    (Submitted / Awarded / Not approved), never "Overdue"; undated/future keep discovery
    semantics.
  * core.permissions.can_edit_status — any authenticated tenant member may edit statuses.

The Active Grants membership rule (a Completed-but-undecided grant enters Active Grants,
bucketed Under Review) lives inline in app_pages/grants.py (a Streamlit page); its logic is
asserted here in miniature to lock the intent.

Run:  python -m unittest tests.test_grants_status
"""
import os
import sys
import unittest
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import pipeline as P            # noqa: E402
from core import permissions as PERM      # noqa: E402

_PAST = (date.today() - timedelta(days=30)).isoformat()
_FUTURE = (date.today() + timedelta(days=30)).isoformat()
_SOON = (date.today() + timedelta(days=3)).isoformat()


class DeadlineStatusTests(unittest.TestCase):
    def test_not_submitted_keeps_discovery_semantics(self):
        self.assertEqual(P.deadline_status(_PAST), "Overdue")
        self.assertEqual(P.deadline_status(_SOON), "Due Soon")
        self.assertEqual(P.deadline_status(_FUTURE), "On Track")

    def test_submitted_past_deadline_is_not_overdue(self):
        self.assertEqual(P.deadline_status(_PAST, submitted=True), "Submitted")

    def test_submitted_and_approved_is_awarded(self):
        self.assertEqual(P.deadline_status(_PAST, submitted=True, decision="Approved"),
                         "Awarded")

    def test_submitted_and_declined(self):
        self.assertEqual(P.deadline_status(_PAST, submitted=True, decision="Not Approved"),
                         "Not approved")

    def test_submitted_future_deadline_still_due_soon(self):
        # A future window is still a live window even once submitted.
        self.assertEqual(P.deadline_status(_SOON, submitted=True), "Due Soon")

    def test_none_deadline(self):
        self.assertIsNone(P.deadline_status(None, submitted=True))


class CanEditStatusTests(unittest.TestCase):
    def test_every_role_can_edit_status(self):
        for role in ("super_user", "admin", "reviewer", "collaborator"):
            self.assertTrue(PERM.can_edit_status({"role": role}), role)

    def test_no_user_cannot(self):
        self.assertFalse(PERM.can_edit_status(None))
        self.assertFalse(PERM.can_edit_status({}))

    def test_delete_stays_admin_only(self):
        # Guard the boundary: opening status editing must NOT open admin-only actions.
        self.assertFalse(PERM.is_admin({"role": "collaborator"}))
        self.assertTrue(PERM.is_admin({"role": "admin"}))


class ActiveGrantsMembershipTests(unittest.TestCase):
    """Mirror of app_pages/grants.py _active/_pending intent (kept in lockstep)."""

    @staticmethod
    def _bucket(donor_decision, progress_status):
        dd = str(donor_decision or "").strip().lower()
        ps = str(progress_status or "").strip().lower()
        completed = ps == "completed"
        active = dd in ("approved", "under review") or (completed and dd != "not approved")
        pending = dd == "under review" or (completed and dd not in ("approved", "not approved"))
        return active, pending

    def test_completed_but_undecided_enters_as_pending(self):
        active, pending = self._bucket("Not submitted", "Completed")   # lead-poisoning row
        self.assertTrue(active)
        self.assertTrue(pending)

    def test_not_approved_stays_out(self):
        active, _ = self._bucket("Not Approved", "Completed")
        self.assertFalse(active)

    def test_discontinued_stays_out(self):
        active, _ = self._bucket("Not submitted", "Discontinued")
        self.assertFalse(active)

    def test_approved_is_active_not_pending(self):
        active, pending = self._bucket("Approved", "Completed")
        self.assertTrue(active)
        self.assertFalse(pending)


if __name__ == "__main__":
    unittest.main(verbosity=2)
