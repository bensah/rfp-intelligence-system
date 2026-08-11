"""The opportunity page must render END TO END, not just section 1.

WHY THIS FILE EXISTS. Section 2 rendered as a heading over white space, and every unit test
passed: `page_rows`, `analyse` and all nine criteria were individually fine. The fault was in
the page itself — the layout renderer used `for _row in _rows:`, shadowing `_row`, which holds
the OPPORTUNITY record for the whole module. By the time section 2 ran, `_row` was a layout row
(a list), so scoring raised `AttributeError: 'list' object has no attribute 'get'`.

With error details suppressed, a raised exception renders as BLANK SPACE. So the page silently
lost everything below the failure while looking merely empty, and nothing in the suite could
see it because nothing executed the page.

That is the gap this closes: drive the real script and assert that the second half arrives.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

CATALOG_ROW = {
    "uid": "es_test_1",
    "opportunity_name": "A Health Delivery Programme",
    "funder_name": "A Funder",
    "opportunity_url": "https://funder.example/call/1",
    "brief_description": "Support for cold chain equipment.",
    "full_description": "The programme funds equipment and training.",
    "deadline": "2026-12-01",
    "date_posted": "2026-06-01",
    "grant_amount": 500000,
    "currency": "USD",
    "call_geographic_scope": ["Countryland"],
    "solicitation_type": "RFP",
    "instrument_type": "Grant",
    "opportunity_type": "Grant/funding call",
    "funding_status": "Open",
    "solicitation_language": "English",
    "raw_text": "The programme funds equipment and training in eligible districts.",
}


# ORDER DEPENDENCE. Another module in this suite installs a FAKE `streamlit` into
# sys.modules and does not restore it, so `AppTest` here imported the stub and every test
# below errored — passing alone, failing in a full run. Capture the real modules and reinstall
# them for the duration of this module rather than depending on import order.
_SAVED: dict = {}


def setUpModule():
    import importlib
    for name in [n for n in list(sys.modules) if n == "streamlit"
                 or n.startswith("streamlit.")]:
        _SAVED[name] = sys.modules.pop(name)
    importlib.import_module("streamlit")


def tearDownModule():
    for name in [n for n in list(sys.modules) if n == "streamlit"
                 or n.startswith("streamlit.")]:
        del sys.modules[name]
    sys.modules.update(_SAVED)


def _run(uid="es_test_1"):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file("app_pages/opportunity.py", default_timeout=120)
    at.query_params["uid"] = uid
    at.session_state["app_user"] = {"email": "dev@example.com", "role": "super_user"}
    with mock.patch("db.supabase_client.get_client"), \
         mock.patch("db.supabase_client.service_client"), \
         mock.patch("core.opportunity_detail.load",
                    return_value={"kind": "catalog", "row": CATALOG_ROW,
                                  "extraction": CATALOG_ROW}), \
         mock.patch("views.opportunity_rail.render_opportunity_rail"):
        at.run()
    return at


class ThePageRendersBothSectionsTests(unittest.TestCase):
    def setUp(self):
        self.at = _run()
        self.md = [m.value for m in self.at.markdown]

    def test_the_page_raises_nothing(self):
        # A raised exception renders as blank space when error details are suppressed, so an
        # empty section and a crash look identical to the reader. Assert the crash.
        self.assertEqual([str(e.value)[:200] for e in self.at.exception], [])

    def test_section_one_arrives(self):
        self.assertTrue(any("1 · The opportunity" in m for m in self.md))

    def test_SECTION_TWO_ARRIVES(self):
        self.assertTrue(any("2 · Decision aid" in m for m in self.md))

    def test_section_two_has_CONTENT_and_not_just_a_heading(self):
        # The exact defect: the heading was present and everything under it was missing.
        idx = next(i for i, m in enumerate(self.md) if "2 · Decision aid" in m)
        after = self.md[idx + 1:]
        self.assertGreater(len(after), 3, "section 2 rendered as a bare heading")

    def test_the_nine_criteria_all_render(self):
        blob = " ".join(self.md)
        for name in ("MUST 1", "MUST 2", "MUST 3", "MUST 4", "MUST 5",
                     "PREFER 6", "PREFER 7", "PREFER 8", "PREFER 9"):
            with self.subTest(criterion=name):
                self.assertIn(name, blob)

    def test_the_bid_strength_banner_renders(self):
        self.assertTrue(any("Bid Strength" in m for m in self.md))

    def test_the_opportunity_row_is_not_shadowed_by_the_layout_loop(self):
        # The root cause, asserted directly: the page must still hold a DICT for the
        # opportunity after the layout loop has run.
        self.assertTrue(any("Award type" in m or "Type of opportunity" in m
                            for m in self.md))
        self.assertTrue(any("Bid Strength" in m for m in self.md))


if __name__ == "__main__":
    unittest.main(verbosity=2)
