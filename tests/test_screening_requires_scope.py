"""A tenant that has declared nothing to screen against must not be screened.

THE REPORTED CASE. One tenant's whole pipeline arrived in a single review week: 31 rows,
none decided, 28 with no readable description, and titles like "IFB - Nigeria - Asphalt
Overlay", "Modern Slavery Fund Viet Nam Programme" and a Finnish trade-promotion scheme -
offered to a health-focused organisation.

WHY. That tenant declares no programme areas, so:
  * `_blank_policies` sets `themes.required_any = []`
    ("no theme gate -> every sector populates")
  * `_seed_themes_from_profile` cannot repair it - the profile lists no areas
  * `theme_eligible` short-circuits on an empty list: "no theme requirements set", a PASS

so geography alone decided what reached the pipeline. Roughly 19 of the 31 rows would have
been rejected on theme alone had a theme list existed.

`screen_all_tenants` documents the opposite intent - "a fresh tenant with a minimal profile
gets many rows, mostly Decline" (Option C). In practice that is not a gentle default but an
unreadable review week, and the owner asked for it to stop (2026-08-16): do not screen a
tenant that has declared nothing to screen against. This test pins that reversal.

Run:  python -m unittest tests.test_screening_requires_scope
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.scan_pipeline import unscoped_screening_reason        # noqa: E402


class TheBlockPredicateTests(unittest.TestCase):
    def test_no_declared_themes_blocks_screening(self):
        reason = unscoped_screening_reason({"themes": {"required_any": []}})
        self.assertTrue(reason)
        self.assertIn("programme areas", reason)

    def test_a_declared_theme_list_screens_normally(self):
        self.assertEqual(
            unscoped_screening_reason({"themes": {"required_any": ["hiv", "malaria"]}}), "")

    def test_a_single_keyword_is_enough(self):
        # The bar is "has the tenant said what it does", not "said it thoroughly".
        self.assertEqual(unscoped_screening_reason({"themes": {"required_any": ["health"]}}),
                         "")

    def test_an_explicit_scan_policy_counts_even_without_profile_areas(self):
        # Checked against the RESOLVED policy on purpose: a tenant may configure the scan
        # policy directly instead of filling in programme areas, and that must still screen.
        self.assertEqual(
            unscoped_screening_reason({"themes": {"required_any": ["snakebite"]},
                                       "countries": {"eligible": []}}), "")

    def test_a_missing_themes_key_blocks_rather_than_crashing(self):
        self.assertTrue(unscoped_screening_reason({}))

    def test_the_reason_tells_the_reader_what_to_do(self):
        reason = unscoped_screening_reason({"themes": {}})
        self.assertIn("organisation profile", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
