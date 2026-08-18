"""Regression tests for the cross-account session bleed (and the membership gate).

THE BUG: Streamlit's session_state belongs to the browser tab, not to the signed-in user.
Sign-out cleared `app_user` and five auth keys but left `tenant_id`, the minted tenant JWT
and the client built from it in place. The next person to sign in on that browser inherited
them: `ensure_tenant_context` saw a still-fresh token (or a tenant_id) and refreshed it onto
the new account, which then browsed the previous account's tenant. On a shared computer that
is a cross-tenant data breach, not a cosmetic landing bug.

THE FIX, tested here:
  * tenant state is STAMPED with the identity that set it and destroyed when a different
    account takes the session over (also when the stamp is missing entirely);
  * no session is ever scoped to a tenant the account has no ACTIVE membership in —
    super_users included — and a refusal degrades to tenant-less (zero rows), never to
    somebody else's data;
  * a membership revoked mid-session is not renewed by the near-expiry refresh.

Run:  python -m unittest tests.test_session_identity_isolation
"""
import os
import sys
import types
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# A fake `streamlit` must precede the import of auth.tenant_context (module-level
# `import streamlit as st`). Same approach as tests/test_tenant_isolation.py.
_fake_st = types.ModuleType("streamlit")
_fake_st.session_state = {}
sys.modules["streamlit"] = _fake_st

os.environ["SUPABASE_JWT_SECRET"] = "test-secret"          # multi-tenant ON
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

import auth.tenant_context as tc           # noqa: E402

HOME = "13d79b44-0000-0000-0000-000000000001"     # platform tenant (user A's)
ORG_B = "28b17088-0000-0000-0000-000000000002"    # a client tenant (user B's)

USER_A = {"id": "uid-a", "email": "a@example.org", "role": "super_user"}
USER_B = {"id": "uid-b", "email": "b@example.org", "role": "collaborator"}

_MEMS = {
    "uid-a": [{"tenant_id": HOME, "name": "Platform Home", "slug": "rfpis",
               "role": "super_user", "is_platform": True}],
    "uid-b": [{"tenant_id": ORG_B, "name": "Client Org", "slug": "client",
               "role": "collaborator", "is_platform": False}],
}


def _memberships(uid):
    return [dict(m) for m in _MEMS.get(str(uid), [])]


def _session(**kw):
    _fake_st.session_state.clear()
    _fake_st.session_state.update(kw)


def _signed_in_as(user, tenant_id, name, *, stamp=True, fresh=True):
    """A session as it looks mid-visit for `user`: tenant resolved, JWT minted, identity
    stamped. `stamp=False` reproduces a session created before identity binding existed;
    `fresh=False` puts the token past its refresh window."""
    import time
    _session(**{
        "app_user": user,
        "tenant_id": tenant_id,
        "tenant_name": name,
        "_tenant_jwt": "jwt-for-" + str(user.get("id")),
        "_tenant_jwt_exp": int(time.time()) + (3600 if fresh else 10),
        "_tenant_client": object(),
        "_tenant_client_jwt": "jwt-for-" + str(user.get("id")),
    })
    if stamp:
        _fake_st.session_state[tc._IDENTITY_KEY] = tc.identity_of(user)


