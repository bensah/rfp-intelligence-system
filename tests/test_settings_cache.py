"""An UNSET setting must be cached too, or reading one in a loop is a network call per row.

This is the whole of a 337-second report render.

`get_setting` caches what it reads for 60s. But the global-store branch returned the code default
early when the row was absent, WITHOUT caching that fact:

    if val is None:
        return default          # <- never cached
    _CACHE[key] = (_now(), val)

So an unset key was re-queried on every single read. The tenant-scoped branch immediately above it
already cached its misses ("cache 'no override' too") — the asymmetry was the bug.

It stayed invisible because nothing is wrong with any individual call. It only bites when a page
resolves a setting per row: moving two name maps into the database turned every name token into a
round trip, 2122 of them at ~150ms, and the report went from a few seconds to five and a half
minutes. Profiling was the only thing that could find it, so this test exists to keep it found.
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

from core import settings                                  # noqa: E402


class _Query:
    """Counts how many times the store was actually asked."""

    def __init__(self, counter, rows):
        self.counter, self.rows = counter, rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def execute(self):
        self.counter["n"] += 1
        return mock.Mock(data=list(self.rows))


class _Client:
    def __init__(self, counter, rows=()):
        self.counter, self.rows = counter, rows

    def table(self, *a, **k): return _Query(self.counter, self.rows)


class _CacheIsolation(unittest.TestCase):
    """`_CACHE` is module state shared with every other test in the process."""

    def setUp(self):
        self._saved = dict(settings._CACHE)
        settings._CACHE.clear()
        # Force the global-store branch: the tenant-scoped path is a different code path.
        self._patch = mock.patch.object(settings, "_scoped_store", return_value=None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        settings._CACHE.clear()
        settings._CACHE.update(self._saved)


class AnUnsetSettingIsQueriedOnceTests(_CacheIsolation):
    def test_repeated_reads_of_a_missing_key_hit_the_store_once(self):
        counter = {"n": 0}
        with mock.patch.object(settings, "get_client", return_value=_Client(counter, rows=[])):
            for _ in range(50):
                settings.get_setting("a_key_that_does_not_exist")
        self.assertEqual(counter["n"], 1,
                         f"an unset key was queried {counter['n']} times — the miss is not cached")

    def test_it_still_returns_the_default_every_time(self):
        counter = {"n": 0}
        with mock.patch.object(settings, "get_client", return_value=_Client(counter, rows=[])):
            for _ in range(5):
                self.assertEqual(settings.get_setting("missing_key", "fallback"), "fallback")

    def test_the_default_is_not_baked_into_the_cache(self):
        # The cached entry records "absent", not the default the first caller happened to pass.
        counter = {"n": 0}
        with mock.patch.object(settings, "get_client", return_value=_Client(counter, rows=[])):
            self.assertEqual(settings.get_setting("missing_key", "first"), "first")
            self.assertEqual(settings.get_setting("missing_key", "second"), "second")
        self.assertEqual(counter["n"], 1)


class APresentSettingStillWorksTests(_CacheIsolation):
    def test_a_value_is_read_once_and_returned(self):
        counter = {"n": 0}
        client = _Client(counter, rows=[{"value": "yes"}])
        with mock.patch.object(settings, "get_client", return_value=client):
            for _ in range(10):
                self.assertEqual(settings.get_setting("present_key"), "yes")
        self.assertEqual(counter["n"], 1)

    def test_an_expired_entry_is_refetched(self):
        counter = {"n": 0}
        client = _Client(counter, rows=[{"value": "yes"}])
        with mock.patch.object(settings, "get_client", return_value=client):
            settings.get_setting("present_key")
            # age the entry past the TTL
            ts, val = settings._CACHE["present_key"]
            settings._CACHE["present_key"] = (ts - settings._TTL - 1, val)
            settings.get_setting("present_key")
        self.assertEqual(counter["n"], 2)

    def test_an_expired_miss_is_refetched_too(self):
        # A key set later must become visible, or caching the miss would be a permanent lie.
        counter = {"n": 0}
        with mock.patch.object(settings, "get_client", return_value=_Client(counter, rows=[])):
            settings.get_setting("later_key")
            ts, val = settings._CACHE["later_key"]
            settings._CACHE["later_key"] = (ts - settings._TTL - 1, val)
            settings.get_setting("later_key")
        self.assertEqual(counter["n"], 2)


class TheNameMapsAreCheapToResolveTests(_CacheIsolation):
    """The specific regression: two unset name-map keys, resolved per name token."""

    def test_resolving_names_repeatedly_does_not_re_query(self):
        from core import member_names

        counter = {"n": 0}
        with mock.patch.object(settings, "get_client", return_value=_Client(counter, rows=[])), \
             mock.patch.object(member_names.dropdowns, "get", return_value=[]):
            for _ in range(40):
                member_names.normalize_member_name("Ada Nwosu")
        # One query per distinct unset key (nicknames, aliases) — not one per name.
        self.assertLessEqual(counter["n"], 4,
                             f"{counter['n']} store reads for 40 name lookups")


if __name__ == "__main__":
    unittest.main(verbosity=2)
