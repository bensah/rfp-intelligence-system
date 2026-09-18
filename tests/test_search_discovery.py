"""Web-search discovery: the channel that reaches calls no registry entry can.

WHY IT EXISTS. The source registry is DONOR-KEYED — list a funder, crawl their site — and
that has a structural blind spot no amount of donor coverage closes: a call published on a
partner's PROGRAMME MICROSITE under the programme's name and never on the funder's own
domain. Three live calls were lost to it, each found only because a person shared a link:

  * Fondation Pierre Fabre's call, whose application calendar lived on odess.io;
  * UBS Optimus Foundation's "Outcomes Accelerator Cohort 5" mental-health CfP, published
    on outcomesaccelerator.org, reaching us two weeks before its EOI deadline;
  * The Audacious Project, which had no registry entry at all.

A search engine does not care whose domain a call sits on, which is exactly the property
the registry lacks.

WHAT THIS MODULE IS NOT. Not a second scraper, gate, or vocabulary. `web_search.search()`
already fans out across providers, expands one broad term over every health pivot, and
VERIFIES each hit by fetching the page and testing it with the same
`_RFP_STRONG_PHRASES` / `_has_rfp_acronym` / body-geography checks the gates use — dropping
confidently-expired, very-old-with-no-future-deadline, off-theme and foreign results. All
of that was reachable only from a button. This turns it into a scan phase.

SO A DISCOVERED CALL IS NOT PRIVILEGED. Its candidates are appended as ONE MORE BATCH in
run_scan's ingest list, so they take the identical route as a crawled candidate: same
gates, same enrichment, same dedup, same shared store, and then each tenant's own
screening. Measured on a live sweep: 14 queries → 136 raw → 30 verified → 25 candidates in
30s, of which the extraction gate kept 14 and rejected 11 (expired, consultancy,
news/blog, not-yet-open, non-call search pages).

Run:  python -m unittest tests.test_search_discovery
"""
import io
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import search_discovery as SD                               # noqa: E402


def _hit(link, title="Call for Proposals: Mental Health Outcomes",
         snippet="Request for proposals.", deadline="", page_date=""):
    return {"title": title, "link": link, "snippet": snippet,
            "domain": link.split("/")[2] if "//" in link else "", "deadline": deadline,
            "page_date": page_date}


def _search_ok(results, queries=("q1", "q2"), raw=20):
    return {"ok": True, "configured": True, "query": queries[0], "queries": list(queries),
            "providers": ["serper"], "raw_count": raw, "results": results, "error": None}


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("RFPIS_SEARCH_DISCOVERY", None)

    def _run(self, search_return, **kw):
        # Patch the REAL module's functions, not sys.modules. `discover` does
        # `from core import web_search`, which resolves the attribute on the `core`
        # PACKAGE - a patch.dict(sys.modules) swap does not touch it, so the mock was
        # ignored and these tests silently hit the live search API (41s of real
        # network, and one test's results changing another's outcome).
        from core import web_search as WS
        with (mock.patch.object(WS, "search", return_value=search_return) as m,
              mock.patch.object(WS, "available", return_value=True),
              mock.patch.object(SD, "enabled", return_value=True)):
            return SD.discover(**kw), m

    def test_a_verified_hit_becomes_a_candidate(self):
        out, _ = self._run(_search_ok([
            _hit("https://outcomesaccelerator.org/pipeline-acceleration-opportunities-2/",
                 deadline="2026-10-02")]))
        self.assertEqual(len(out["candidates"]), 1)
        c = out["candidates"][0]
        self.assertEqual(c["call_submission_deadline"], "2026-10-02")
        self.assertEqual(c["_source_origin"], SD.SOURCE_LABEL)

    def test_the_provenance_label_is_stamped(self):
        # "where did this come from" must stay answerable once it is in the store
        # alongside rows from registered sources.
        out, _ = self._run(_search_ok([_hit("https://example.org/a-call")]))
        self.assertIn("search", out["candidates"][0]["_source_origin"].lower())

    def test_duplicate_links_are_collapsed(self):
        # The same call surfaces under several pivots; paying for enrichment twice is waste.
        out, _ = self._run(_search_ok([
            _hit("https://example.org/call"), _hit("https://example.org/call/"),
            _hit("https://example.org/call#apply")]))
        self.assertEqual(len(out["candidates"]), 1)

    def test_non_http_links_are_dropped(self):
        out, _ = self._run(_search_ok([_hit("javascript:void(0)"), _hit("")]))
        self.assertEqual(out["candidates"], [])
        self.assertEqual(out["stats"]["no_link"], 2)

    def test_max_candidates_is_honoured(self):
        out, _ = self._run(
            _search_ok([_hit(f"https://example.org/c{i}") for i in range(40)]),
            max_candidates=5)
        self.assertEqual(len(out["candidates"]), 5)

    def test_rows_already_fresh_in_the_store_are_skipped_before_enrichment(self):
        # Enrichment is where the cost is, so the skip has to happen here, not after.
        from core import extracted_store as ES
        link = "https://example.org/known-call"
        with mock.patch.object(ES, "recent_uids", return_value={ES.make_uid(link)}):
            out, _ = self._run(_search_ok([_hit(link), _hit("https://example.org/new")]))
        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(out["stats"]["already_fresh"], 1)

    def test_an_aggregator_host_is_labelled_as_one(self):
        # A sweep hits DevelopmentAid constantly; mislabelling one as primary would
        # record the aggregator as the donor instead of resolving to the real source.
        out, _ = self._run(_search_ok([
            _hit("https://www.developmentaid.org/tenders/view/12345")]))
        self.assertEqual(out["candidates"][0]["_source_class"], "aggregator")

    def test_a_provisional_funder_is_filled_but_only_provisionally(self):
        out, _ = self._run(_search_ok([
            _hit("https://outcomesaccelerator.org/x")]))
        # Non-empty (dedup + the donor-intel join both read it) and honestly host-derived.
        self.assertEqual(out["candidates"][0]["funding_agency"], "Outcomesaccelerator")

    def test_a_search_provided_funder_wins_over_the_host_guess(self):
        h = _hit("https://outcomesaccelerator.org/x")
        h["funder"] = "UBS Optimus Foundation"
        out, _ = self._run(_search_ok([h]))
        self.assertEqual(out["candidates"][0]["funding_agency"], "UBS Optimus Foundation")


