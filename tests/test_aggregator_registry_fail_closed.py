"""The aggregator gate, against the hosts that actually got through it.

Three republishers put rows into the pipeline while every guard reported
normally: grantedai.com and globalscholardesk.com (unrecognised, so 'unknown',
which fails open) and fundsforngospremium.com — whose parent domain
fundsforngos.org WAS on the blocklist the whole time. A plain substring test
cannot see past the TLD the needle carries, so the paywalled sibling was a
different host as far as the matcher was concerned.

These tests pin the matcher's two directions apart, because that asymmetry is
the whole design: the blocklist may over-match (a human reinstates from the
reject log), the allowlist may not (a false positive there readmits every
republisher sharing a prefix).
"""
from __future__ import annotations

import unittest

from core import aggregators


class _NoRegistry(unittest.TestCase):
    """Test the CODE's own verdict, with the human registry stubbed out.

    `classify` consults core.source_registry first and a confirmed row wins by
    design, so an unstubbed test would assert against live database state — and
    would change verdict when someone classifies a host. (It already does: the
    owner has confirmed reliefweb.int as 'aggregator', which correctly overrides
    the _KNOWN_PRIMARY entry for it.) Everything here is about the fallback
    logic, so the registry is silenced.
    """

    def setUp(self):
        self._orig = aggregators.confirmed_class
        aggregators.confirmed_class = lambda _host: None

    def tearDown(self):
        aggregators.confirmed_class = self._orig


class KnownRepublishersAreBlocked(_NoRegistry):
    """Every host observed leaking into the pipeline now classifies aggregator."""

    LEAKED = {
        # host                            rows it put into rfp_submissions
        "grantedai.com": 3,
        "globalscholardesk.com": 1,
        "fundsforngospremium.com": 4,
        "advance-africa.com": 1,
        "australiantenders.com.au": 1,
    }

    def test_each_leaked_host_is_an_aggregator(self):
        for host, rows in self.LEAKED.items():
            with self.subTest(host=host, rows=rows):
                kind, why = aggregators.classify(f"https://{host}/grants/some-call")
                self.assertEqual(kind, "aggregator", f"{host} ({rows} rows) -> {why}")
                self.assertTrue(aggregators.is_aggregator(f"https://{host}/x"))
                self.assertTrue(aggregators.is_non_primary(f"https://{host}/x")[0])

    def test_subdomains_of_a_republisher_are_blocked_too(self):
        # home.fundsforngospremium.com carried 12 registry hits of its own.
        for url in ("https://home.fundsforngospremium.com/premium-call",
                    "https://www.grantedai.com/grants/x",
                    "https://blog.globalscholardesk.com/x"):
            with self.subTest(url=url):
                self.assertEqual(aggregators.classify(url)[0], "aggregator")


class BrandStemMatching(_NoRegistry):
    """A long brand stem matches an operator's other domains; a short one does not."""

    def test_stem_catches_the_paywalled_sibling(self):
        # The needle on the list is "fundsforngos.org"; the leak was on .com with
        # "premium" appended. Both must land on the same verdict.
        self.assertEqual(aggregators.classify("https://fundsforngos.org/x")[0],
                         "aggregator")
        self.assertEqual(aggregators.classify("https://fundsforngospremium.com/x")[0],
                         "aggregator")

    def test_short_stems_are_not_prefix_matched(self):
        # "candid.org" and "devex.com" are on the list. Their stems are below
        # _STEM_MIN, so an unrelated host that merely starts with those letters
        # must NOT be swept up.
        self.assertLess(len("candid"), aggregators._STEM_MIN)
        self.assertLess(len("devex"), aggregators._STEM_MIN)
        for url in ("https://candidature-recherche.fr/appel",
                    "https://devexpress-foundation.org/grants"):
            with self.subTest(url=url):
                self.assertNotEqual(aggregators.classify(url)[0], "aggregator")

    def test_the_listed_short_domains_still_match_exactly(self):
        for url in ("https://candid.org/x", "https://www.devex.com/x",
                    "https://sub.candid.org/x"):
            with self.subTest(url=url):
                self.assertEqual(aggregators.classify(url)[0], "aggregator")


