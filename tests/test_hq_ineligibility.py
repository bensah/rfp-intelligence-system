"""Regression tests for the MUST-1 HQ-geography ineligibility gate (criteria_derive).

A funding call can require the applicant/lead institution to be HEADQUARTERED in a specific
country OR REGION (e.g. IDRC/ANeSA: "Organizations headquartered outside sub-Saharan Africa
are not eligible"). MUST-1 item C must:
  * DISQUALIFY an org whose HQ is outside the required region (CHAI Cameroon, HQ = United
    States, applying to a Sub-Saharan-Africa-HQ call);
  * ADMIT an org genuinely HQ'd inside the region (HQ = Cameroon / Nigeria) — the region
    requirement must be expanded to member countries, not matched as a literal string;
  * keep exact-country requirements working;
  * stay inactive when the call states no HQ restriction.

Item C is HARD/fatal, so score 0 auto-Declines. HQ is read from org_settings.org_hq_country
(the headquarters), distinct from the operating country.

Run:  python -m unittest tests.test_hq_ineligibility
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.criteria_derive import qualification_factors    # noqa: E402


def _hq(org=None, donor=None, org_settings=None):
    items = qualification_factors(org or {}, {}, donor or {}, org_settings or {})
    return next(i for i in items if i["key"] == "donor_hq_country")


_SSA_REQ = {"donor_hq_country_required": ["Sub-Saharan Africa"]}


class HqIneligibilityTests(unittest.TestCase):
    def test_hq_outside_region_is_declined(self):
        # CHAI Cameroon: HQ = United States → excluded from a Sub-Saharan-Africa-HQ call.
        it = _hq(donor=_SSA_REQ, org_settings={"org_hq_country": "United States"})
        self.assertTrue(it["active"])
        self.assertEqual(it["score"], 0.0)
        self.assertIs(it["met"], False)   # hard/fatal → auto-Decline

    def test_hq_inside_region_passes(self):
        for hq in ("Cameroon", "Nigeria", "Kenya"):
            it = _hq(donor=_SSA_REQ, org_settings={"org_hq_country": hq})
            self.assertEqual(it["score"], 1.0, hq)   # region expanded to members → pass
            self.assertIs(it["met"], True, hq)

    def test_region_synonym_expands(self):
        it = _hq(donor={"donor_hq_country_required": ["SSA"]},
                 org_settings={"org_hq_country": "Cameroon"})
        self.assertEqual(it["score"], 1.0)   # 'SSA' canonicalises to Sub-Saharan Africa

    def test_exact_country_requirement_still_works(self):
        ok = _hq(donor={"donor_hq_country_required": ["United States"]},
                 org_settings={"org_hq_country": "United States"})
        self.assertIs(ok["met"], True)
        bad = _hq(donor={"donor_hq_country_required": ["Kenya"]},
                  org_settings={"org_hq_country": "United States"})
        self.assertIs(bad["met"], False)

    def test_no_requirement_is_inactive(self):
        it = _hq(donor={}, org_settings={"org_hq_country": "United States"})
        self.assertFalse(it["active"])   # call states no HQ rule → gate doesn't apply

    def test_any_requirement_is_inactive(self):
        it = _hq(donor={"donor_hq_country_required": ["Any"]},
                 org_settings={"org_hq_country": "United States"})
        self.assertFalse(it["active"])   # 'Any' = no real restriction

    def test_hq_falls_back_to_operating_country_when_hq_blank(self):
        # Documents the DATA requirement: a blank org_hq_country falls back to org_country,
        # so an org that OPERATES in-region but is HQ'd elsewhere would wrongly pass unless
        # org_hq_country is set explicitly.
        it = _hq(donor=_SSA_REQ, org_settings={"org_hq_country": "", "org_country": "Cameroon"})
        self.assertIs(it["met"], True)   # fallback to operating Cameroon → passes (by design)


if __name__ == "__main__":
    unittest.main(verbosity=2)
