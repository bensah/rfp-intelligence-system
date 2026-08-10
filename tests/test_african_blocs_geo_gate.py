"""A call scoped to an African economic bloc must be tested against its MEMBERS.

THE LEAK. A regional tender scoped `["COMESA"]` reached a West/Central-African tenant's
pipeline. The membership lists were already correct — a previous fix added them to
`_REGION_MEMBERS`, with a comment naming this very case — but the bloc names were never
registered as DETECTABLE geographies (`REGION_TERMS` / `BROAD_GEOGRAPHIES`). So:

    broad_geos_in_text("comesa region")  ->  set()

The gate saw a call with NO stated geography, which it treats as global and admits. The
membership test it would have used never ran, because nothing told it a geography was
there. Half the fix had landed.

Registering the blocs is all that was missing: detection finds the label, and the existing
membership test answers the real question. COMESA is Eastern/Southern Africa and contains
neither Cameroon nor Mali, so a COMESA-only call is genuinely out of scope — while ECOWAS
(Mali) and CEMAC/ECCAS (Cameroon) must keep passing.

Membership is FACT, not preference, so it is asserted directly: getting a member list wrong
would silently reject in-scope calls, which is the more damaging direction.
"""
from __future__ import annotations

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import auto_scorer as A                            # noqa: E402
from core import geographies as G                            # noqa: E402

BLOCS = ("COMESA", "ECOWAS", "CEMAC", "ECCAS", "EAC", "SADC",
         "UEMOA", "WAEMU", "IGAD", "AMU")

# A West + Central African tenant, the shape that was leaked into.
POLICIES = {"countries": {"eligible": ["Cameroon", "Mali"]},
            "themes": {"required_any": ["health"]}}


def _call(scope):
    return {"opportunity_title": "Health systems strengthening call",
            "brief_description": "Grants for primary health care delivery.",
            "opportunity_type": "Grant/funding call",
            "call_geographic_scope": [scope] if isinstance(scope, str) else list(scope)}


def _members(bloc):
    return {c.lower() for c in G._REGION_MEMBERS.get(bloc.lower(), [])}


class TheBlocsAreDetectableTests(unittest.TestCase):
    """The half of the fix that was missing."""

    def test_every_bloc_is_a_recognised_region_term(self):
        for b in BLOCS:
            with self.subTest(bloc=b):
                self.assertIn(b, G.REGION_TERMS, b)

    def test_every_bloc_is_a_broad_geography(self):
        for b in BLOCS:
            with self.subTest(bloc=b):
                self.assertIn(b, G.BROAD_GEOGRAPHIES, b)

    def test_a_bare_acronym_in_prose_is_detected(self):
        self.assertEqual(G.broad_geos_in_text("comesa region"), {"COMESA"})

    def test_a_spelled_out_bloc_is_detected(self):
        self.assertIn("SADC",
                      G.broad_geos_in_text("the southern african development community"))
        self.assertIn("ECOWAS",
                      G.broad_geos_in_text("economic community of west african states"))

    def test_every_bloc_still_has_its_member_list(self):
        for b in BLOCS:
            with self.subTest(bloc=b):
                self.assertTrue(_members(b), b)


class MembershipIsFactTests(unittest.TestCase):
    """A wrong member list would silently reject IN-SCOPE calls — the worse direction."""

    def test_comesa_is_eastern_southern_africa(self):
        m = _members("COMESA")
        for inside in ("kenya", "uganda", "zambia", "zimbabwe", "egypt", "ethiopia"):
            self.assertIn(inside, m, inside)
        for outside in ("cameroon", "mali", "nigeria", "senegal"):
            self.assertNotIn(outside, m, outside)

    def test_ecowas_is_west_africa_and_contains_mali(self):
        m = _members("ECOWAS")
        self.assertIn("mali", m)
        self.assertIn("nigeria", m)
        self.assertNotIn("cameroon", m)

    def test_cemac_and_eccas_contain_cameroon(self):
        self.assertIn("cameroon", _members("CEMAC"))
        self.assertIn("cameroon", _members("ECCAS"))

    def test_uemoa_and_waemu_are_the_same_bloc(self):
        self.assertEqual(_members("UEMOA"), _members("WAEMU"))
        self.assertIn("mali", _members("UEMOA"))

    def test_amu_is_the_maghreb(self):
        m = _members("AMU")
        self.assertIn("morocco", m)
        self.assertIn("tunisia", m)
        self.assertNotIn("cameroon", m)


class TheGateNowJudgesTheScopeTests(unittest.TestCase):
    def test_an_out_of_scope_bloc_is_rejected(self):
        for b in ("COMESA", "SADC", "EAC", "IGAD", "AMU"):
            with self.subTest(bloc=b):
                reject, reason = A.geographic_exclusion_reject(_call(b), POLICIES)
                self.assertTrue(reject, b)
                self.assertIn(b, reason)

    def test_an_in_scope_bloc_still_passes(self):
        # The safety direction: these DO contain the tenant's countries.
        for b in ("ECOWAS", "UEMOA", "CEMAC", "ECCAS"):
            with self.subTest(bloc=b):
                reject, _ = A.geographic_exclusion_reject(_call(b), POLICIES)
                self.assertFalse(reject, b)

    def test_a_continent_or_tier_scope_still_passes(self):
        for scope in ("Africa", "Sub-Saharan Africa"):
            with self.subTest(scope=scope):
                self.assertFalse(A.geographic_exclusion_reject(_call(scope), POLICIES)[0])

    def test_a_bloc_plus_an_in_scope_country_passes(self):
        # A multi-scope call that names the tenant's own country is in scope.
        self.assertFalse(
            A.geographic_exclusion_reject(_call(["COMESA", "Cameroon"]), POLICIES)[0])

    def test_geography_is_still_not_an_extraction_gate(self):
        # DATA_SCHEMA_ETL.md §3: the global store is a SUPERSET — an out-of-scope call is
        # still a real call and must reach the catalogue. Only tenant screening drops it.
        c = _call("COMESA")
        c["call_submission_deadline"] = "2099-01-01"
        self.assertTrue(A.is_eligible(c, POLICIES, geo_org_gates=False,
                                      theme_gate=False)[0])
        self.assertFalse(A.is_eligible(c, POLICIES)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
