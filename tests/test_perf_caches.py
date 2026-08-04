"""Regression tests for the page-load performance caches (Tier 0).

Page latency in this app is dominated by the NUMBER of sequential Supabase round-trips
(~0.35s each, regardless of payload) — Streamlit re-runs the whole script on every widget
interaction and st.tabs() executes every tab body. These caches remove the repeated reads.

What must hold:
  * a second call inside the TTL must NOT issue another query;
  * a write must invalidate, so the next read sees fresh data;
  * a caller mutating the returned value must NOT poison the cache (the org-profile editor
    mutates what get_profile() hands back);
  * a transient error must NOT be cached as a result.

Pure/offline: the Supabase client is a counting fake.

Run:  python -m unittest tests.test_perf_caches
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import org_profile, settings          # noqa: E402
from auth import tenant_context as tc           # noqa: E402

_TID = "t-alpha"


class _FakeQuery:
    def __init__(self, counter, payload, boom=False):
        self._c, self._payload, self._boom = counter, payload, boom

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        self._c["n"] += 1
        if self._boom:
            raise RuntimeError("transient")
        return mock.Mock(data=self._payload)


class _FakeClient:
    def __init__(self, counter, payload, boom=False):
        self._c, self._payload, self._boom = counter, payload, boom

    def table(self, _name):
        return _FakeQuery(self._c, self._payload, self._boom)


class OrgProfileCacheTests(unittest.TestCase):
    def setUp(self):
        org_profile._clear_profile_cache()

    def _patch(self, counter, payload, boom=False):
        client = _FakeClient(counter, payload, boom)
        return mock.patch.object(org_profile, "_tenant_store", return_value=(client, _TID))

    def test_second_call_is_served_from_cache(self):
        c = {"n": 0}
        with self._patch(c, [{"org_profile": {"org_short": "X"}}]):
            org_profile.get_profile()
            org_profile.get_profile()
            org_profile.get_profile()
        self.assertEqual(c["n"], 1, "cached reads must not re-query")

    def test_mutating_the_result_does_not_poison_the_cache(self):
        # The Org-setup editor mutates the dict it gets back. If the cache handed out a
        # shared merged object, the next caller would see the mutation.
        c = {"n": 0}
        with self._patch(c, [{"org_profile": {"org_short": "X"}}]):
            first = org_profile.get_profile()
            first["org_short"] = "MUTATED"
            first.setdefault("domains", []).append("junk")
            second = org_profile.get_profile()
        self.assertEqual(second.get("org_short"), "X")
        self.assertNotIn("junk", second.get("domains") or [])

    def test_set_profile_invalidates(self):
        """A real set_profile() call must drop the cache so the next read re-queries and
        returns the SAVED value — not the pre-write one."""
        c = {"n": 0}
        stored = [{"org_profile": {"org_short": "X"}}]

        class _WQ(_FakeQuery):
            def update(self, payload):          # the write path set_profile uses
                stored[0] = {"org_profile": dict(payload["org_profile"])}
                return _FakeQuery({"n": 0}, [{"id": _TID}])   # own counter: writes aren't reads

        class _WC:
            def table(self, _n):
                return _WQ(c, [stored[0]])      # .data is a LIST of rows

        with mock.patch.object(org_profile, "_tenant_store", return_value=(_WC(), _TID)):
            self.assertEqual(org_profile.get_profile().get("org_short"), "X")
            self.assertEqual(c["n"], 1)
            org_profile.set_profile({"org_short": "Y"})
            after = org_profile.get_profile()
        self.assertEqual(after.get("org_short"), "Y",
                         "stale profile served after a save — invalidation failed")
        self.assertEqual(c["n"], 2, "the post-write read must hit the DB again")

    def test_transient_error_is_not_cached(self):
        c = {"n": 0}
        with self._patch(c, [], boom=True):
            org_profile.get_profile()
            org_profile.get_profile()
        self.assertEqual(c["n"], 2, "a failed read must not be cached as a result")


class TenantIdentityCacheTests(unittest.TestCase):
    def setUp(self):
        settings._clear_org_cache()

    def test_identity_read_is_cached_then_cleared(self):
        c = {"n": 0}
        client = _FakeClient(c, [{"org_identity": {"logo_b64": "abc"}}])
        settings._read_tenant_identity(client, _TID)
        settings._read_tenant_identity(client, _TID)
        self.assertEqual(c["n"], 1, "header logo read must not re-query every render")
        settings._clear_org_cache()
        settings._read_tenant_identity(client, _TID)
        self.assertEqual(c["n"], 2, "_clear_org_cache must drop the identity cache too")

    def test_identity_result_is_copied(self):
        c = {"n": 0}
        client = _FakeClient(c, [{"org_identity": {"logo_b64": "abc"}}])
        first = settings._read_tenant_identity(client, _TID)
        first["logo_b64"] = "MUTATED"
        self.assertEqual(settings._read_tenant_identity(client, _TID)["logo_b64"], "abc")


class MembershipCacheTests(unittest.TestCase):
    def setUp(self):
        tc.clear_membership_cache()

    def test_memberships_cached_and_invalidated(self):
        c = {"n": 0}
        rows = [{"tenant_id": _TID, "role": "admin",
                 "tenants": {"name": "Alpha", "slug": "alpha", "status": "active"}}]

        class _MQ(_FakeQuery):
            def eq(self, *a, **k):
                return self

        client = _FakeClient(c, rows)
        with mock.patch.object(tc, "service_client", return_value=client):
            tc.active_memberships("u1")
            tc.active_memberships("u1")
            self.assertEqual(c["n"], 1, "3x-per-render membership reads must collapse to 1")
            tc.clear_membership_cache("u1")
            tc.active_memberships("u1")
            self.assertEqual(c["n"], 2, "a membership write must force a re-read")

    def test_cache_is_per_user(self):
        c = {"n": 0}
        client = _FakeClient(c, [])
        with mock.patch.object(tc, "service_client", return_value=client):
            tc.active_memberships("u1")
            tc.active_memberships("u2")
        self.assertEqual(c["n"], 2, "different users must not share a cache entry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
