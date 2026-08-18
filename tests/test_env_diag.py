"""Tests for the deployment diagnostics + the two safeguards around them.

What matters here:
  * the snapshot NEVER discloses a secret value (it is rendered to a browser);
  * a secret nested under a [section] header is still found (the deployment mistake that
    silently disables multi-tenant mode);
  * the verdicts name the failure states in words an operator can act on;
  * under multi-tenant, an unresolved tenant no longer inherits the legacy global org
    identity — the behaviour that made a wrong-tenant landing invisible.

Run:  python -m unittest tests.test_env_diag
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

from core import env_diag as ed          # noqa: E402
from db import supabase_client as sc     # noqa: E402

_REAL_SECRET = "super-secret-jwt-value-do-not-leak"


class _FakeSecrets(dict):
    """Stands in for st.secrets: a mapping whose values may themselves be mappings."""


class SecretLookupTests(unittest.TestCase):
    def test_env_wins_and_reports_env(self):
        with mock.patch.dict(os.environ, {"A_SECRET": "v"}, clear=False):
            self.assertEqual(sc.secret_lookup("A_SECRET"), ("v", "env"))

    def test_missing_everywhere(self):
        self.assertEqual(sc.secret_lookup("DEFINITELY_NOT_SET_ANYWHERE"), (None, None))

    def test_found_in_nested_section(self):
        # THE DEPLOYMENT TRAP: set under [supabase], invisible to a top-level lookup.
        fake_st = mock.Mock()
        fake_st.secrets = _FakeSecrets({"supabase": {"NESTED_ONLY": "v"}})
        with mock.patch.dict(sys.modules, {"streamlit": fake_st}):
            self.assertEqual(sc.secret_lookup("NESTED_ONLY"), ("v", "st.secrets[supabase]"))

    def test_string_section_is_not_substring_searched(self):
        # A plain-string section must never match by `in` (substring) and hand back junk.
        fake_st = mock.Mock()
        fake_st.secrets = _FakeSecrets({"note": "contains NESTED_ONLY in prose"})
        with mock.patch.dict(sys.modules, {"streamlit": fake_st}):
            self.assertEqual(sc.secret_lookup("NESTED_ONLY"), (None, None))


class RedactionTests(unittest.TestCase):
    def test_secret_value_never_appears_in_snapshot(self):
        with mock.patch.dict(os.environ, {"SUPABASE_JWT_SECRET": _REAL_SECRET}, clear=False):
            row = ed._secret_row("SUPABASE_JWT_SECRET")
        self.assertNotIn(_REAL_SECRET, repr(row))
        self.assertTrue(row["present"])
        self.assertEqual(row["length"], len(_REAL_SECRET))
        self.assertEqual(row["fingerprint"], ed._fingerprint(_REAL_SECRET))
        self.assertEqual(len(row["fingerprint"]), 8)

    def test_url_row_reports_project_ref_not_a_fingerprint(self):
        with mock.patch.dict(os.environ,
                             {"SUPABASE_URL": "https://abc123.supabase.co"}, clear=False):
            row = ed._secret_row("SUPABASE_URL")
        self.assertEqual(row["project_ref"], "abc123")
        self.assertNotIn("fingerprint", row)


class KeyClassTests(unittest.TestCase):
    def test_new_format_keys(self):
        self.assertEqual(ed.key_class("sb_secret_abc")[1], True)
        self.assertEqual(ed.key_class("sb_publishable_abc")[1], False)

    def test_legacy_jwt_role_claim_is_read(self):
        import base64
        import json
        payload = base64.urlsafe_b64encode(
            json.dumps({"role": "service_role"}).encode()).decode().rstrip("=")
        label, is_service = ed.key_class(f"eyJhbGciOiJIUzI1NiJ9.{payload}.sig")
        self.assertTrue(is_service)
        self.assertIn("service_role", label)

    def test_unknown_format_is_undecided_not_a_false_alarm(self):
        self.assertIsNone(ed.key_class("something-else")[1])
        self.assertEqual(ed.key_class(None)[1], None)


class VerdictTests(unittest.TestCase):
    def _snap(self, **over):
        snap = {
            "secrets": {"SUPABASE_JWT_SECRET": {"present": True, "source": "st.secrets"},
                        "SUPABASE_KEY": {"present": True, "is_service_role": True,
                                         "kind": "service-role (sb_secret_...)"}},
            "multitenant": {"multitenant_enabled": True, "tenant_count": 4},
            "session": {"live_tenant_session": True, "tenant_id": "t-1",
                        "tenant_name": "RFPIS APP", "data_scope": "scoped to one tenant"},
        }
        snap.update(over)
        return snap

    def test_healthy_deployment_reports_ok_only(self):
        levels = [lvl for lvl, _ in ed.verdicts(self._snap())]
        self.assertEqual(levels, ["ok"])

    def test_missing_jwt_secret_is_an_error(self):
        snap = self._snap()
        snap["secrets"]["SUPABASE_JWT_SECRET"] = {"present": False, "source": None}
        msgs = " ".join(m for lvl, m in ed.verdicts(snap) if lvl == "error")
        self.assertIn("SUPABASE_JWT_SECRET", msgs)

    def test_multitenant_off_with_many_tenants_is_an_error(self):
        snap = self._snap(multitenant={"multitenant_enabled": False, "tenant_count": 4})
        msgs = " ".join(m for lvl, m in ed.verdicts(snap) if lvl == "error")
        self.assertIn("OFF", msgs)
        self.assertIn("4 tenants", msgs)

    def test_multitenant_on_but_no_tenant_resolved_is_an_error(self):
        snap = self._snap(session={"live_tenant_session": True, "tenant_id": None,
                                   "data_scope": "NO ROWS (fail-closed sentinel)"})
        msgs = " ".join(m for lvl, m in ed.verdicts(snap) if lvl == "error")
        self.assertIn("NO tenant", msgs)

    def test_cli_context_is_informational_not_a_warning(self):
        # env_report.py / cron: no session -> unscoped is correct, and must not read as a
        # fault in the very tool used to diagnose faults.
        snap = self._snap(session={"live_tenant_session": False, "tenant_id": None,
                                   "data_scope": "UNSCOPED - every tenant's rows"})
        levels = {lvl for lvl, _ in ed.verdicts(snap)}
        self.assertIn("info", levels)
        self.assertNotIn("warning", levels)
        self.assertNotIn("error", levels)

    def test_anon_key_is_an_error(self):
        snap = self._snap()
        snap["secrets"]["SUPABASE_KEY"] = {"present": True, "is_service_role": False,
                                           "kind": "publishable/anon"}
        msgs = " ".join(m for lvl, m in ed.verdicts(snap) if lvl == "error")
        self.assertIn("publishable/anon", msgs)

    def test_nested_section_is_flagged_as_info_not_failure(self):
        snap = self._snap()
        snap["secrets"]["SUPABASE_JWT_SECRET"] = {"present": True,
                                                  "source": "st.secrets[supabase]"}
        levels = {lvl for lvl, _ in ed.verdicts(snap)}
        self.assertIn("info", levels)
        self.assertNotIn("error", levels)


class SnapshotResilienceTests(unittest.TestCase):
    def test_snapshot_never_raises_even_with_every_probe_broken(self):
        boom = mock.Mock(side_effect=RuntimeError("db down"))
        with mock.patch.object(sc, "service_client", boom):
            snap = ed.snapshot({"email": "x@y.z", "role": "super_user"})
        self.assertIn("build", snap)
        self.assertIn("secrets", snap)
        # The failure is REPORTED, not swallowed into a healthy-looking blank.
        self.assertTrue(snap["multitenant"].get("tenants_query_error")
                        or snap["multitenant"].get("tenant_count") is None)


class LegacyIdentityGuardTests(unittest.TestCase):
    """core.settings must not dress a tenant-less multi-tenant session in the legacy
    (pre-multi-tenant) organisation identity."""

    def setUp(self):
        from core import settings as st_mod
        self.settings = st_mod
        st_mod._clear_org_cache()

    def tearDown(self):
        self.settings._clear_org_cache()

    def test_multitenant_on_without_tenant_returns_neutral_defaults(self):
        with mock.patch.object(self.settings, "_tenant_ctx", return_value=None), \
             mock.patch.object(self.settings, "_legacy_identity_allowed", return_value=False), \
             mock.patch.object(self.settings, "get_setting",
                               side_effect=AssertionError("legacy store must not be read")):
            org = self.settings.get_org()
            self.assertEqual(org["org_name"], self.settings._ORG_DEFAULTS["org_name"])
            self.assertEqual(self.settings.get_org_logo(), (None, None))

    def test_single_tenant_still_reads_the_legacy_store(self):
        with mock.patch.object(self.settings, "_tenant_ctx", return_value=None), \
             mock.patch.object(self.settings, "_legacy_identity_allowed", return_value=True), \
             mock.patch.object(self.settings, "get_setting",
                               side_effect=lambda k, d=None: "Legacy Org"
                               if k == "org_name" else (d or "")):
            self.assertEqual(self.settings.get_org()["org_name"], "Legacy Org")

    def test_guard_follows_the_multitenant_switch(self):
        from auth import tenant_context as tc
        with mock.patch.object(tc, "multitenant_enabled", return_value=True):
            self.assertFalse(self.settings._legacy_identity_allowed())
        with mock.patch.object(tc, "multitenant_enabled", return_value=False):
            self.assertTrue(self.settings._legacy_identity_allowed())


if __name__ == "__main__":
    unittest.main(verbosity=2)
