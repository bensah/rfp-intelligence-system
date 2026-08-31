"""MUST-1 registration is transfer-aware for a Sub (P3).

A US-federal call requires US incorporation. A country-team org applying as a **Sub**
is not itself US-registered — but the Prime (or its parent org) is, and THAT is who must
be registered. So:
  * with an applicant graph, registration is covered if self / parent / prime is
    US-registered → score 1.0 → MUST-1 "Yes, fully";
  * a Sub we could not confirm scores 0.5 "unclear" (met None → NON-FATAL) → MUST-1
    "Mostly, one item unclear", NOT a hard "No, not eligible" auto-Decline;
  * a Prime (or graph-less caller) that is not US-registered still hard-fails at 0.

The graph-less path is asserted byte-for-byte unchanged (zero regression).

Run:  python -m unittest tests.test_must1_sub_registration_graph
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

# A US-federal call (grants.gov listing) — donor states no explicit registration region,
# so _is_us_federal derives "United States" for REGISTRATION. The WORK geography is
# in-country (Kenya), which the org covers, so MUST-4 passes and only registration is at
# issue — modelling the real case (US-federal money, work done in the country of operation).
# donor={} keeps every other MUST-1 item off.
US_FEDERAL_SUB = {"applicant_role": "sub", "funding_agency": "USDoS",
                  "opportunity_link": "https://www.grants.gov/x",
                  "call_geographic_scope": "Kenya",
                  "lead_applicant": "Prime Lead Org"}
DONOR: dict = {}
SELF = {"org_registered_countries": ["Kenya"],          # not US-registered
        "org_operating_countries": ["Kenya"]}           # but present in the work geography

CHILD_ID = "t-child"


def _rows(*, prime_consent=True, prime_us=True, parent_us=False):
    parent = {"id": "t-parent", "name": "Parent Org", "slug": "parent",
              "status": "active", "parent_tenant_id": None,
              "share_for_consortium_scoring": False,
              "org_profile": {"org_registered_countries": ["United States"]} if parent_us else {},
              "org_identity": {}}
    child = {"id": CHILD_ID, "name": "Child Team", "slug": "child",
             "status": "active", "parent_tenant_id": ("t-parent" if parent_us else None),
             "share_for_consortium_scoring": False, "org_profile": {}, "org_identity": {}}
    prime = {"id": "t-prime", "name": "Prime Lead Org", "slug": "prime-lead",
             "status": "active", "parent_tenant_id": None,
             "share_for_consortium_scoring": prime_consent,
             "org_profile": {"org_registered_countries": ["United States"]} if prime_us else {},
             "org_identity": {}}
    return [child, parent, prime]


def _reg(items):
    return next(x for x in items if x["key"] == "local_registration")


class SubRegistrationGraphTests(unittest.TestCase):
    def test_graph_none_is_unchanged_hard_fail(self):
        # No graph → self only, no soft floor → hard 0 → "No, not eligible" → fatal.
        it = _reg(CD.qualification_factors(SELF, US_FEDERAL_SUB, DONOR, {}))
        self.assertEqual(it["score"], 0.0)
        self.assertIs(it["met"], False)
        self.assertEqual(CD.derive_qualification(SELF, US_FEDERAL_SUB, DONOR, {}),
                         "No, not eligible")
        self.assertTrue(CD.fatal_decline(SELF, US_FEDERAL_SUB, DONOR, {})[0])

    def test_sub_unconfirmed_is_soft_unclear_and_non_fatal(self):
        # Prime named but not consented → unresolved → Sub soft-0.5, non-fatal.
        g = AG.build_graph(US_FEDERAL_SUB, SELF, CHILD_ID, _rows(prime_consent=False))
        self.assertTrue(g.unresolved_prime)
        it = _reg(CD.qualification_factors(SELF, US_FEDERAL_SUB, DONOR, {}, graph=g))
        self.assertEqual(it["score"], 0.5)
        self.assertIsNone(it["met"])
        self.assertEqual(CD.derive_qualification(SELF, US_FEDERAL_SUB, DONOR, {}, graph=g),
                         "Mostly, one item unclear")
        self.assertFalse(CD.fatal_decline(SELF, US_FEDERAL_SUB, DONOR, {}, graph=g)[0])

    def test_sub_inherits_registered_prime(self):
        g = AG.build_graph(US_FEDERAL_SUB, SELF, CHILD_ID, _rows(prime_consent=True, prime_us=True))
        self.assertIsNotNone(g.prime)
        it = _reg(CD.qualification_factors(SELF, US_FEDERAL_SUB, DONOR, {}, graph=g))
        self.assertEqual(it["score"], 1.0)
        self.assertEqual(CD.derive_qualification(SELF, US_FEDERAL_SUB, DONOR, {}, graph=g),
                         "Yes, fully")
        self.assertFalse(CD.fatal_decline(SELF, US_FEDERAL_SUB, DONOR, {}, graph=g)[0])

    def test_sub_inherits_registered_parent(self):
        g = AG.build_graph(US_FEDERAL_SUB, SELF, CHILD_ID,
                           _rows(prime_consent=False, parent_us=True))
        self.assertIsNotNone(g.parent)
        it = _reg(CD.qualification_factors(SELF, US_FEDERAL_SUB, DONOR, {}, graph=g))
        self.assertEqual(it["score"], 1.0)                  # parent's US registration inherited

    def test_prime_not_registered_still_hard_fails(self):
        rfp = dict(US_FEDERAL_SUB, applicant_role="prime")   # WE are the Prime
        g = AG.build_graph(rfp, SELF, CHILD_ID, _rows(prime_consent=False))
        it = _reg(CD.qualification_factors(SELF, rfp, DONOR, {}, graph=g))
        self.assertEqual(it["score"], 0.0)                  # a Prime must itself be registered
        self.assertIs(it["met"], False)
        self.assertTrue(CD.fatal_decline(SELF, rfp, DONOR, {}, graph=g)[0])


if __name__ == "__main__":
    unittest.main()
