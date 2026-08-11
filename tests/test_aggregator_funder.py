"""An aggregator may identify an opportunity. It may never BE the source, or the funder.

The rule (owner, 2026-07-06): a listing on an aggregator is a search booster. We take the
title, find the call's OWN primary page, and extract THAT. The pipeline already enforced
half of it — an aggregator URL is never stored, and a hit that fails to resolve is dropped.

The other half leaked. Resolution replaced the URL and left `funding_agency` alone, so the
aggregator's own label rode into the store as the donor: 20 catalogue rows and 7 pipeline
rows read "DevelopmentAid Aggregator", "FundsForNGOs", or even a bare host such as
"www2.fundsforngos.org" as the funder. That string is what a reviewer reads on the
opportunity page and in the opportunity rail, so the call appeared to be funded by the
aggregator that merely listed it.

Two changes are locked down here:

  * the funder is re-derived FROM THE RESOLVED PAGE — the curated registry name first
    (a human wrote it), then og:site_name, then the site name in the <title> tail
  * resolution PREFERS a host the registry already classes as a primary source, because a
    curated list beats guessing from how a domain is spelled

No network: the search and the fetch are stubbed.
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

from bs4 import BeautifulSoup                        # noqa: E402
from core import source_resolver as SR               # noqa: E402


class AnAggregatorIsNeverTheFunderTests(unittest.TestCase):
    def test_the_aggregator_labels_are_recognised(self):
        for name in ("DevelopmentAid Aggregator", "DevelopmentAid Grants Aggregator",
                     "FundsForNGOs", "fundsforngos.org", "Devex", "GrantWatch",
                     "Instrumentl", "GrantStation"):
            with self.subTest(name=name):
                self.assertTrue(SR.is_aggregator_funder(name))

    def test_a_bare_host_is_not_a_funder_either(self):
        # The scraper falling back to the host produced these verbatim.
        for name in ("www2.fundsforngos.org", "https://example.org/",
                     "portal.example.com", "some-site.co.uk"):
            with self.subTest(name=name):
                self.assertTrue(SR.is_aggregator_funder(name))

    def test_a_real_funder_name_is_left_alone(self):
        for name in ("A National Research Council", "The Example Foundation",
                     "Ministry of Health", "An Regional Development Bank",
                     "Global Health Programme (Framework)"):
            with self.subTest(name=name):
                self.assertFalse(SR.is_aggregator_funder(name))

    def test_a_blank_funder_is_not_flagged_as_an_aggregator(self):
        for name in (None, "", "   "):
            self.assertFalse(SR.is_aggregator_funder(name))


PAGE = ("<html><head><title>Call for Proposals | The Example Foundation</title>"
        "<meta property='og:site_name' content='The Example Foundation'></head>"
        "<body><p>Applications close 30 September 2026.</p></body></html>")


class _Resp:
    def __init__(self, text=PAGE):
        self.text, self.status_code = text, 200

    def raise_for_status(self):
        return None


def _run(cand, *, resolved="https://examplefoundation.org/calls/1", page=PAGE,
         curated=None):
    with mock.patch.object(SR, "resolve", return_value=resolved), \
         mock.patch("requests.get", return_value=_Resp(page)), \
         mock.patch("core.source_registry.primary_donor_name", return_value=curated):
        return SR.resolve_and_enrich(cand)


class TheFunderComesFromTheResolvedPageTests(unittest.TestCase):
    def _cand(self, **kw):
        c = {"opportunity_title": "Call for Proposals: A Research Programme",
             "opportunity_link": "https://www.developmentaid.org/grants/view/1",
             "funding_agency": "DevelopmentAid Aggregator"}
        c.update(kw)
        return c

    def test_the_aggregator_label_is_replaced(self):
        c = self._cand()
        self.assertTrue(_run(c))
        self.assertEqual(c["funding_agency"], "The Example Foundation")

    def test_the_curated_registry_name_wins_over_the_page(self):
        c = self._cand()
        _run(c, curated="A Curated Foundation Name")
        self.assertEqual(c["funding_agency"], "A Curated Foundation Name")

    def test_the_original_label_is_kept_for_audit(self):
        c = self._cand()
        _run(c)
        self.assertEqual(c["_funder_from_aggregator"], "DevelopmentAid Aggregator")
        self.assertEqual(c["_aggregator_link"],
                         "https://www.developmentaid.org/grants/view/1")

    def test_a_bare_host_funder_is_replaced_too(self):
        c = self._cand(funding_agency="www2.fundsforngos.org")
        _run(c)
        self.assertEqual(c["funding_agency"], "The Example Foundation")

    def test_a_blank_funder_is_filled(self):
        c = self._cand(funding_agency=None)
        _run(c)
        self.assertEqual(c["funding_agency"], "The Example Foundation")

    def test_A_REAL_FUNDER_NAME_IS_NOT_OVERWRITTEN(self):
        # The listing sometimes gets the funder right. A page's <title> tail is a weaker
        # signal than that, so it must not be traded in.
        c = self._cand(funding_agency="A National Research Council")
        _run(c)
        self.assertEqual(c["funding_agency"], "A National Research Council")

    def test_the_title_tail_is_used_when_there_is_no_og_site_name(self):
        page = "<html><head><title>Grant call - A Second Foundation</title></head></html>"
        c = self._cand()
        _run(c, page=page)
        self.assertEqual(c["funding_agency"], "A Second Foundation")

    def test_an_aggregator_name_on_the_page_is_not_accepted(self):
        # Resolving to another aggregator must not launder the name through og:site_name.
        page = ("<html><head><meta property='og:site_name' content='FundsForNGOs'>"
                "<title>x</title></head></html>")
        c = self._cand()
        _run(c, page=page)
        self.assertEqual(c["funding_agency"], "DevelopmentAid Aggregator")   # unchanged

    def test_nothing_derivable_leaves_the_field_untouched(self):
        page = "<html><head><title>x</title></head><body>y</body></html>"
        c = self._cand()
        _run(c, page=page)
        self.assertEqual(c["funding_agency"], "DevelopmentAid Aggregator")
        # ...and the pipeline's backstop is what then drops the row.
        self.assertTrue(SR.is_aggregator_funder(c["funding_agency"]))

    def test_a_failed_resolution_changes_nothing(self):
        c = self._cand()
        self.assertFalse(_run(c, resolved=None))
        self.assertEqual(c["opportunity_link"],
                         "https://www.developmentaid.org/grants/view/1")


class TheCuratedPrimaryListIsPreferredTests(unittest.TestCase):
    """73 hosts are classed "Primary source" in the registry, 59 carrying the funder's
    name. That list is curated; `_score_domain` only knows how a domain is spelled."""

    def test_a_registry_primary_gets_the_bonus(self):
        with mock.patch("core.source_registry.is_registry_primary", return_value=True):
            self.assertEqual(SR._registry_bonus("https://known.example/x"),
                             SR._REGISTRY_PRIMARY_BONUS)

    def test_an_unknown_host_gets_none(self):
        with mock.patch("core.source_registry.is_registry_primary", return_value=False):
            self.assertEqual(SR._registry_bonus("https://unknown.example/x"), 0.0)

    def test_a_curated_host_is_acceptable_even_with_no_name_overlap(self):
        # THE GUARANTEE THAT MATTERS. A funder whose domain does not echo its name scores
        # nothing on the spelling heuristic and used to fall below the acceptance
        # threshold, so resolution gave up and the aggregator hit was dropped. Registry
        # membership alone now clears it.
        # The host must echo NOTHING of the name for this to test what it claims.
        tokens = SR._name_tokens("A Research Programme", "An Unrelated Funder")
        url = "https://q7z-portal.int/calls/1"
        plain = SR._score_domain(url, tokens)
        with mock.patch("core.source_registry.is_registry_primary", return_value=True):
            curated = plain + SR._registry_bonus(url)
        self.assertLess(plain, SR._MIN_DOMAIN_SCORE)          # rejected before
        self.assertGreaterEqual(curated, SR._MIN_DOMAIN_SCORE)  # accepted now

    def test_a_curated_host_outranks_an_equally_anonymous_unknown_one(self):
        tokens = SR._name_tokens("A Research Programme", "An Opaque Funder")
        a, b = "https://curated.int/x", "https://random.int/x"
        with mock.patch("core.source_registry.is_registry_primary",
                        side_effect=lambda u: "curated" in (u or "")):
            self.assertGreater(SR._score_domain(a, tokens) + SR._registry_bonus(a),
                               SR._score_domain(b, tokens) + SR._registry_bonus(b))

    def test_A_STRONG_NAME_MATCH_STILL_WINS_and_that_is_deliberate(self):
        # The bonus is ADDITIVE, not an override. The registry holds large portals that
        # each host thousands of unrelated calls, so "always prefer a curated host" would
        # resolve a small foundation's call to a government portal that never carried it.
        # Registry membership says the host is a legitimate primary; the name match is what
        # says the page is about THIS call, and relevance has to win.
        tokens = SR._name_tokens("Daylight Research Grant", "A Daylight Foundation")
        own = "https://adaylightfoundation.ch/calls/daylight-research"
        portal = "https://big-portal.gov/opportunities/12345"
        with mock.patch("core.source_registry.is_registry_primary",
                        side_effect=lambda u: "big-portal" in (u or "")):
            self.assertGreater(SR._score_domain(own, tokens) + SR._registry_bonus(own),
                               SR._score_domain(portal, tokens) + SR._registry_bonus(portal))

    def test_a_registry_lookup_failure_never_breaks_resolution(self):
        with mock.patch("core.source_registry.is_registry_primary",
                        side_effect=RuntimeError("db down")):
            self.assertEqual(SR._registry_bonus("https://x.example/y"), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
