"""Regression tests for the baseline-default screener (BUG 4).

A per-tenant policy must never inherit CHAI's DEFAULT_POLICIES countries/themes, and any
geo/theme scope a tenant leaves empty is seeded from its own profile — so a configured
tenant hard-gates on at least its own geography instead of seeing everything. The
single-tenant CHAI deployment keeps the shipped defaults.

Pure unit tests: policies internals (tenant detection + profile lookup) are monkeypatched;
no DB, no streamlit.

Run:  python -m unittest tests.test_baseline_policies
"""
import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import policies as P            # noqa: E402


class BaselinePolicyTests(unittest.TestCase):
    def setUp(self):
        self._orig = {k: getattr(P, k) for k in
                      ("get_setting", "_is_scoped_tenant",
                       "_profile_geo_eligible", "_profile_theme_keywords")}
        # Default stubs — each test overrides what it needs.
        P.get_setting = lambda key: None
        P._is_scoped_tenant = lambda: True
        P._profile_geo_eligible = lambda: ["Kenya"]      # distinct from CHAI's defaults
        P._profile_theme_keywords = lambda: []

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(P, k, v)

    def _geo(self, pol):
        return pol["countries"]["eligible"], pol["countries"]["broad_terms"]

    def test_scoped_tenant_no_policy_seeds_from_profile(self):
        P.get_setting = lambda key: None
        elig, broad = self._geo(P.get_policies())
        self.assertEqual(elig, ["Kenya"])          # profile geo, NOT CHAI Cameroon/Mali
        self.assertEqual(broad, [])

    def test_scoped_tenant_saved_without_geo_falls_back_to_profile(self):
        # A saved policy that set only themes must NOT inherit CHAI's Cameroon/Mali.
        P.get_setting = lambda key: json.dumps({"themes": {"required_any": ["water"]}})
        pol = P.get_policies()
        elig, broad = self._geo(pol)
        self.assertEqual(elig, ["Kenya"])
        self.assertNotIn("Cameroon", elig)
        self.assertEqual(pol["themes"]["required_any"], ["water"])   # explicit theme kept

    def test_scoped_tenant_explicit_geo_is_honored(self):
        P.get_setting = lambda key: json.dumps(
            {"countries": {"eligible": ["Ghana"], "broad_terms": []}})
        elig, _ = self._geo(P.get_policies())
        self.assertEqual(elig, ["Ghana"])          # explicit wins over profile

    def test_scoped_tenant_region_only_scope_preserved(self):
        P.get_setting = lambda key: json.dumps(
            {"countries": {"eligible": [], "broad_terms": ["Sub-Saharan Africa"]}})
        elig, broad = self._geo(P.get_policies())
        self.assertEqual(elig, [])                  # region-only respected — not collapsed
        self.assertEqual(broad, ["Sub-Saharan Africa"])

    def test_single_tenant_no_policy_keeps_chai_defaults(self):
        P._is_scoped_tenant = lambda: False
        P.get_setting = lambda key: None
        elig, _ = self._geo(P.get_policies())
        self.assertEqual(sorted(elig), ["Cameroon", "Mali"])   # shipped CHAI defaults

    def test_single_tenant_saved_without_geo_inherits_defaults(self):
        # Single-tenant: merging onto DEFAULT_POLICIES is correct (they're CHAI's own).
        P._is_scoped_tenant = lambda: False
        P.get_setting = lambda key: json.dumps({"themes": {"required_any": ["health"]}})
        elig, _ = self._geo(P.get_policies())
        self.assertEqual(sorted(elig), ["Cameroon", "Mali"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
