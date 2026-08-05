"""Tests for (a) the portal-name ROOT FIX and (b) the past-deadline deep link.

(a) Every Horizon / EDCTP3 GRANT topic lives under ec.europa.eu/info/funding-tenders/..., and
    the bare "tender" rule matched that path substring — so EU grant calls were detected as
    solicitation "Tender" + instrument "Contract". That mislabel fed the opportunity-type
    gate, the RFP editor and the source registry.

(b) The past-deadline nudge told the user to go and fix the row but not WHERE it was. The
    warning now links straight to it. st.tabs cannot be selected programmatically, so
    /pipelines?uid=<uid> renders a focused single-RFP Review view instead of dropping the
    user on the Screen tab to hunt.

The Streamlit view modules execute at import, so the wiring is asserted against source.

Run:  python -m unittest tests.test_deeplink_and_portal_noise
"""
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.type_detect import _denoise, detect_solicitation, detect_instrument   # noqa: E402


def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class PortalNoiseTests(unittest.TestCase):
    EU_LINK = ("https://ec.europa.eu/info/funding-tenders/opportunities/portal/"
               "screen/opportunities/topic-details/horizon-ju-edctp3-2026")

    def test_portal_marker_loses_its_type_word(self):
        self.assertNotIn("tender", _denoise("x/funding-tenders/y"))

    def test_a_genuine_tender_phrase_survives(self):
        self.assertIn("tender", _denoise("invitation to tender for works"))

    def test_variants_are_covered(self):
        for v in ("funding-tenders", "funding_tenders", "funding-and-tenders",
                  "fundingandtenders"):
            self.assertNotIn("tender", _denoise("https://x/%s/z" % v), v)

    def test_eu_grant_call_is_not_a_tender_or_contract(self):
        cand = {"opportunity_title": "Global collaboration action for TB drugs",
                "brief_description": "grant funding", "opportunity_link": self.EU_LINK}
        self.assertNotEqual(detect_solicitation(cand), "Tender")
        self.assertNotEqual(detect_instrument(cand), "Contract")

    def test_real_tender_still_detected(self):
        cand = {"opportunity_title": "Invitation to tender for cleaning services",
                "opportunity_link": "https://council.gov/tenders/123"}
        self.assertEqual(detect_solicitation(cand), "Tender")


class DeepLinkWiringTests(unittest.TestCase):
    def test_submit_warning_links_to_the_row(self):
        src = _src("views/submit_form.py")
        i = src.index("needs_submission_check(row)")
        window = src[i:i + 1400]
        self.assertIn("/pipelines?uid=", window)
        self.assertIn("link_button", window)

    def test_link_is_only_on_the_past_deadline_path(self):
        # It must hang off needs_submission_check, not fire for every submission.
        src = _src("views/submit_form.py")
        self.assertEqual(src.count("/pipelines?uid="), 1)
        self.assertLess(src.index("needs_submission_check(row)"),
                        src.index("/pipelines?uid="))

    def test_pipelines_renders_a_focused_view_for_a_uid(self):
        src = _src("app_pages/pipelines.py")
        self.assertIn('st.query_params.get("uid")', src)
        self.assertIn('render_view("review_rfp")', src)
        # and offers a way back out
        self.assertIn("All pipelines", src)

    def test_focus_mode_short_circuits_the_tabs(self):
        src = _src("app_pages/pipelines.py")
        focus = src.index('_focus_uid = ')
        tabs = src.index("tab_screen, tab_review")
        self.assertLess(focus, tabs, "focused mode must be decided before the tabs render")
        self.assertIn("st.stop()", src[focus:tabs])

    def test_review_preselects_the_linked_row_and_its_week(self):
        src = _src("views/review_rfp.py")
        i = src.index("DEEP LINK")
        window = src[i:i + 1200]
        self.assertIn("review_rfp_selected_uid", window)
        self.assertIn("review_rfp_week", window)

    def test_review_applies_the_deep_link_before_the_week_widget(self):
        src = _src("views/review_rfp.py")
        self.assertLess(src.index("DEEP LINK"), src.index('key="review_rfp_week"'),
                        "session_state must be set before the widget is created")

    def test_missing_row_is_reported_not_silent(self):
        src = _src("views/review_rfp.py")
        i = src.index("DEEP LINK")
        self.assertIn("may have been deleted", src[i:i + 1200])


if __name__ == "__main__":
    unittest.main(verbosity=2)
