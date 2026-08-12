"""Person names: never in source, honorifics are not names, one person is one bar.

Three defects, one file.

**A real name was hard-coded in the page.** Worse than a leak: the alias mapped a real first name
to a surname that had been INVENTED during an anonymisation pass, so the report charted a person
under a name that does not exist. And because the invented name collided with the person's real
record, it split them across two bars. Person names are data; they belong in the database behind
the same access controls as the roster, and the maps here are empty until configured there.

**A title was charted as a person.** The chart label is the first token, so a value beginning
"Prof" produced a bar labelled "Prof".

**One person appeared with two names among a column of first names.** Two spellings of the same
person share a first name, and the old rule read that as a collision between two people and
printed both in full.

Every name below is invented.
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

from core import member_names as mn                        # noqa: E402

_SOURCES = ("core/member_names.py", "views/report.py")


def _roster(*names):
    """Patch the canonical roster the resolver matches against."""
    return mock.patch.object(mn.dropdowns, "get", return_value=list(names))


class NoPersonNamesInSourceTests(unittest.TestCase):
    """The maps must come from the database. A default that is anything but empty means somebody
    has put a person into a file that gets pushed."""

    def test_the_maps_are_empty_without_database_configuration(self):
        with mock.patch.object(mn.settings, "get_setting", return_value=None):
            self.assertEqual(mn._nickname_map(), {})
            self.assertEqual(mn._fullname_aliases(), {})

    def test_a_database_failure_leaves_names_as_typed(self):
        with mock.patch.object(mn.settings, "get_member_nicknames",
                               side_effect=RuntimeError("no db")), \
             mock.patch.object(mn.settings, "get_member_name_aliases",
                               side_effect=RuntimeError("no db")):
            self.assertEqual(mn._nickname_map(), {})
            self.assertEqual(mn._fullname_aliases(), {})
            with _roster():
                self.assertEqual(mn.normalize_member_name("ada nwosu"), "Ada Nwosu")

    def test_the_source_files_declare_no_name_maps_inline(self):
        # A dict literal of names is what was there before; assert the shape cannot come back.
        for rel in _SOURCES:
            with self.subTest(file=rel):
                with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
                    src = fh.read()
                self.assertNotIn("_NICKNAME_TO_FULL = {", src)
                self.assertNotIn("_FULLNAME_ALIASES = {", src)

    def test_the_aliases_are_read_from_the_database(self):
        with mock.patch.object(mn.settings, "get_member_name_aliases",
                               return_value={"ada": "Ada Nwosu"}), _roster("Ada Nwosu"):
            self.assertEqual(mn.normalize_member_name("Ada"), "Ada Nwosu")

    def test_a_nickname_from_the_database_resolves(self):
        with mock.patch.object(mn.settings, "get_member_nicknames",
                               return_value={"addy": "ada"}), _roster("Ada Nwosu"):
            self.assertEqual(mn.normalize_member_name("Addy"), "Ada Nwosu")


class AHonorificIsNotANameTests(unittest.TestCase):
    def test_a_leading_title_is_dropped(self):
        for raw in ("Prof Ada Nwosu", "Prof. Ada Nwosu", "DR ada nwosu", "Mrs. Ada Nwosu"):
            with self.subTest(raw=raw):
                with _roster():
                    self.assertEqual(mn.normalize_member_name(raw), "Ada Nwosu")

    def test_a_value_that_is_only_a_title_is_not_a_person(self):
        for raw in ("Prof", "Prof.", "Dr", "  Mr. "):
            with self.subTest(raw=raw):
                with _roster():
                    self.assertEqual(mn.normalize_member_name(raw), "(unknown)")

    def test_the_chart_label_is_the_name_not_the_title(self):
        # The reported bug: a bar labelled "Prof".
        self.assertEqual(mn.first_name_display_map(["Prof Ada Nwosu"])["Prof Ada Nwosu"], "Ada")

    def test_stacked_titles_are_all_dropped(self):
        self.assertEqual(mn._strip_honorifics("Prof. Dr. Ada Nwosu"), "Ada Nwosu")

    def test_a_name_that_merely_starts_similarly_is_kept(self):
        # "Drew" begins with "Dr" but is a name; matching is per TOKEN, not prefix.
        self.assertEqual(mn._strip_honorifics("Drew Hall"), "Drew Hall")

    def test_no_label_is_ever_just_a_title(self):
        labels = set(mn.first_name_display_map(["Prof Ada Nwosu", "Dr Bo Eze"]).values())
        self.assertFalse(labels & mn._HONORIFICS)


class OnePersonIsOneBarTests(unittest.TestCase):
    def test_a_first_name_and_a_full_name_share_one_label(self):
        # The map is applied before the group-by, so one label means one merged bar.
        got = mn.first_name_display_map(["Ada", "Ada Nwosu"])
        self.assertEqual(got["Ada"], "Ada")
        self.assertEqual(got["Ada Nwosu"], "Ada")

    def test_two_different_people_keep_their_full_names(self):
        got = mn.first_name_display_map(["Ada Nwosu", "Ada Okafor"])
        self.assertEqual(got["Ada Nwosu"], "Ada Nwosu")
        self.assertEqual(got["Ada Okafor"], "Ada Okafor")

    def test_a_lone_full_name_shows_only_the_first_name(self):
        self.assertEqual(mn.first_name_display_map(["Ada Nwosu"])["Ada Nwosu"], "Ada")

    def test_three_spellings_of_one_person_still_merge(self):
        got = mn.first_name_display_map(["Ada", "Ada Nwosu", "Prof Ada Nwosu"])
        self.assertEqual(set(got.values()) - {"(unknown)"}, {"Ada"})

    def test_disambiguated_labels_still_drop_the_title(self):
        got = mn.first_name_display_map(["Prof Ada Nwosu", "Ada Okafor"])
        self.assertEqual(got["Prof Ada Nwosu"], "Ada Nwosu")

    def test_unknown_passes_through_so_it_still_groups(self):
        self.assertEqual(mn.first_name_display_map(["Ada Nwosu"])["(unknown)"], "(unknown)")

    def test_blanks_are_ignored(self):
        got = mn.first_name_display_map(["Ada Nwosu", "", None])
        self.assertEqual(set(got) - {"(unknown)"}, {"Ada Nwosu"})


class TidyingTests(unittest.TestCase):
    def test_all_caps_and_lower_case_normalise(self):
        with _roster():
            for raw in ("ADA NWOSU", "ada nwosu", "  Ada   Nwosu "):
                with self.subTest(raw=raw):
                    self.assertEqual(mn.normalize_member_name(raw), "Ada Nwosu")

    def test_an_apostrophe_keeps_its_casing(self):
        with _roster():
            self.assertEqual(mn.normalize_member_name("ADA O'BRIEN"), "Ada O'Brien")

    def test_blank_and_missing_become_unknown(self):
        with _roster():
            for raw in (None, "", "   "):
                with self.subTest(raw=raw):
                    self.assertEqual(mn.normalize_member_name(raw), "(unknown)")

    def test_a_first_name_rolls_up_to_the_roster_entry(self):
        with _roster("Ada Nwosu", "Bo Eze"):
            self.assertEqual(mn.normalize_member_name("Ada"), "Ada Nwosu")

    def test_the_longest_roster_match_wins(self):
        with _roster("Ada", "Ada Nwosu"):
            self.assertEqual(mn.normalize_member_name("Ada Nwosu"), "Ada Nwosu")

    def test_comma_separated_people_split(self):
        with _roster():
            self.assertEqual(mn.split_and_normalize_names("Ada Nwosu, Bo Eze"),
                             ["Ada Nwosu", "Bo Eze"])

    def test_a_title_inside_a_list_does_not_become_a_person(self):
        with _roster():
            self.assertEqual(mn.split_and_normalize_names("Ada Nwosu, Prof. Bo Eze"),
                             ["Ada Nwosu", "Bo Eze"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
