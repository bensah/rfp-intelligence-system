"""New rows must arrive populated, not wait for somebody to remember the backfill.

The nine §4 narrative/eligibility fields had a writer that only ever ran from a script. So every
row a scan added arrived EMPTY — and the opportunity page showed dashes for the newest calls,
which are the ones anyone is actually looking at.

Synthesis now runs inside the store path, before the upsert, so one write lands a populated row.
This is the INGEST path and a model call is ~12 seconds, so most of what is tested here is the
bounds:

  * a per-scan ceiling, and an off switch
  * nothing re-paid for on a re-scan. `build_record` rebuilds these fields as None every time,
    so without reading the stored row first, every weekly scan would re-synthesise the whole
    catalogue.
  * a synthesis failure never costs the scan the row it just extracted

No network: the synthesiser and the store are stubbed.
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

from core import extract as EX                     # noqa: E402

SYNTH = {"full_description": "What this call funds.",
         "what_is_funded": "Equipment\nTraining",
         "applicant_fit_profile": "An established NGO."}


def _rec(**kw):
    r = {"uid": "es_new", "opportunity_name": "A Health Call",
         "opportunity_url": "https://funder.example/c/1",
         "raw_text": "A long enough body of call text to be worth reading. " * 12}
    r.update(kw)
    return r


class ANewRowArrivesPopulatedTests(unittest.TestCase):
    def setUp(self):
        EX.reset_scan_synthesis()

    def test_the_fields_are_on_the_record_before_it_is_written(self):
        written = {}
        with mock.patch("core.catalog_synthesis.is_enabled", return_value=True), \
             mock.patch("core.catalog_synthesis.synthesize_row", return_value=dict(SYNTH)), \
             mock.patch.object(EX.extracted_store, "get_extracted", return_value=None), \
             mock.patch.object(EX.extracted_store, "upsert_extracted",
                               side_effect=lambda rec: written.update(rec) or "es_new"), \
             mock.patch.object(EX, "build_record", return_value=(_rec(), "ok")):
            uid, _reason = EX.extract_and_store({}, {})
        self.assertEqual(uid, "es_new")
        # ONE write, already carrying the synthesis — not an empty row plus a later pass.
        self.assertEqual(written.get("full_description"), "What this call funds.")
        self.assertEqual(EX.scan_synthesis_calls(), 1)

    def test_the_row_is_still_stored_when_synthesis_fails(self):
        # A rate limit must not cost the scan the row it just extracted.
        with mock.patch("core.catalog_synthesis.is_enabled", return_value=True), \
             mock.patch("core.catalog_synthesis.synthesize_row",
                        side_effect=RuntimeError("rate limited")), \
             mock.patch.object(EX.extracted_store, "get_extracted", return_value=None), \
             mock.patch.object(EX.extracted_store, "upsert_extracted", return_value="es_new"), \
             mock.patch.object(EX, "build_record", return_value=(_rec(), "ok")):
            uid, _reason = EX.extract_and_store({}, {})
        self.assertEqual(uid, "es_new")
        self.assertEqual(EX.scan_synthesis_calls(), 0)

    def test_a_gate_reject_never_reaches_the_synthesiser(self):
        with mock.patch("core.catalog_synthesis.synthesize_row") as synth, \
             mock.patch.object(EX, "build_record", return_value=(None, "gate: off-theme")):
            uid, reason = EX.extract_and_store({}, {})
        self.assertIsNone(uid)
        self.assertEqual(reason, "gate: off-theme")
        synth.assert_not_called()


class NothingIsPaidForTwiceTests(unittest.TestCase):
    """`build_record` rebuilds these fields as None every time, so on a re-scan they LOOK
    missing even when the stored row has them. Without reading the stored row first, every
    weekly scan would re-synthesise the whole catalogue."""

    def setUp(self):
        EX.reset_scan_synthesis()

    def test_a_row_already_synthesised_costs_no_call(self):
        stored = {"uid": "es_new", "full_description": "Already written.",
                  "what_is_funded": "Equipment"}
        with mock.patch("core.catalog_synthesis.is_enabled", return_value=True), \
             mock.patch("core.catalog_synthesis.synthesize_row") as synth, \
             mock.patch.object(EX.extracted_store, "get_extracted", return_value=stored), \
             mock.patch.object(EX.extracted_store, "upsert_extracted", return_value="es_new"), \
             mock.patch.object(EX, "build_record", return_value=(_rec(), "ok")):
            EX.extract_and_store({}, {})
        synth.assert_not_called()
        self.assertEqual(EX.scan_synthesis_calls(), 0)

    def test_the_stored_values_are_carried_so_the_upsert_cannot_regress_them(self):
        stored = {"uid": "es_new", "full_description": "Already written.",
                  "submission_format": "Online portal"}
        written = {}
        with mock.patch("core.catalog_synthesis.is_enabled", return_value=True), \
             mock.patch.object(EX.extracted_store, "get_extracted", return_value=stored), \
             mock.patch.object(EX.extracted_store, "upsert_extracted",
                               side_effect=lambda rec: written.update(rec) or "es_new"), \
             mock.patch.object(EX, "build_record", return_value=(_rec(), "ok")):
            EX.extract_and_store({}, {})
        self.assertEqual(written.get("full_description"), "Already written.")
        self.assertEqual(written.get("submission_format"), "Online portal")


class TheScanIsBoundedTests(unittest.TestCase):
    def setUp(self):
        EX.reset_scan_synthesis()

    def _run_one(self):
        with mock.patch("core.catalog_synthesis.is_enabled", return_value=True), \
             mock.patch("core.catalog_synthesis.synthesize_row", return_value=dict(SYNTH)), \
             mock.patch.object(EX.extracted_store, "get_extracted", return_value=None), \
             mock.patch.object(EX.extracted_store, "upsert_extracted", return_value="es_new"), \
             mock.patch.object(EX, "build_record", return_value=(_rec(), "ok")):
            EX.extract_and_store({}, {})

    def test_the_per_scan_ceiling_stops_it(self):
        with mock.patch.object(EX, "_SCAN_SYNTH_MAX", 2):
            for _ in range(5):
                self._run_one()
        self.assertEqual(EX.scan_synthesis_calls(), 2)

    def test_zero_turns_scan_time_synthesis_off(self):
        with mock.patch.object(EX, "_SCAN_SYNTH_MAX", 0), \
             mock.patch("core.catalog_synthesis.synthesize_row") as synth:
            self._run_one()
        synth.assert_not_called()

    def test_the_ceiling_is_a_per_scan_budget_not_per_process(self):
        # A long-lived worker must not carry a spent counter into the next scan and silently
        # stop synthesising.
        with mock.patch.object(EX, "_SCAN_SYNTH_MAX", 1):
            self._run_one()
            self.assertEqual(EX.scan_synthesis_calls(), 1)
            EX.reset_scan_synthesis()
            self._run_one()
        self.assertEqual(EX.scan_synthesis_calls(), 1)

    def test_the_default_ceiling_is_modest(self):
        # ~12s a call on the free tier, so the default has to be a few minutes, not an hour.
        self.assertGreater(EX._SCAN_SYNTH_MAX, 0)
        self.assertLessEqual(EX._SCAN_SYNTH_MAX, 60)

    def test_a_disabled_model_costs_nothing(self):
        with mock.patch("core.catalog_synthesis.is_enabled", return_value=False), \
             mock.patch("core.catalog_synthesis.synthesize_row") as synth, \
             mock.patch.object(EX.extracted_store, "get_extracted", return_value=None), \
             mock.patch.object(EX.extracted_store, "upsert_extracted", return_value="es_new"), \
             mock.patch.object(EX, "build_record", return_value=(_rec(), "ok")):
            EX.extract_and_store({}, {})
        synth.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
