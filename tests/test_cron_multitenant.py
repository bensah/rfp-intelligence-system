"""Regression test for the Friday cron's multi-tenant detection.

The weekly scan must populate EACH tenant's pipeline (extract → per-tenant screen). That
per-tenant screening writes tenant_id via the headless override, which is JWT-independent,
so the cron must treat the deployment as multi-tenant when the DB has >=2 active
non-platform tenants — even if SUPABASE_JWT_SECRET isn't in the cron env. A genuine
single-tenant deploy (JWT off, <2 real tenants) must stay single-tenant so run_scan falls
back to a normal full ingest rather than tenant-tagging a lone seeded tenant.

Run:  python -m unittest tests.test_cron_multitenant
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import scan_pipeline as SP        # noqa: E402
import auth.tenant_context as tc            # noqa: E402


def _t(name, platform=False):
    return {"id": name, "name": name, "is_platform": platform}


class MultiTenantDetectionTests(unittest.TestCase):
    def setUp(self):
        self._orig = tc.multitenant_enabled

    def tearDown(self):
        tc.multitenant_enabled = self._orig

    def _set_jwt(self, on):
        tc.multitenant_enabled = lambda: on

    def test_jwt_on_is_multitenant_regardless_of_tenants(self):
        self._set_jwt(True)
        self.assertTrue(SP.is_multitenant_deploy([]))
        self.assertTrue(SP.is_multitenant_deploy([_t("solo")]))

    def test_jwt_off_two_non_platform_is_multitenant(self):
        self._set_jwt(False)
        tenants = [_t("RFPIS APP", platform=True), _t("the organisation"), _t("the second tenant")]
        self.assertTrue(SP.is_multitenant_deploy(tenants))   # DB signal → cron screens

    def test_jwt_off_single_non_platform_is_single_tenant(self):
        self._set_jwt(False)
        self.assertFalse(SP.is_multitenant_deploy([_t("RFPIS APP", platform=True), _t("the organisation")]))
        self.assertFalse(SP.is_multitenant_deploy([_t("the organisation")]))
        self.assertFalse(SP.is_multitenant_deploy([]))

    def test_platform_tenants_do_not_count_toward_multitenant(self):
        self._set_jwt(False)
        # Two tenants but both platform → not multi-tenant.
        self.assertFalse(SP.is_multitenant_deploy([_t("a", platform=True), _t("b", platform=True)]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