class AllowlistIsStricterThanBlocklist(_NoRegistry):
    """_KNOWN_PRIMARY must match on a label boundary and never by prefix.

    This is the direction that cannot be allowed to over-match: whitelisting a
    lookalike host turns the whole gate off for it.
    """

    def test_lookalike_hosts_do_not_inherit_primary(self):
        for url in ("https://notgrants.gov/x", "https://grants.gov.evil.com/x",
                    "https://grantsomething.gov/x", "https://sam.gov.example.net/x",
                    "https://reliefweb.int.mirror.org/x"):
            with self.subTest(url=url):
                self.assertNotEqual(
                    aggregators.classify(url)[0], "primary",
                    f"{url} must not be treated as a known primary portal")

    def test_the_real_primary_portals_and_their_subdomains_still_pass(self):
        for url in ("https://grants.gov/search-results-detail/123",
                    "https://www.sam.gov/opp/x", "https://reliefweb.int/job/1",
                    "https://labs.reliefweb.int/x"):
            with self.subTest(url=url):
                self.assertEqual(aggregators.classify(url)[0], "primary")


class BlogPlatformsStillMatchAsSuffixes(_NoRegistry):
    """The change to _host_hits must not lose the blogspot case it was built for."""

    def test_grants_gov_blogspot_is_still_a_blog(self):
        self.assertEqual(
            aggregators.classify("https://grants-gov.blogspot.com/2026/01/x")[0],
            "blog")

    def test_other_blog_platforms(self):
        for url in ("https://someone.wordpress.com/post",
                    "https://x.substack.com/p/y", "https://a.wixsite.com/b"):
            with self.subTest(url=url):
                self.assertEqual(aggregators.classify(url)[0], "blog")


class GenuineFundersAreUntouched(_NoRegistry):
    """The blocklist must not sweep up donor sites that reached the pipeline."""

    FUNDERS = ("wellcome.org", "idrc-crdi.ca", "afdb.org", "mmv.org",
               "grandchallenges.org", "gcgh.grandchallenges.org", "norad.no",
               "enabel.be", "finnpartnership.fi", "tballiance.org",
               "mesamalaria.org", "coefficientgiving.org", "ec.europa.eu")

    def test_donor_hosts_are_never_classified_aggregator_or_blog(self):
        for host in self.FUNDERS:
            with self.subTest(host=host):
                kind = aggregators.classify(f"https://{host}/funding/a-call")[0]
                self.assertNotIn(kind, ("aggregator", "blog"),
                                 f"{host} would be blocked as {kind}")


class AcceptanceIsNotEvidenceOfPrimary(unittest.TestCase):
    """An accepted UNKNOWN host must stay unknown, not become 'primary'.

    The registry used to promote any host whose candidate was accepted. That is
    circular — an unrecognised republisher is accepted BECAUSE 'unknown' fails
    open — and it left grantedai.com sitting as classification='primary' over 26
    hits, one bulk-confirm away from authoritative via confirmed_class(). 38 of
    the 40 pending-'primary' rows in the live registry came from this rule.
    """

    def test_the_promotion_branch_is_gone(self):
        # Behaviour-level proof without a database: the aggregation no longer has
        # any path from `accepted` to 'primary'. Assert on the source of the
        # branch that did it, which is the thing that must not come back.
        import inspect

        import core.source_registry as sr
        src = inspect.getsource(sr.record_encounters)
        self.assertNotIn('a["detected"] = "primary"', src,
                         "acceptance must never promote a host to 'primary'")

    def test_an_unknown_host_stays_unknown(self):
        import core.source_registry as sr
        # The aggregation is internal, so drive it and inspect what it would have
        # written by intercepting the insert payload.
        written: list[dict] = []

        class _T:
            def insert(self, rows):
                written.extend(rows)
                return self

            def update(self, _):
                return self

            def eq(self, *_a):
                return self

            def select(self, *_a):
                return self

            def execute(self):
                class R:
                    data: list = []
                return R()

        class _C:
            def table(self, _name):
                return _T()

        orig_client, orig_all = sr.get_client, sr.get_all
        sr.get_client = lambda: _C()
        sr.get_all = lambda force=False: {}
        try:
            sr.record_encounters([{
                "url": "https://brand-new-republisher.example/grants/x",
                "title": "Some Call", "detected": "unknown", "accepted": True,
            }])
        finally:
            sr.get_client, sr.get_all = orig_client, orig_all

        self.assertTrue(written, "expected a pending row to be inserted")
        row = written[0]
        self.assertEqual(row["classification"], "unknown",
                         "an accepted unknown host must not be labelled primary")
        self.assertEqual(row["status"], "pending")


if __name__ == "__main__":
    unittest.main()
