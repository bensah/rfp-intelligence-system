"""Regression tests for tenant read/write isolation in db.supabase_client.get_client().

These guard the cross-tenant leak fix (BUG 1): get_client() must FAIL CLOSED inside a
live multi-tenant web session — it must never return the unscoped, RLS-bypassing
service-role client there — and a super_user must be scoped to the tenant they are
VIEWING (su_view_tenant), not their home.

Pure unit tests: no network, no real Supabase. A fake `streamlit` module supplies
session_state, and service_client()/_session_tenant_client() are monkeypatched to tagged
fakes so we can assert which base client and which tenant scope get_client() chose.

Run:  python -m unittest tests.test_tenant_isolation   (from the repo root)
"""
import os
import sys
import types
import unittest

# Repo root on sys.path so `db`/`auth` import when run directly.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# A fake `streamlit` MUST be in sys.modules before importing app modules that do
# `import streamlit as st` at module top (auth.tenant_context does).
_fake_st = types.ModuleType("streamlit")
_fake_st.session_state = {}
sys.modules["streamlit"] = _fake_st

# Multi-tenant ON for these tests (presence of the JWT secret is the master switch).
os.environ["SUPABASE_JWT_SECRET"] = "test-secret"
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

import db.supabase_client as sc            # noqa: E402
import auth.tenant_context as tc           # noqa: E402

the organisation = "28b17088-4d52-4546-85fb-5a14ba9ae22c"
HOME = "13d79b44-44c3-4614-b8fa-463ad7a58e10"       # super_user's platform home (RFPIS APP)
TAADOM = "df584f3e-42f1-443d-8d08-2ae821e9b2d4"


class _FakeBase:
    def __init__(self, tag):
        self.tag = tag

    def table(self, name):                 # pragma: no cover - not exercised here
        return (self.tag, name)


def _set_session(**kw):
    _fake_st.session_state.clear()
    _fake_st.session_state.update(kw)


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self._svc = _FakeBase("SERVICE")   # RLS-bypassing service-role client
        self._jwt = _FakeBase("JWT")       # per-session RLS-backed tenant client
        # Deterministic clients — no network.
        sc.service_client = lambda: self._svc
        sc._session_tenant_client = (
            lambda: self._jwt if _fake_st.session_state.get("_tenant_jwt") else None)
        # No headless override during web-session tests.
        tc._TENANT_OVERRIDE.set(None)
        os.environ["SUPABASE_JWT_SECRET"] = "test-secret"

    def _scope(self, client):
        """The tenant_id the returned client is scoped to, or None if it's an unscoped base."""
        return getattr(client, "_tid", None)

    def _base(self, client):
        return getattr(client, "_real", client)

    # --- regular (non-super) users ----------------------------------------
    def test_regular_user_scopes_to_own_tenant_via_jwt(self):
        _set_session(app_user={"role": "collaborator"}, tenant_id=the organisation, _tenant_jwt="jwt")
        c = sc.get_client()
        self.assertEqual(self._scope(c), the organisation)
        self.assertIs(self._base(c), self._jwt)   # RLS-backed base

    def test_regular_user_missing_tenant_fails_closed(self):
        # In a live session but tenant_id unresolved → must NOT be the unscoped firehose.
        _set_session(app_user={"role": "collaborator"}, tenant_id=None, _tenant_jwt="jwt")
        c = sc.get_client()
        self.assertEqual(self._scope(c), sc._NO_TENANT_SENTINEL)
        self.assertIsNot(c, self._svc)

    def test_regular_user_without_jwt_still_scoped(self):
        # JWT client unavailable → falls to service client but STILL app-scoped to tenant.
        _set_session(app_user={"role": "collaborator"}, tenant_id=the organisation)  # no _tenant_jwt
        c = sc.get_client()
        self.assertEqual(self._scope(c), the organisation)
        self.assertIs(self._base(c), self._svc)

    # --- super_user -------------------------------------------------------
    def test_super_viewing_tenant_scopes_to_viewed_not_home(self):
        _set_session(app_user={"role": "super_user"}, tenant_id=HOME,
                     su_view_tenant=TAADOM, _tenant_jwt="jwt")
        c = sc.get_client()
        self.assertEqual(self._scope(c), TAADOM)  # the tenant being viewed, not home
        self.assertIs(self._base(c), self._svc)   # service base so view-as can read X

    def test_super_home_scopes_to_home(self):
        _set_session(app_user={"role": "super_user"}, tenant_id=HOME, _tenant_jwt="jwt")
        c = sc.get_client()
        self.assertEqual(self._scope(c), HOME)

    def test_super_no_tenant_fails_closed(self):
        _set_session(app_user={"role": "super_user"}, tenant_id=None, _tenant_jwt="jwt")
        c = sc.get_client()
        self.assertEqual(self._scope(c), sc._NO_TENANT_SENTINEL)

    # --- non-web contexts -------------------------------------------------
    def test_single_tenant_is_unscoped(self):
        os.environ.pop("SUPABASE_JWT_SECRET", None)   # multi-tenant OFF
        try:
            _set_session()                            # no app_user
            c = sc.get_client()
            self.assertIsNone(self._scope(c))         # unscoped base
            self.assertIs(c, self._svc)
        finally:
            os.environ["SUPABASE_JWT_SECRET"] = "test-secret"

    def test_headless_override_scopes_to_override(self):
        _set_session()                                # no web session
        tok = tc.set_tenant_override(TAADOM)
        try:
            c = sc.get_client()
            self.assertEqual(self._scope(c), TAADOM)
        finally:
            tc.reset_tenant_override(tok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
