"""MUST-4 geographic reach is consortium-transferable (P5b).

A Sub with no OWN presence in the work scope still delivers when the Prime / a co-Sub /
the parent operates there. That reads "Yes, via a partner" (0.5) — never our own 1.0 —
and moves the label off "No presence there", so the fatal geo gate no longer fires.
Own presence still wins; self-only when no graph (byte-for-byte).

Run:  python -m unittest tests.test_must4_geographic_consortium
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                       # noqa: E402
from core import applicant_graph as AG                       # noqa: E402

CHILD_ID = "t-child"
RFP = {"applicant_role": "sub", "call_geographic_scope": "United States",
       "lead_applicant": "Prime Lead Org"}
DONOR: dict = {}
SELF = {"org_registered_countries": ["Kenya"], "org_operating_countries": ["Kenya"]}


def _rows(*, prime_in_scope=True, prime_consent=True):
    return [
        {"id": CHILD_ID, "name": "Child Team", "slug": "child", "status": "active",
         "parent_tenant_id": None, "share_for_consortium_scoring": False,
         "org_profile": {}, "org_identity": {}},
        {"id": "t-prime", "name": "Prime Lead Org", "slug": "prime-lead", "status": "active",
         "parent_tenant_id": None, "share_for_consortium_scoring": prime_consent,
         "org_profile": {"org_operating_countries": ["United States"]} if prime_in_scope else
                        {"org_operating_countries": ["Ghana"]},
         "org_identity": {}},
    ]


def _geo(items):
    return next(x for x in items if x["key"] == "geo_presence")


class GeographicConsortiumTests(unittest.TestCase):
    def test_graph_none_is_no_presence_and_fatal(self):
        self.assertEqual(CD.derive_geographic_fit(SELF, RFP, {}, DONOR), "No presence there")
        self.assertEqual(_geo(CD._geo_factors(SELF, RFP, {}, DONOR))["score"], 0.0)
        self.assertTrue(CD.fatal_decline(SELF, RFP, DONOR, {})[0])

    def test_prime_in_scope_lifts_to_via_partner_and_clears_gate(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows(prime_in_scope=True))
        self.assertEqual(CD.derive_geographic_fit(SELF, RFP, {}, DONOR, graph=g),
                         "Yes, via a partner")
        self.assertEqual(_geo(CD._geo_factors(SELF, RFP, {}, DONOR, graph=g))["score"], 0.5)
        self.assertFalse(CD.fatal_decline(SELF, RFP, DONOR, {}, graph=g)[0])

    def test_prime_out_of_scope_stays_no_presence(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows(prime_in_scope=False))
        self.assertEqual(CD.derive_geographic_fit(SELF, RFP, {}, DONOR, graph=g),
                         "No presence there")           # honest — consortium not in scope either

    def test_unconsented_prime_does_not_transfer(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows(prime_consent=False))
        self.assertEqual(CD.derive_geographic_fit(SELF, RFP, {}, DONOR, graph=g),
                         "No presence there")

    def test_own_presence_still_wins(self):
        own = {"org_registered_countries": ["Kenya"], "org_operating_countries": ["United States"]}
        g = AG.build_graph(RFP, own, CHILD_ID, _rows(prime_in_scope=True))
        # Operating in scope → own tier (via operation), not downgraded to consortium.
        self.assertEqual(CD.derive_geographic_fit(own, RFP, {}, DONOR, graph=g),
                         "Yes, via a partner")           # own operating-country tier is 0.5


if __name__ == "__main__":
    unittest.main()
