"""Tenant-personalised copy, and the single chart palette.

`{tenant}` in UI copy replaces "entity" — our internal word for a tenant, which had leaked into
captions where it told the reader nothing. Naming the tenant also sidesteps the reason "entity"
was chosen: a tenant may be an organisation OR an individual, and a name is accurate either way.

The palette replaces a per-chart free-for-all with one hue at varying opacity.
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

from core import chart_theme as theme                      # noqa: E402
from core import tenant_labels as labels                   # noqa: E402


class NamingTheTenantTests(unittest.TestCase):
    def test_the_placeholder_becomes_the_tenant_name(self):
        with mock.patch.object(labels.settings, "get_org_name", return_value="Country Team A"):
            self.assertEqual(labels.fill("Strong fit for {tenant}."),
                             "Strong fit for Country Team A.")

    def test_copy_without_the_placeholder_is_untouched(self):
        self.assertEqual(labels.fill("Fresh calls worth a look."), "Fresh calls worth a look.")

    def test_an_unconfigured_tenant_falls_back_to_second_person(self):
        # Substituting settings' own "Your Organization" default verbatim would read
        # "Strong fit for Your Organization".
        with mock.patch.object(labels.settings, "get_org_name", return_value="Your Organization"):
            self.assertEqual(labels.fill("calls {tenant} is eligible for"),
                             "calls your organization is eligible for")

    def test_a_missing_name_falls_back_too(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with mock.patch.object(labels.settings, "get_org_name", return_value=value):
                    self.assertEqual(labels.tenant_name(), "your organization")

    def test_a_settings_failure_never_takes_a_page_down_over_a_caption(self):
        with mock.patch.object(labels.settings, "get_org_name",
                               side_effect=RuntimeError("no db")):
            self.assertEqual(labels.tenant_name(), "your organization")
            self.assertEqual(labels.fill("for {tenant}"), "for your organization")

    def test_the_possessive_handles_a_trailing_s(self):
        with mock.patch.object(labels.settings, "get_org_name", return_value="Northern Programs"):
            self.assertEqual(labels.tenant_possessive(), "Northern Programs'")
        with mock.patch.object(labels.settings, "get_org_name", return_value="Country Team A"):
            self.assertEqual(labels.tenant_possessive(), "Country Team A's")

    def test_empty_copy_is_safe(self):
        self.assertEqual(labels.fill(""), "")
        self.assertEqual(labels.fill(None), None)

    def test_braces_in_copy_do_not_raise(self):
        # `str.format` would fail on these; the substitution is deliberately plain replacement.
        self.assertEqual(labels.fill("100% of {unknown} calls"), "100% of {unknown} calls")


class OneHueAtVaryingOpacityTests(unittest.TestCase):
    def test_the_ramp_runs_from_emphatic_to_faint(self):
        r = theme.ramp(4)
        self.assertEqual(len(r), 4)
        alphas = [float(c.rsplit(",", 1)[1].rstrip(")")) for c in r]
        self.assertEqual(alphas, sorted(alphas, reverse=True))

    def test_every_shade_is_the_same_hue(self):
        for shade in theme.ramp(5):
            self.assertTrue(shade.startswith("rgba(0,112,60,"), shade)

    def test_a_single_category_gets_full_strength_not_the_midpoint(self):
        self.assertEqual(theme.ramp(1), [theme.rgba(0.95)])

    def test_the_faintest_shade_is_still_visible(self):
        alphas = [float(c.rsplit(",", 1)[1].rstrip(")")) for c in theme.ramp(9)]
        self.assertGreaterEqual(min(alphas), 0.3, "a bar this faint reads as a rendering fault")

    def test_no_categories_gives_no_shades(self):
        self.assertEqual(theme.ramp(0), [])
        self.assertEqual(theme.ramp(-3), [])

    def test_the_deep_blue_is_not_in_the_palette(self):
        self.assertNotIn("003366", theme.BRAND)
        self.assertNotEqual(theme.BRAND_RGB, (0, 51, 102))


class ShadingFollowsMeaningNotFrequencyTests(unittest.TestCase):
    """The darkest shade must land on the same category every time, or one report's colour means
    something different from the next one's."""

    def test_the_given_order_wins_over_the_input_order(self):
        got = theme.sequence_for(["Decline", "Proceed", "Park"], order=theme.DECISION_ORDER)
        self.assertEqual(got["Proceed"], theme.ramp(3)[0])   # most emphatic
        self.assertEqual(got["Decline"], theme.ramp(3)[2])   # least

    def test_categories_outside_the_order_still_get_a_shade(self):
        got = theme.sequence_for(["Proceed", "Something New"], order=theme.DECISION_ORDER)
        self.assertEqual(set(got), {"Proceed", "Something New"})

    def test_a_missing_category_does_not_shift_the_others(self):
        # A report with no Park rows must still shade Proceed darkest.
        got = theme.sequence_for(["Proceed", "Decline"], order=theme.DECISION_ORDER)
        self.assertEqual(got["Proceed"], theme.ramp(2)[0])

    def test_every_ordered_vocabulary_is_covered(self):
        for order in (theme.DECISION_ORDER, theme.PROGRESS_ORDER,
                      theme.DONOR_DECISION_ORDER, theme.SUBMITTED_ORDER):
            with self.subTest(order=order[0]):
                got = theme.sequence_for(list(order), order=order)
                self.assertEqual(len(got), len(order))


class TheSharedFigureStyleTests(unittest.TestCase):
    def test_it_clears_the_plot_background(self):
        # Plotly's grey plot area inside a bordered container reads as a box within a box.
        import plotly.express as px
        fig = theme.style(px.bar(x=[1, 2], y=["a", "b"]))
        self.assertEqual(fig.layout.plot_bgcolor, "rgba(0,0,0,0)")
        self.assertEqual(fig.layout.paper_bgcolor, "rgba(0,0,0,0)")

    def test_height_and_legend_are_optional_overrides(self):
        import plotly.express as px
        fig = theme.style(px.bar(x=[1], y=["a"]), height=222, showlegend=False)
        self.assertEqual(fig.layout.height, 222)
        self.assertFalse(fig.layout.showlegend)


if __name__ == "__main__":
    unittest.main(verbosity=2)
