"""Regression test for the jsonb double-encoding writer bug (extracted_store._clean).

`extracted_solicitations` jsonb columns (call_geographic_scope, focus_themes, …) must be
written as NATIVE Python lists/dicts. supabase-py/PostgREST serialises the request body to
JSON exactly once, so a native list lands in jsonb as a real array. The old code did
``json.dumps(v)`` first, which the client then re-serialised — storing a jsonb STRING
scalar (``"[\"EU\"]"``) instead of an array (``["EU"]``). That corrupted the geo screening
gate and every list-valued jsonb column.

These are pure/offline checks: _clean does no I/O.

Run:  python -m unittest tests.test_extracted_store_encoding
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.extracted_store import _clean, _COLS    # noqa: E402


class CleanEncodingTests(unittest.TestCase):
    def test_jsonb_list_passed_through_natively(self):
        # The heart of the bug: a list must stay a list, never a JSON string.
        out = _clean({"call_geographic_scope": ["European Union", "Philippines"]})
        self.assertEqual(out["call_geographic_scope"], ["European Union", "Philippines"])
        self.assertIsInstance(out["call_geographic_scope"], list)
        self.assertNotIsInstance(out["call_geographic_scope"], str)

    def test_jsonb_dict_passed_through_natively(self):
        prov = {"call_geographic_scope": "llm", "grant_amount": "regex"}
        out = _clean({"field_provenance": prov})
        self.assertEqual(out["field_provenance"], prov)
        self.assertIsInstance(out["field_provenance"], dict)

    def test_all_list_jsonb_columns_stay_lists(self):
        rec = {
            "eligibility_applicant_types": ["NGO"],
            "eligibility_countries": ["Cameroon"],
            "focus_themes": ["health"],
            "call_domain_areas": ["malaria"],
            "attachments": [{"name": "rfp.pdf"}],
            "resource_links": ["https://x"],
            "funding_tiers": [{"min": 1, "max": 2}],
        }
        out = _clean(rec)
        for k, v in rec.items():
            self.assertEqual(out[k], v, k)
            self.assertNotIsInstance(out[k], str, k)

    def test_empty_list_is_preserved_not_stringified(self):
        out = _clean({"focus_themes": []})
        self.assertEqual(out["focus_themes"], [])
        self.assertIsInstance(out["focus_themes"], list)

    def test_none_dropped_so_db_default_applies(self):
        out = _clean({"call_geographic_scope": None, "opportunity_name": "X"})
        self.assertNotIn("call_geographic_scope", out)
        self.assertEqual(out["opportunity_name"], "X")

    def test_unknown_columns_dropped(self):
        out = _clean({"not_a_real_column": ["x"], "opportunity_name": "X"})
        self.assertNotIn("not_a_real_column", out)
        self.assertIn("opportunity_name", out)

    def test_scalar_values_untouched(self):
        out = _clean({"grant_amount": 50000, "currency": "USD",
                      "extraction_confidence": 0.9})
        self.assertEqual(out["grant_amount"], 50000)
        self.assertEqual(out["currency"], "USD")
        self.assertEqual(out["extraction_confidence"], 0.9)

    def test_jsonb_columns_are_still_real_table_columns(self):
        # Guard: the columns this test asserts on must remain in the table schema.
        for c in ("call_geographic_scope", "focus_themes", "call_domain_areas",
                  "field_provenance", "attachments"):
            self.assertIn(c, _COLS, c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
