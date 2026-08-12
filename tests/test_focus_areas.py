"""The focus-area cloud shows what the tenant PURSUED, not what the scanner searched for.

It used to match a curated global-health vocabulary against the title and brief of every
DISCOVERED call — so it drew the scanner's search terms, across Park and Decline rows too. The
cloud was therefore largest exactly where the team had decided not to bid. In a report a tenant
sends to their leadership that is worse than uninformative: it presents rejected subject matter as
the tenant's focus.

It now reads `call_domain_areas` off the tenant's Proceed calls. Two data problems had to be
handled for that to be readable, both verified against the live rows:

  * the same area is stored several ways — "Cross-cutting - Digital Health (+AI)",
    "Cross-cutting  - Digital Health (+AI)" (two spaces) and
    "Cross-cutting Expert Areas - Digital Health". Unmerged, one area drew as three at a third
    of its weight. 34 distinct labels collapse to 24.
  * "Unspecified Program Areas" is the extractor's placeholder for "could not tell". Drawing it
    presents an absence of information as a focus.

The helper is exercised by exec'ing it out of the page, which is script-scope and cannot be
imported.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import types
import unittest

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

_REPORT = os.path.join(_ROOT, "views", "report.py")


def _helpers():
    src = io.open(_REPORT, encoding="utf-8").read()
    start = src.index("# Values that are not a programme area.")
    end = src.index("def _period_slug(")
    mod = types.ModuleType("focus_helpers")
    mod.__dict__.update({"json": json, "re": re, "pd": pd})
    exec(compile(src[start:end], "focus_helpers", "exec"), mod.__dict__)
    return mod


_H = _helpers()


def _rows(*area_lists):
    return pd.DataFrame({"call_domain_areas": list(area_lists)})


class CountingCallsPerAreaTests(unittest.TestCase):
    def test_each_call_counts_once_for_an_area(self):
        got = _H._programme_area_freq(_rows(["MNCH"], ["MNCH"], ["Vaccines"]))
        self.assertEqual(got, {"MNCH": 2, "Vaccines": 1})

    def test_a_repeat_within_one_call_is_not_counted_twice(self):
        got = _H._programme_area_freq(_rows(["MNCH", "MNCH"]))
        self.assertEqual(got, {"MNCH": 1})

    def test_no_rows_gives_nothing(self):
        self.assertEqual(_H._programme_area_freq(_rows()), {})
        self.assertEqual(_H._programme_area_freq(None), {})

    def test_a_frame_without_the_column_gives_nothing(self):
        self.assertEqual(_H._programme_area_freq(pd.DataFrame({"other": [1]})), {})

    def test_a_json_encoded_list_is_read(self):
        # The column is jsonb and arrives as a real list or as its JSON text.
        got = _H._programme_area_freq(_rows('["MNCH", "Vaccines"]'))
        self.assertEqual(got, {"MNCH": 1, "Vaccines": 1})

    def test_a_bare_comma_string_is_read(self):
        got = _H._programme_area_freq(_rows("MNCH, Vaccines"))
        self.assertEqual(got, {"MNCH": 1, "Vaccines": 1})

    def test_blanks_and_nulls_are_ignored(self):
        got = _H._programme_area_freq(_rows(["", None, "MNCH"], None, []))
        self.assertEqual(got, {"MNCH": 1})


class TheInternalPrefixIsStrippedTests(unittest.TestCase):
    def test_a_category_prefix_is_removed(self):
        got = _H._programme_area_freq(_rows(["IDs - Malaria & NTDs"]))
        self.assertEqual(got, {"Malaria & NTDs": 1})

    def test_the_same_area_written_three_ways_becomes_one(self):
        got = _H._programme_area_freq(_rows(
            ["Cross-cutting - Digital Health (+AI)"],
            ["Cross-cutting  - Digital Health (+AI)"],       # two spaces
            ["Cross-cutting Expert Areas - Digital Health"],
        ))
        self.assertEqual(got, {"Digital Health (+AI)": 3})

    def test_an_area_whose_own_name_contains_a_dash_survives(self):
        # Split on the LAST separator, so the area keeps its own punctuation.
        got = _H._programme_area_freq(_rows(["WCH - Maternal - Newborn"]))
        self.assertEqual(list(got), ["Newborn"])

    def test_no_result_still_carries_a_prefix(self):
        got = _H._programme_area_freq(_rows(["WCH - Nutrition"], ["Cross-cutting - Research"]))
        self.assertFalse([k for k in got if " - " in k])


class CanonicalisingAgainstTheTaxonomyTests(unittest.TestCase):
    def test_an_unambiguous_prefix_snaps_to_the_taxonomy_spelling(self):
        self.assertEqual(_H._canonical_area("Digital Health"), "Digital Health (+AI)")

    def test_an_exact_name_is_left_alone(self):
        self.assertEqual(_H._canonical_area("Research"), "Research")

    def test_an_area_the_taxonomy_does_not_know_keeps_its_own_name(self):
        self.assertEqual(_H._canonical_area("Quantum Epidemiology"), "Quantum Epidemiology")

    def test_an_ambiguous_prefix_is_not_guessed_at(self):
        # "D" prefixes several sub-areas; snapping it would invent a fact.
        got = _H._canonical_area("D")
        self.assertEqual(got, "D")


class PlaceholdersAreNotFocusAreasTests(unittest.TestCase):
    def test_the_extractor_placeholder_is_dropped(self):
        for value in ("Unspecified Program Areas", "unspecified programme areas",
                      "Not specified", "N/A", "None", "Other", "TBD"):
            with self.subTest(value=value):
                self.assertEqual(_H._programme_area_freq(_rows([value])), {})

    def test_a_placeholder_alongside_a_real_area_keeps_the_real_one(self):
        got = _H._programme_area_freq(_rows(["Unspecified Program Areas", "MNCH"]))
        self.assertEqual(got, {"MNCH": 1})


class ThePageUsesProceedOnlyTests(unittest.TestCase):
    """Scope is the point of the change: Park and Decline must not appear."""

    def test_the_cloud_is_built_from_proceed_rows(self):
        src = io.open(_REPORT, encoding="utf-8").read()
        block = src[src.index("# PROCEED ONLY"):src.index('st.markdown("#### Focus areas")')]
        self.assertIn('str.startswith("proceed")', block)
        self.assertIn("_programme_area_freq(_proceed_all)", block)

    def test_the_scan_vocabulary_is_no_longer_used_for_it(self):
        src = io.open(_REPORT, encoding="utf-8").read()
        block = src[src.index("# PROCEED ONLY"):src.index('st.markdown("#### Focus areas")')]
        self.assertNotIn("extract_keyword_frequencies", block)

    def test_the_caption_states_the_scope_it_actually_uses(self):
        # A reader has to know these are Proceed-only and not period-filtered, or the numbers
        # will not reconcile with the section above.
        src = io.open(_REPORT, encoding="utf-8").read()
        self.assertIn("chose to pursue", src)
        self.assertIn("not filtered by the period", src)
        self.assertIn("Park and Decline", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
