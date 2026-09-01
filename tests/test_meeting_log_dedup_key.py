"""The meeting-log dedup / sync-merge identity key must be ACTIONS-AWARE.

Regression guard for the "resolved actions come back as Not Resolved on Excel
sync" bug and its fix. Two invariants the sync (migrate_excel) and the dedup
script both rely on:

  * true duplicates — same meeting (date + rfp/donor) AND same action text —
    share one key, so they collapse to a single (resolved-preferring) survivor;
  * DISTINCT actions for the same meeting get DIFFERENT keys, so they are never
    merged into one (which would silently drop an action).
"""
import unittest

from scripts.dedup_meeting_logs import _natural_key


class TestNaturalKey(unittest.TestCase):
    def test_identical_action_same_key(self):
        a = _natural_key("2026-05-04", "World Diabetes Foundation", "WD-1",
                         "-Share responsibilities and get draft ready")
        b = _natural_key("2026-05-04", "World Diabetes Foundation", "WD-1",
                         "-Share responsibilities and get draft ready")
        self.assertEqual(a, b)

    def test_distinct_actions_same_meeting_differ(self):
        # The exact case the coarse (date+donor-only) key would have merged.
        share = _natural_key("2026-05-04", "World Diabetes Foundation", "WD-1",
                             "-Share responsibilities and get draft ready")
        align = _natural_key("2026-05-04", "World Diabetes Foundation", "WD-1",
                             "-Align on concept and draft proposal")
        self.assertNotEqual(share, align)

    def test_action_text_normalised(self):
        # Whitespace / case differences are not treated as distinct actions.
        a = _natural_key("2026-05-04", "Donor X", "R-1", "  Draft The Proposal ")
        b = _natural_key("2026-05-04", "Donor X", "R-1", "draft the proposal")
        self.assertEqual(a, b)

    def test_rfp_uid_preferred_over_donor(self):
        # Same rfp_uid → same identity even if the donor label was edited.
        a = _natural_key("2026-05-04", "Donor A", "R-1", "do the thing")
        b = _natural_key("2026-05-04", "Donor A (renamed)", "R-1", "do the thing")
        self.assertEqual(a, b)

    def test_donor_fallback_when_no_rfp(self):
        a = _natural_key("2026-05-04", "Donor A", None, "do the thing")
        b = _natural_key("2026-05-04", "Donor B", None, "do the thing")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