class _Patched:
    """tenant_context with its DB touchpoints stubbed to the fixtures above."""

    def __enter__(self):
        self._p = [
            mock.patch.object(tc, "_resolve_user_id", side_effect=lambda u: u.get("id")),
            mock.patch.object(tc, "active_memberships", side_effect=_memberships),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._p:
            p.stop()


class SessionHandoverTests(unittest.TestCase):
    def test_second_account_does_not_inherit_the_first_ones_tenant(self):
        # THE BUG: sign out as A, sign in as B on the same browser.
        _signed_in_as(USER_A, HOME, "Platform Home")
        with _Patched():
            tc.ensure_tenant_context(USER_B)
        self.assertEqual(_fake_st.session_state["tenant_id"], ORG_B)
        self.assertEqual(_fake_st.session_state[tc._IDENTITY_KEY], "uid-b")
        self.assertNotEqual(_fake_st.session_state["_tenant_jwt"], "jwt-for-uid-a")

    def test_unstamped_tenant_state_is_never_inherited(self):
        # A session that predates identity binding carries tenant state owned by nobody.
        _signed_in_as(USER_A, HOME, "Platform Home", stamp=False)
        with _Patched():
            tc.ensure_tenant_context(USER_B)
        self.assertEqual(_fake_st.session_state["tenant_id"], ORG_B)

    def test_same_account_keeps_its_context_without_re_resolving(self):
        # The guard must not churn a healthy session: a fresh token for the SAME identity
        # short-circuits, so no membership lookup happens at all.
        _signed_in_as(USER_A, HOME, "Platform Home")
        with mock.patch.object(tc, "active_memberships",
                               side_effect=AssertionError("must not re-resolve")):
            tc.ensure_tenant_context(USER_A)
        self.assertEqual(_fake_st.session_state["tenant_id"], HOME)

    def test_stale_view_as_target_does_not_survive_the_handover(self):
        _signed_in_as(USER_A, HOME, "Platform Home")
        _fake_st.session_state["su_view_tenant"] = ORG_B
        _fake_st.session_state["su_view_name"] = "Client Org"
        with _Patched():
            tc.ensure_tenant_context(USER_B)
        self.assertIsNone(_fake_st.session_state.get("su_view_tenant"))
        self.assertIsNone(_fake_st.session_state.get("su_view_name"))

    def test_adopt_returns_false_and_keeps_state_for_the_same_identity(self):
        _signed_in_as(USER_A, HOME, "Platform Home")
        self.assertFalse(tc.adopt_session_identity(USER_A))
        self.assertEqual(_fake_st.session_state["tenant_id"], HOME)

    def test_clear_tenant_session_leaves_nothing_behind(self):
        _signed_in_as(USER_A, HOME, "Platform Home")
        _fake_st.session_state["su_view_tenant"] = ORG_B
        tc.clear_tenant_session()
        for key in tc._SESSION_TENANT_KEYS:
            self.assertNotIn(key, _fake_st.session_state, f"{key} survived sign-out")
        self.assertIn("app_user", _fake_st.session_state)   # auth layer owns that one


class MembershipGateTests(unittest.TestCase):
    def test_refuses_a_tenant_the_account_has_no_membership_in(self):
        _session(app_user=USER_B)
        with _Patched():
            ok = tc.set_active_tenant(USER_B, HOME, role="collaborator", name="Platform Home")
        self.assertFalse(ok)
        self.assertIsNone(_fake_st.session_state["tenant_id"])      # tenant-LESS, not HOME
        self.assertEqual(_fake_st.session_state["_tenant_denied"], HOME)

    def test_allows_a_tenant_the_account_does_belong_to(self):
        _session(app_user=USER_B)
        with _Patched():
            ok = tc.set_active_tenant(USER_B, ORG_B, role="collaborator", name="Client Org")
        self.assertTrue(ok)
        self.assertEqual(_fake_st.session_state["tenant_id"], ORG_B)
        self.assertEqual(_fake_st.session_state[tc._IDENTITY_KEY], "uid-b")
        self.assertNotIn("_tenant_denied", _fake_st.session_state)

    def test_super_user_is_not_exempt_from_the_gate(self):
        # A super whose platform membership is gone lands tenant-less (visible, fails
        # closed) rather than occupying a tenant with no membership row.
        _session(app_user=USER_A)
        with mock.patch.object(tc, "_resolve_user_id", side_effect=lambda u: u.get("id")), \
             mock.patch.object(tc, "active_memberships", return_value=[]), \
             mock.patch.object(tc, "_platform_home_membership",
                               return_value={"tenant_id": HOME, "name": "Platform Home",
                                             "slug": "rfpis", "role": "super_user",
                                             "is_platform": True}):
            tc.ensure_tenant_context(USER_A)
        self.assertIsNone(_fake_st.session_state["tenant_id"])

    def test_revoked_membership_is_not_renewed_at_refresh(self):
        _signed_in_as(USER_B, ORG_B, "Client Org", fresh=False)   # token due for refresh
        with mock.patch.object(tc, "_resolve_user_id", side_effect=lambda u: u.get("id")), \
             mock.patch.object(tc, "active_memberships", return_value=[]):
            tc.ensure_tenant_context(USER_B)
        self.assertIsNone(_fake_st.session_state["tenant_id"])

    def test_a_just_created_membership_is_not_refused_by_a_stale_cache(self):
        # Onboarding: the membership row is written, then the session scopes into it. A
        # cache older than the row must be re-read, not treated as proof of absence.
        calls = {"n": 0}

        def _flaky(uid):
            calls["n"] += 1
            return [] if calls["n"] == 1 else _memberships(uid)

        _session(app_user=USER_B)
        with mock.patch.object(tc, "_resolve_user_id", side_effect=lambda u: u.get("id")), \
             mock.patch.object(tc, "active_memberships", side_effect=_flaky):
            self.assertTrue(tc.tenant_allowed(USER_B, ORG_B))
        self.assertEqual(calls["n"], 2)                  # re-checked against fresh rows

    def test_gate_fails_closed_when_membership_lookup_errors(self):
        _session(app_user=USER_B)
        with mock.patch.object(tc, "_resolve_user_id", side_effect=lambda u: u.get("id")), \
             mock.patch.object(tc, "active_memberships",
                               side_effect=RuntimeError("db down")):
            self.assertFalse(tc.tenant_allowed(USER_B, ORG_B))


class SignOutSourceTests(unittest.TestCase):
    """The sign-out handler is UI code; assert on the source that it drops tenant state —
    a regression here silently restores the bleed."""

    def test_signout_clears_the_tenant_context(self):
        with open(os.path.join(_ROOT, "core", "app_header.py"), encoding="utf-8") as fh:
            src = fh.read()
        signout = src.split('key="topbar_signout"', 1)[1].split("def _render_search", 1)[0]
        self.assertIn("clear_tenant_session", signout)
        self.assertIn('"display_name"', signout)

    def test_login_binds_the_session_to_the_new_identity(self):
        with open(os.path.join(_ROOT, "auth", "authenticator.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("adopt_session_identity", src)
        self.assertNotIn('setdefault("display_name"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
