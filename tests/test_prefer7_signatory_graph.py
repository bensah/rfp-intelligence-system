"""MUST-5 signatory + PREFER-7 relationships are transfer-aware (P4).

A child Sub can inherit, from the parent org / Prime / a co-Sub:
  * MUST-5 "Authorized signatory (this donor)" — a signatory the family already holds;
  * PREFER-7 grantee / shared-collaborator / engaged — a relationship the family holds.

HONEST: when NO consulted profile holds it, the score stays 0 / the label stays None —
the graph can only raise. The graph-less path is byte-for-byte unchanged.

All names here are SYNTHETIC or public funders — no real org identity.

Run:  python -m unittest tests.test_prefer7_signatory_graph
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
RFP = {"applicant_role": "sub", "funding_agency": "US Department of State"}
DONOR = {"donor": "US Department of State",
         "donor_authorized_signatory_signoff_required": "yes"}   # activates the signatory gate
SELF: dict = {}                                             # child holds nothing itself


def _rows(parent_profile):
    return [
        {"id": CHILD_ID, "name": "Child Team", "slug": "child", "status": "active",
         "parent_tenant_id": "t-parent", "share_for_consortium_scoring": False,
         "org_profile": {}, "org_identity": {}},
        {"id": "t-parent", "name": "Parent Org", "slug": "parent", "status": "active",
         "parent_tenant_id": None, "share_for_consortium_scoring": False,
         "org_profile": parent_profile, "org_identity": {}},
    ]


PARENT_HOLDS = {"org_authorized_signatory_donors": ["US Department of State"],
                "org_funder_history": ["US Department of State"]}


def _sig(items):
    return next(x for x in items if x["key"] == "authorized_signatory")


def _by_key(items):
    return {i["key"]: i for i in items}


class SignatoryRelationshipGraphTests(unittest.TestCase):
    # --- MUST-5 authorized signatory ------------------------------------------------
    def test_signatory_graph_none_is_self_only_zero(self):
        it = _sig(CD.compliance_factors(SELF, RFP, DONOR, {}))
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)                 # child holds none → honest 0

    def test_signatory_inherited_from_parent(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows(PARENT_HOLDS))
        it = _sig(CD.compliance_factors(SELF, RFP, DONOR, {}, graph=g))
        self.assertEqual(it["score"], 1.0)                 # parent's signatory inherited

    def test_signatory_stays_zero_when_no_one_holds_it(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows({}))  # parent holds nothing
        it = _sig(CD.compliance_factors(SELF, RFP, DONOR, {}, graph=g))
        self.assertEqual(it["score"], 0.0)                 # not forced positive

    def test_cofinancing_label_reflects_inherited_signatory(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows(PARENT_HOLDS))
        # signatory is the only active MUST-5 gate here → inherited pass → "Yes, fully met".
        self.assertEqual(CD.derive_cofinancing(SELF, RFP, DONOR, org_settings={}, graph=g),
                         "Yes, fully met")
        self.assertEqual(CD.derive_cofinancing(SELF, RFP, DONOR, org_settings={}),
                         "Not met")                         # graph-less: self holds none

    # --- PREFER-7 relationships ------------------------------------------------------
    def test_prefer7_grantee_inherited_label_and_component(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows(PARENT_HOLDS))
        self.assertEqual(CD.derive_funder_relationship(SELF, RFP, DONOR, graph=g),
                         "Current/past grantee")
        rel = _by_key(CD._relationship_factors(SELF, RFP, DONOR, graph=g))
        self.assertTrue(rel["rel_grantee"]["met"])

    def test_prefer7_graph_none_is_self_only(self):
        # Child holds no relationship data at all → None (not determinable), unchanged.
        self.assertIsNone(CD.derive_funder_relationship(SELF, RFP, DONOR))
        rel = _by_key(CD._relationship_factors(SELF, RFP, DONOR))
        self.assertFalse(rel["rel_grantee"]["met"])

    def test_label_and_components_agree_under_graph(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows(PARENT_HOLDS))
        label = CD.derive_funder_relationship(SELF, RFP, DONOR, graph=g)
        rel = _by_key(CD._relationship_factors(SELF, RFP, DONOR, graph=g))
        self.assertEqual(label, "Current/past grantee")
        self.assertTrue(rel["rel_grantee"]["met"])          # never disagree


if __name__ == "__main__":
    unittest.main()
