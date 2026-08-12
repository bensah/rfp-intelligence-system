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


class TheHousePaletteTests(unittest.TestCase):
    def test_the_ramp_runs_from_primary_toward_the_light_end(self):
        r = theme.ramp(4)
        self.assertEqual(len(r), 4)
        self.assertEqual(r[0], theme.TURQUOISE)
        self.assertNotEqual(r[-1], theme.TURQUOISE)

    def test_a_single_category_gets_the_full_primary_not_a_midpoint(self):
        self.assertEqual(theme.ramp(1), [theme.TURQUOISE])

    def test_the_faintest_step_stops_short_of_the_background_tint(self):
        # A bar in the exact light-blue tint reads as a rendering fault, not a small value.
        self.assertNotEqual(theme.ramp(8)[-1].lower(), theme.LIGHT_BLUE.lower())

    def test_no_categories_gives_no_shades(self):
        self.assertEqual(theme.ramp(0), [])
        self.assertEqual(theme.ramp(-3), [])

    def test_dark_red_never_appears_in_the_ramp(self):
        # Reserved for negatives. If it showed up decoratively it would stop meaning "bad".
        for n in range(1, 9):
            with self.subTest(n=n):
                self.assertNotIn(theme.DARK_RED, theme.ramp(n))

    def test_the_house_dark_blue_is_not_used(self):
        self.assertNotIn("003e78", theme.TURQUOISE.lower())
        for n in range(1, 9):
            self.assertFalse(any("003e78" in c.lower() for c in theme.ramp(n)))


class DarkRedIsReservedForNegativesTests(unittest.TestCase):
    def test_a_negative_category_gets_dark_red(self):
        for cat in ("Decline", "Not Approved", "Missed", "rejected"):
            with self.subTest(cat=cat):
                self.assertEqual(theme.sequence_for([cat])[cat], theme.DARK_RED)

    def test_a_neutral_category_never_does(self):
        for cat in ("Proceed", "Park", "Under Review", "In Progress", "Submitted"):
            with self.subTest(cat=cat):
                self.assertNotEqual(theme.sequence_for([cat])[cat], theme.DARK_RED)

    def test_the_negative_is_taken_out_of_the_ramp_so_it_does_not_consume_a_step(self):
        got = theme.sequence_for(["Proceed", "Park", "Decline"], order=theme.DECISION_ORDER)
        self.assertEqual(got["Proceed"], theme.ramp(2)[0])
        self.assertEqual(got["Park"], theme.ramp(2)[1])
        self.assertEqual(got["Decline"], theme.DARK_RED)


class ShadingFollowsMeaningNotFrequencyTests(unittest.TestCase):
    """The darkest shade must land on the same category every time, or one report's colour means
    something different from the next one's."""

    def test_the_given_order_wins_over_the_input_order(self):
        got = theme.sequence_for(["Decline", "Proceed", "Park"], order=theme.DECISION_ORDER)
        self.assertEqual(got["Proceed"], theme.ramp(2)[0])   # most emphatic of the positives
        self.assertEqual(got["Decline"], theme.DARK_RED)     # negative, out of the ramp

    def test_categories_outside_the_order_still_get_a_shade(self):
        got = theme.sequence_for(["Proceed", "Something New"], order=theme.DECISION_ORDER)
        self.assertEqual(set(got), {"Proceed", "Something New"})

    def test_a_missing_category_does_not_shift_the_others(self):
        # A report with no Park rows must still shade Proceed with the primary.
        got = theme.sequence_for(["Proceed", "Decline"], order=theme.DECISION_ORDER)
        self.assertEqual(got["Proceed"], theme.TURQUOISE)

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


class DistinctColoursForUnorderedCategoriesTests(unittest.TestCase):
    """A single-hue ramp is right for an ordered scale and wrong for one-series-per-person:
    thirteen steps of the same turquoise are indistinguishable, which is what the per-member
    stacked chart looked like."""

    def test_every_colour_is_distinct(self):
        for n in (3, 8, 12, 13, 20, 30):
            with self.subTest(n=n):
                cols = theme.categorical(n)
                self.assertEqual(len(cols), n)
                self.assertEqual(len(set(cols)), n, "two categories share a colour")

    def test_the_primary_leads(self):
        self.assertEqual(theme.categorical(5)[0], theme.TURQUOISE)

    def test_dark_red_is_never_spent_on_a_category(self):
        # It means "negative" everywhere else; using it for whoever is sixth in a legend would
        # empty it of that meaning.
        for n in (5, 12, 25):
            with self.subTest(n=n):
                self.assertNotIn(theme.DARK_RED, theme.categorical(n))

    def test_the_house_dark_blue_is_not_used(self):
        self.assertFalse(any("003e78" in c.lower() for c in theme.categorical(24)))

    def test_none_requested_gives_none(self):
        self.assertEqual(theme.categorical(0), [])
        self.assertEqual(theme.categorical(-2), [])

    def test_beyond_the_accent_list_it_lightens_rather_than_repeats(self):
        base = theme.categorical(12)
        longer = theme.categorical(24)
        self.assertEqual(longer[:12], base)          # stable prefix
        self.assertEqual(len(set(longer)), 24)
