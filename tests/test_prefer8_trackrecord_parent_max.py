"""PREFER-8 track record is parent-transferable, parent-MAX (P5).

A child that hasn't rated a program area yet still shows the PARENT org's demonstrated
record there. The org's rating per area is the MAX across self + parent; the band is then
judged against the donor's priority as before. Self-only when no graph (byte-for-byte).

Models the reported case: the call's area is "Pandemic Response", the child has NOT rated
it (→ Low on its own), the parent org has (→ High once inherited).

Run:  python -m unittest tests.test_prefer8_trackrecord_parent_max
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
RFP = {"applicant_role": "sub", "call_domain_areas": ["Pandemic Response"]}
DONOR: dict = {}                                            # no priority for the area → default 5

# Child has a record in another area, but NOT the call's ("IDs - Pandemic Response").
SELF = {"org_domain_expertise": ["IDs - HIV/AIDS"],
        "org_domain_ratings": {"IDs - HIV/AIDS": 4}}
PARENT_HAS_PR = {"org_domain_expertise": ["IDs - Pandemic Response"],
                 "org_domain_ratings": {"IDs - Pandemic Response": 5}}


def _rows(parent_profile):
    return [
        {"id": CHILD_ID, "name": "Child Team", "slug": "child", "status": "active",
         "parent_tenant_id": "t-parent", "share_for_consortium_scoring": False,
         "org_profile": {}, "org_identity": {}},
        {"id": "t-parent", "name": "Parent Org", "slug": "parent", "status": "active",
         "parent_tenant_id": None, "share_for_consortium_scoring": False,
         "org_profile": parent_profile, "org_identity": {}},
    ]


def _track(items):
    return next(x for x in items if x["key"] == "comp_track")


class TrackRecordParentMaxTests(unittest.TestCase):
    def test_graph_none_is_self_only_low(self):
        # Child alone has no Pandemic-Response rating → band Low (0.0).
        band = CD._track_record_band(SELF, RFP, DONOR)
        self.assertIsNotNone(band)
        self.assertEqual(band[0], 0.0)
        self.assertEqual(_track(CD._competitiveness_factors(SELF, RFP, DONOR, {}))["score"], 0.0)

    def test_parent_rating_is_inherited_high(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows(PARENT_HAS_PR))
        band = CD._track_record_band(SELF, RFP, DONOR, graph=g)
        self.assertEqual(band[0], 1.0)                      # parent's 5 vs donor 5 → High
        self.assertEqual(band[1], 5.0)                      # org_rating = parent-MAX
        self.assertEqual(
            _track(CD._competitiveness_factors(SELF, RFP, DONOR, {}, graph=g))["score"], 1.0)

    def test_parent_without_the_area_leaves_it_low(self):
        g = AG.build_graph(RFP, SELF, CHILD_ID, _rows({"org_domain_ratings": {"IDs - HIV/AIDS": 5}}))
        band = CD._track_record_band(SELF, RFP, DONOR, graph=g)
        self.assertEqual(band[0], 0.0)                      # neither holds the area → honest Low

    def test_max_not_replace(self):
        # Child rated the area 2, parent rated it 5 → MAX = 5 (child's own not lowered).
        self_rated = {"org_domain_expertise": ["IDs - Pandemic Response"],
                      "org_domain_ratings": {"IDs - Pandemic Response": 2}}
        g = AG.build_graph(RFP, self_rated, CHILD_ID, _rows(PARENT_HAS_PR))
        self.assertEqual(CD._track_record_band(self_rated, RFP, DONOR, graph=g)[1], 5.0)


if __name__ == "__main__":
    unittest.main()
