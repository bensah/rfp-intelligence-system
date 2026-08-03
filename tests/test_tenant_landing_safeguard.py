"""Regression tests for the super-user tenant-landing safeguard (tenant_context).

A super_user with MORE THAN ONE active membership must land in their platform (super
console) home. The old code tried is_platform / slug 'rfpis' / name-startswith 'rfpis'
AMONG their memberships, then fell back to the alphabetically-first membership — so when
the platform membership was filtered out of active_memberships() (pending/blacklisted
tenant, non-active membership row, or a lost is_platform embed), the super deterministically
landed in the alphabetically-first NON-platform tenant (the "lands in Example Country Team" bug).

The safeguard resolves the platform tenant DIRECTLY (service client) before the alphabetical
fallback. These tests patch that resolver so they stay pure/offline.

Run:  python -m unittest tests.test_tenant_landing_safeguard
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

from auth import tenant_context as tc    # noqa: E402

_SUPER = {"role": "super_user"}
_USER = {"role": "collaborator"}

# Two non-platform memberships, alphabetical first = "Example Country Team".
_ALPHA = {"tenant_id": "28b17088", "name": "Example Country Team", "slug": "Example Country Team",
         "role": "admin", "is_platform": False}
_Example Partner = {"tenant_id": "df584f3e", "name": "Example Partner Co", "slug": "Example Partner",
           "role": "admin", "is_platform": False}
_PLATFORM_MEM = {"tenant_id": "13d79b44", "name": "RFPIS APP", "slug": "rfpis",
                 "role": "super_user", "is_platform": True}
_PLATFORM_DIRECT = {"tenant_id": "13d79b44", "name": "RFPIS APP", "slug": "rfpis",
                    "role": "super_user", "is_platform": True, "is_developer": False}


class LandingSafeguardTests(unittest.TestCase):
    def test_super_multi_no_platform_resolves_directly(self):
        # THE BUG FIX: platform membership absent from mems → resolve it directly,
        # NOT the alphabetically-first Example Country Team.
        with mock.patch.object(tc, "_platform_home_membership", return_value=_PLATFORM_DIRECT):
            chosen = tc._default_membership(_SUPER, [_ALPHA, _Example Partner])
        self.assertEqual(chosen["tenant_id"], "13d79b44")
        self.assertTrue(chosen["is_platform"])

    def test_super_multi_with_platform_membership_used_without_direct_lookup(self):
        # Platform membership present → return it; the direct resolver must NOT be needed.
        with mock.patch.object(tc, "_platform_home_membership",
                               side_effect=AssertionError("should not be called")):
            chosen = tc._default_membership(_SUPER, [_ALPHA, _PLATFORM_MEM, _Example Partner])
        self.assertEqual(chosen["tenant_id"], "13d79b44")

    def test_super_multi_direct_none_falls_back_to_alphabetical(self):
        # No platform anywhere → graceful alphabetical fallback (never tenant-less).
        with mock.patch.object(tc, "_platform_home_membership", return_value=None):
            chosen = tc._default_membership(_SUPER, [_Example Partner, _ALPHA])
        self.assertEqual(chosen["name"], "Example Country Team")   # alphabetical first

    def test_single_membership_unchanged(self):
        # The common case (both current supers): exactly one membership → that one, and the
        # direct resolver is never consulted.
        with mock.patch.object(tc, "_platform_home_membership",
                               side_effect=AssertionError("should not be called")):
            self.assertEqual(tc._default_membership(_SUPER, [_Example Partner])["tenant_id"], "df584f3e")
            self.assertEqual(tc._default_membership(_USER, [_ALPHA])["tenant_id"], "28b17088")

    def test_nonsuper_multi_prefers_remembered_then_alphabetical(self):
        # Non-super path is untouched by the safeguard.
        with mock.patch.object(tc, "_platform_home_membership",
                               side_effect=AssertionError("should not be called")):
            remembered = tc._default_membership({**_USER, "last_tenant_id": "df584f3e"},
                                                [_ALPHA, _Example Partner])
            self.assertEqual(remembered["tenant_id"], "df584f3e")
            first = tc._default_membership(_USER, [_Example Partner, _ALPHA])
            self.assertEqual(first["name"], "Example Country Team")

    def test_no_memberships_returns_none(self):
        self.assertIsNone(tc._default_membership(_SUPER, []))

    def test_platform_home_membership_queries_service_client(self):
        # The direct resolver: is_platform=True hit → membership-shaped dict, role forced super.
        fake_exec = mock.Mock()
        fake_exec.execute.return_value = mock.Mock(
            data=[{"id": "13d79b44", "name": "RFPIS APP", "slug": "rfpis", "is_platform": True}])
        chain = mock.Mock()
        chain.select.return_value.eq.return_value.limit.return_value = fake_exec
        fake_client = mock.Mock()
        fake_client.table.return_value = chain
        with mock.patch.object(tc, "service_client", return_value=fake_client):
            out = tc._platform_home_membership()
        self.assertEqual(out["tenant_id"], "13d79b44")
        self.assertEqual(out["role"], "super_user")
        self.assertTrue(out["is_platform"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
