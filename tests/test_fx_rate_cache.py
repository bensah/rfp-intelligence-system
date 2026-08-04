"""Regression tests for the FX rate cache (core.fx._erapi_rate).

This is a BLOCKING outbound HTTP call sitting inside the per-row scoring path
(criteria_derive._usd → dropdowns.usd_rate → _erapi_rate), so it is charged once per
distinct currency on every screen that scores rows.

It used to be an @st.cache_data(ttl=6h). The app calls st.cache_data.clear() in ~37 places
(after any write / registry edit / sync), and each one ALSO wiped the FX cache — so the next
render re-fetched every currency over the network (~1.7s each, up to the timeout), which is
minutes on a data-heavy page. It is now a module-level TTL cache: immune to
st.cache_data.clear(), shared across sessions in the process, and it never spends a network
call on a value that can't be an ISO code.

Run:  python -m unittest tests.test_fx_rate_cache
"""
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import fx    # noqa: E402


class _Resp:
    status_code = 200

    def __init__(self, usd=1.25):
        self._usd = usd

    def json(self):
        return {"result": "success", "rates": {"USD": self._usd},
                "time_last_update_utc": "Mon, 04 Aug 2026 00:00:00 +0000"}


class FxRateCacheTests(unittest.TestCase):
    def setUp(self):
        fx._RATE_CACHE.clear()

    def test_usd_needs_no_network(self):
        with mock.patch("httpx.get", side_effect=AssertionError("should not call out")):
            self.assertEqual(fx._erapi_rate("USD")[0], 1.0)

    def test_rate_is_fetched_once_then_cached(self):
        with mock.patch("httpx.get", return_value=_Resp(1.25)) as g:
            self.assertEqual(fx._erapi_rate("GBP")[0], 1.25)
            self.assertEqual(fx._erapi_rate("GBP")[0], 1.25)
            self.assertEqual(fx._erapi_rate("GBP")[0], 1.25)
        self.assertEqual(g.call_count, 1, "cached rate must not re-hit the network")

    def test_cache_survives_st_cache_data_clear(self):
        # THE REGRESSION: the old @st.cache_data version was wiped by any of the ~37
        # st.cache_data.clear() calls, forcing a full re-fetch on the next render.
        with mock.patch("httpx.get", return_value=_Resp(1.1)) as g:
            fx._erapi_rate("EUR")
            try:
                import streamlit as st
                st.cache_data.clear()
            except Exception:
                pass
            fx._erapi_rate("EUR")
        self.assertEqual(g.call_count, 1)

    def test_non_iso_junk_never_hits_the_network(self):
        # Free-typed cells in the wild ("USD $", "Euro €") normalise to a bare token that
        # may not be an ISO code. Anything that can't be one must short-circuit BEFORE the
        # network — otherwise every such row burns a request that can only fail.
        calls = mock.Mock(side_effect=AssertionError("should not call out"))
        with mock.patch("httpx.get", calls):
            for junk in ("US$", "£", "XX", "TOOLONG", "E U R", "123"):
                self.assertEqual(fx._erapi_rate(junk), (None, None), junk)
        self.assertEqual(calls.call_count, 0)

    def test_alias_maps_to_a_real_code(self):
        with mock.patch("httpx.get", return_value=_Resp(1.15)) as g:
            self.assertEqual(fx._erapi_rate("EURO")[0], 1.15)     # → EUR, one call
            self.assertEqual(fx._erapi_rate("EUR")[0], 1.15)      # same cache entry
        self.assertEqual(g.call_count, 1, "alias must share the canonical code's entry")

    def test_dollar_aliases_short_circuit(self):
        with mock.patch("httpx.get", side_effect=AssertionError("should not call out")):
            for v in ("DOLLARS", "DOLLAR", "US"):
                self.assertEqual(fx._erapi_rate(v)[0], 1.0)

    def test_failure_is_negative_cached(self):
        with mock.patch("httpx.get", side_effect=RuntimeError("network down")) as g:
            self.assertEqual(fx._erapi_rate("ZZZ")[0], None)
            self.assertEqual(fx._erapi_rate("ZZZ")[0], None)
        self.assertEqual(g.call_count, 1, "a failing currency must not be retried every row")

    def test_timeout_is_bounded(self):
        # A page render must never hang on a third-party API.
        self.assertLessEqual(fx._HTTP_TIMEOUT, 5.0)
        captured = {}
        with mock.patch("httpx.get", return_value=_Resp()) as g:
            fx._erapi_rate("SEK")
            captured = g.call_args.kwargs
        self.assertEqual(captured.get("timeout"), fx._HTTP_TIMEOUT)

    def test_blank_currency_is_treated_as_usd(self):
        with mock.patch("httpx.get", side_effect=AssertionError("should not call out")):
            self.assertEqual(fx._erapi_rate("")[0], 1.0)
            self.assertEqual(fx._erapi_rate(None)[0], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
