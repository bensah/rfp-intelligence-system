"""The tenant slug in the address bar (core.app_header._apply_tenant_url).

?tenant=<slug> now names the tenant every signed-in page is showing, so the URL answers
"which tenant am I in?" and a bookmark returns you to the same place. It carries two
different meanings on purpose, and the split is the security-relevant part:

  * for a SUPER_USER the parameter SELECTS (view-as another tenant);
  * for everyone else it only DESCRIBES — an incoming value is overwritten with their own
    slug, never read. A URL that could move a user into a tenant would reopen, at the front
    door, exactly the hole the membership gate closes.

Run:  python -m unittest tests.test_tenant_url
"""
import os
import sys
import types
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _QueryParams(dict):
    """Enough of st.query_params for these tests: get / set / del / membership."""


_fake_st = types.ModuleType("streamlit")
_fake_st.session_state = {}
_fake_st.query_params = _QueryParams()
_fake_st.warning = lambda *a, **k: None
_fake_st.markdown = lambda *a, **k: None
sys.modules["streamlit"] = _fake_st

os.environ["SUPABASE_JWT_SECRET"] = "test-secret"
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import app_header as ah        # noqa: E402

HOME = "13d79b44-0000-0000-0000-000000000001"
OTHER = "28b17088-0000-0000-0000-000000000002"
SUPER = {"id": "uid-a", "email": "a@example.org", "role": "super_user"}
MEMBER = {"id": "uid-b", "email": "b@example.org", "role": "collaborator"}


def _session(**kw):
    _fake_st.session_state.clear()
    _fake_st.session_state.update(kw)


def _url(**kw):
    _fake_st.query_params.clear()
    _fake_st.query_params.update(kw)


class TenantUrlTests(unittest.TestCase):
    def test_member_url_is_stamped_with_their_own_tenant(self):
        _session(app_user=MEMBER, tenant_id=OTHER, tenant_slug="client",
                 _tenant_slug_for=OTHER)
        _url()
        ah._apply_tenant_url()
        self.assertEqual(_fake_st.query_params["tenant"], "client")

    def test_member_cannot_select_a_tenant_through_the_url(self):
        # A link to somebody else's tenant must not move them, and must not be left in the
        # address bar implying it did.
        _session(app_user=MEMBER, tenant_id=OTHER, tenant_slug="client",
                 _tenant_slug_for=OTHER)
        _url(tenant="rfpis")
        ah._apply_tenant_url()
        self.assertEqual(_fake_st.query_params["tenant"], "client")
        self.assertEqual(_fake_st.session_state["tenant_id"], OTHER)     # unmoved
        self.assertNotIn("su_view_tenant", _fake_st.session_state)       # no view-as either

    def test_super_link_still_selects_the_viewed_tenant(self):
        _session(app_user=SUPER, tenant_id=HOME, tenant_slug="rfpis", _tenant_slug_for=HOME)
        _url(tenant="client")
        with mock.patch.object(ah, "_session_tenant_slug", return_value="rfpis"), \
             mock.patch("auth.tenant_context.resolve_tenant_by_key",
                        return_value={"id": OTHER, "name": "Client Org", "slug": "client"}):
            ah._apply_tenant_url()
        self.assertEqual(_fake_st.session_state["su_view_tenant"], OTHER)
        self.assertEqual(_fake_st.query_params["tenant"], "client")      # the viewed tenant

    def test_super_at_home_gets_their_own_slug_in_the_url(self):
        _session(app_user=SUPER, tenant_id=HOME, tenant_slug="rfpis", _tenant_slug_for=HOME)
        _url()
        ah._apply_tenant_url()
        self.assertEqual(_fake_st.query_params["tenant"], "rfpis")

    def test_a_tenant_less_session_carries_no_tenant_in_the_url(self):
        # Refused or unresolved: an address bar still naming a tenant would be a lie.
        _session(app_user=MEMBER)
        _url(tenant="rfpis")
        ah._apply_tenant_url()
        self.assertNotIn("tenant", _fake_st.query_params)

    def test_stamp_is_skipped_when_already_correct(self):
        # Guarded on a difference so re-stamping never loops on every render.
        _url(tenant="client")
        marker = object()
        _fake_st.query_params["_untouched"] = marker
        ah._stamp_tenant_param("client")
        self.assertIs(_fake_st.query_params["_untouched"], marker)
        self.assertEqual(_fake_st.query_params["tenant"], "client")

    def test_slug_lookup_is_cached_per_tenant(self):
        _session(app_user=MEMBER, tenant_id=OTHER)
        calls = {"n": 0}

        class _Client:
            def table(self, _name):
                return self

            def select(self, *_a):
                return self

            def eq(self, *_a):
                return self

            def limit(self, *_a):
                return self

            def execute(self):
                calls["n"] += 1
                return types.SimpleNamespace(data=[{"slug": "client"}])

        with mock.patch("db.supabase_client.service_client", return_value=_Client()):
            self.assertEqual(ah._session_tenant_slug(), "client")
            self.assertEqual(ah._session_tenant_slug(), "client")
        self.assertEqual(calls["n"], 1)          # second call served from the session


if __name__ == "__main__":
    unittest.main(verbosity=2)