class ItFailsQuietlyAndSaysSoTests(unittest.TestCase):
    """A silent zero is the failure mode this whole area keeps producing."""

    def test_disabled_by_env_returns_nothing(self):
        os.environ["RFPIS_SEARCH_DISCOVERY"] = "0"
        try:
            out = SD.discover()
        finally:
            os.environ.pop("RFPIS_SEARCH_DISCOVERY", None)
        self.assertEqual(out["candidates"], [])
        self.assertFalse(out["stats"]["configured"])

    def test_a_provider_error_is_recorded_not_raised(self):
        from core import web_search as WS
        with (mock.patch.object(WS, "search", side_effect=RuntimeError("serper 429")),
              mock.patch.object(WS, "available", return_value=True),
              mock.patch.object(SD, "enabled", return_value=True)):
            out = SD.discover()
            out = SD.discover()
        self.assertEqual(out["candidates"], [])
        self.assertTrue(out["stats"]["errors"])
        self.assertIn("NOT RUN", SD.summarize(out["stats"]))

    def test_not_run_reads_differently_from_found_nothing(self):
        unconfigured = SD.summarize({"configured": False, "errors": []})
        ran_empty = SD.summarize({"configured": True, "queries": 14, "raw": 0,
                                  "verified": 0, "already_fresh": 0, "candidates": 0,
                                  "errors": []})
        self.assertIn("NOT RUN", unconfigured)
        self.assertNotIn("NOT RUN", ran_empty)
        self.assertIn("14 queries", ran_empty)


class ItIsWiredIntoTheScanTests(unittest.TestCase):
    def _src(self):
        with io.open(os.path.join(_ROOT, "scripts", "run_scan.py"),
                     encoding="utf-8") as fh:
            return fh.read()

    def test_the_flag_exists_and_reaches_run(self):
        src = self._src()
        self.assertIn('"--discover"', src)
        self.assertIn("discover=args.discover", src)

    def test_its_hits_go_through_the_normal_ingest(self):
        # The whole point: discovered candidates are one more BATCH, not a side door that
        # skips the gates. They must be appended to `scraped` before the ingest loop.
        src = self._src()
        append = src.index("search_discovery")
        ingest = src.index("_ingest_rank")
        self.assertLess(append, ingest,
                        "discovery must feed the existing sequential ingest")

    def test_source_health_ignores_the_search_batch(self):
        # The channel has no donor_sources row, so counting it would make the health
        # ratio read as a failed write every time discovery runs.
        src = self._src()
        self.assertIn("_n_reg", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
