"""Applicant-graph resolution (P2) — pure logic, no DB.

Covers the resolver against the reported shape (a Sub whose Lead and co-Sub are not yet
consented tenants) plus the boundary guarantees: parent link needs no consent,
co-applicants DO, self never resolves as its own co-applicant, the whitelist is the only
thing that crosses, and acronym name-matching works.

All names here are SYNTHETIC — fixtures never carry a real org identity.

Run:  python -m unittest tests.test_applicant_graph
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import applicant_graph as AG                       # noqa: E402

# --- a small synthetic tenant set -----------------------------------------------------
CHILD_ID, PARENT_ID, PRIME_ID, COSUB_ID = "t-child", "t-parent", "t-prime", "t-cosub"

PARENT_PROFILE = {
    "org_registered_countries": ["United States"],
    "org_authorized_signatory_donors": ["US Department of State"],
    "org_domain_ratings": {"IDs - Pandemic Response": 5},
    "org_min_target": 999,          # NOT whitelisted — must never cross
}
PRIME_PROFILE = {"org_registered_countries": ["United States"],
                 "org_funder_history": ["US Department of State"]}
COSUB_PROFILE = {"org_operating_countries": ["Nigeria"]}

# The child's own name is a de-identified placeholder; parent is a fictional multi-word
# org whose acronym (NHA) exercises acronym matching.
ROWS = [
    {"id": CHILD_ID, "name": "Sample Country Team", "slug": "sample-country-team",
     "status": "active", "parent_tenant_id": PARENT_ID,
     "share_for_consortium_scoring": False, "org_profile": {}, "org_identity": {}},
    {"id": PARENT_ID, "name": "Northwind Health Alliance Inc.", "slug": "northwind-inc",
     "status": "active", "parent_tenant_id": None,
     "share_for_consortium_scoring": False, "org_profile": PARENT_PROFILE, "org_identity": {}},
    {"id": PRIME_ID, "name": "Prime Lead Org", "slug": "prime-lead",
     "status": "active", "parent_tenant_id": None,
     "share_for_consortium_scoring": True, "org_profile": PRIME_PROFILE, "org_identity": {}},
    {"id": COSUB_ID, "name": "Co Sub Org", "slug": "co-sub",
     "status": "active", "parent_tenant_id": None,
     "share_for_consortium_scoring": True, "org_profile": COSUB_PROFILE, "org_identity": {}},
]

SELF = {"org_registered_countries": ["Kenya", "Uganda"]}


class ApplicantGraphTests(unittest.TestCase):
    def test_parent_link_needs_no_consent_and_projects_whitelist(self):
        # Parent has share=False yet is still inherited (ownership link).
        g = AG.build_graph({"applicant_role": "sub"}, SELF, CHILD_ID, ROWS)
        self.assertIsNotNone(g.parent)
        self.assertEqual(g.parent["org_registered_countries"], ["United States"])
        self.assertNotIn("org_min_target", g.parent)          # whitelist boundary holds

    def test_prime_resolves_only_when_consented(self):
        rfp = {"applicant_role": "sub", "lead_applicant": "Prime Lead Org",
               "sub_applicant": "Sample Country Team, Co Sub Org"}
        g = AG.build_graph(rfp, SELF, CHILD_ID, ROWS)
        self.assertIsNotNone(g.prime)
        self.assertEqual(g.prime["org_funder_history"], ["US Department of State"])
        self.assertFalse(g.unresolved_prime)
        # self (Sample Country Team) named in sub_applicant is excluded; only Co Sub remains.
        self.assertEqual(len(g.cosubs), 1)
        self.assertEqual(g.cosubs[0]["org_operating_countries"], ["Nigeria"])

    def test_unconsented_coapplicant_contributes_nothing(self):
        rows = [dict(r) for r in ROWS]
        for r in rows:
            if r["id"] == PRIME_ID:
                r["share_for_consortium_scoring"] = False    # withdraw consent
        rfp = {"applicant_role": "sub", "lead_applicant": "Prime Lead Org"}
        g = AG.build_graph(rfp, SELF, CHILD_ID, rows)
        self.assertIsNone(g.prime)
        self.assertTrue(g.unresolved_prime)                  # named, but not consented

    def test_unresolved_prime_when_lead_is_not_a_tenant(self):
        # The reported case: the Lead org is not a tenant at all.
        rfp = {"applicant_role": "sub", "lead_applicant": "Riverside Consortium",
               "sub_applicant": "Sample Country Team, Beacon Partners"}
        g = AG.build_graph(rfp, SELF, CHILD_ID, ROWS)
        self.assertIsNone(g.prime)
        self.assertTrue(g.unresolved_prime)
        self.assertEqual(g.cosubs, ())                       # Beacon Partners not a tenant

    def test_acronym_name_match(self):
        # A co-applicant typed as the acronym resolves to the full-name tenant.
        rows = [dict(r) for r in ROWS]
        for r in rows:
            if r["id"] == PARENT_ID:
                r["share_for_consortium_scoring"] = True     # consent so it can be a co-app
        rfp = {"applicant_role": "sub", "sub_applicant": "NHA"}
        g = AG.build_graph(rfp, SELF, CHILD_ID, rows)
        self.assertEqual(len(g.cosubs), 1)                   # "NHA" → Northwind Health Alliance

    def test_precedence_order_self_first(self):
        rfp = {"applicant_role": "sub", "lead_applicant": "Prime Lead Org"}
        g = AG.build_graph(rfp, SELF, CHILD_ID, ROWS)
        reg = g.for_registration()                           # self → parent → prime
        self.assertEqual(reg[0], SELF)
        self.assertEqual(reg[1], g.parent)
        self.assertEqual(reg[2], g.prime)
        self.assertEqual(g.for_competitiveness(), [SELF, g.parent])

    def test_no_parent_no_applicants_is_self_only(self):
        rows = [{"id": "solo", "name": "Solo", "slug": "solo", "status": "active",
                 "parent_tenant_id": None, "share_for_consortium_scoring": False,
                 "org_profile": {}, "org_identity": {}}]
        g = AG.build_graph({"applicant_role": "prime"}, SELF, "solo", rows)
        self.assertIsNone(g.parent)
        self.assertIsNone(g.prime)
        self.assertEqual(g.cosubs, ())
        self.assertFalse(g.unresolved_prime)
        self.assertEqual(g.for_registration(), [SELF])

    def test_inactive_tenant_is_not_consulted(self):
        rows = [dict(r) for r in ROWS]
        for r in rows:
            if r["id"] == PRIME_ID:
                r["status"] = "suspended"
        rfp = {"applicant_role": "sub", "lead_applicant": "Prime Lead Org"}
        g = AG.build_graph(rfp, SELF, CHILD_ID, rows)
        self.assertIsNone(g.prime)
        self.assertTrue(g.unresolved_prime)


if __name__ == "__main__":
    unittest.main()
