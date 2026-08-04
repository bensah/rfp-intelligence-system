"""Tests for the FEATURED rail — the safety net for screening misses.

The existing three rail cards rank the tenant's OWN pipeline (rfp_submissions), so they can
never surface a call that screening dropped or never reached. `featured()` ranks the SHARED
catalog (extracted_solicitations) against the tenant's stated preferences AND their actual
behaviour (funders they engage, programme areas they pursue), so a miss stays discoverable.

It is a PREFERENCE ranking, not a second gate — but it must not re-introduce the noise the
gate exists to remove, so opportunity types this org can never pursue stay out.

Run:  python -m unittest tests.test_featured_feed
"""
import os
import sys
import unittest
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import opportunity_feed as F        # noqa: E402

TODAY = date(2026, 8, 4)
SOON = (TODAY + timedelta(days=10)).isoformat()
LATER = (TODAY + timedelta(days=200)).isoformat()
PAST = (TODAY - timedelta(days=5)).isoformat()

PREFS = {
    "countries": ["Cameroon", "Mali"],
    "broad_terms": ["Sub-Saharan Africa"],
    "themes": ["Malaria & NTDs", "Health Financing"],
    "pursued_areas": ["Malaria & NTDs"],
    "known_funders": ["Wellcome Trust", "Gates Foundation"],
}


def _cat(uid, **over):
    row = {"uid": uid, "opportunity_name": "Call " + uid,
           "opportunity_url": "https://donor.org/" + uid,
           "funder_name": "Some Funder", "deadline": LATER,
           "grant_amount": 1_000_000, "currency": "USD",
           "call_geographic_scope": ["Global / worldwide"],
           "call_domain_areas": ["Other"], "brief_description": "A grant call."}
    row.update(over)
    return row


class RankingTests(unittest.TestCase):
    def test_geography_and_theme_fit_outrank_a_generic_call(self):
        rows = [
            _cat("generic"),
            _cat("fit", call_geographic_scope=["Cameroon"],
                 call_domain_areas=["Malaria & NTDs"]),
        ]
        out = F.featured(rows, PREFS, today=TODAY)
        self.assertEqual(out[0]["uid"], "fit")

    def test_a_funder_they_already_work_with_is_boosted(self):
        rows = [_cat("cold"), _cat("warm", funder_name="Wellcome Trust")]
        out = F.featured(rows, PREFS, today=TODAY)
        self.assertEqual(out[0]["uid"], "warm")
        self.assertIn("funder you already work with", out[0]["_why"])

    def test_every_item_explains_itself(self):
        for it in F.featured([_cat("a"), _cat("b")], PREFS, today=TODAY):
            self.assertTrue(it["_why"].strip(), "a featured item must say why")

    def test_closing_soon_is_surfaced_in_the_reason(self):
        out = F.featured([_cat("urgent", deadline=SOON,
                               call_geographic_scope=["Cameroon"])], PREFS, today=TODAY)
        self.assertIn("closing soon", out[0]["_why"])


class ExclusionTests(unittest.TestCase):
    def test_expired_calls_are_dropped(self):
        self.assertEqual(F.featured([_cat("old", deadline=PAST)], PREFS, today=TODAY), [])

    def test_rows_already_in_the_pipeline_are_dropped(self):
        rows = [_cat("dup", opportunity_url="https://donor.org/dup")]
        out = F.featured(rows, PREFS, seen_keys={"https://donor.org/dup"}, today=TODAY)
        self.assertEqual(out, [])

    def test_procurement_is_never_featured(self):
        # Featuring a tender the gate correctly rejected would reintroduce the exact noise
        # the gate exists to remove.
        rows = [_cat("tender", opportunity_url="https://www.ungm.org/Public/Notice/9",
                     opportunity_name="Adquisición de equipo médico")]
        self.assertEqual(F.featured(rows, PREFS, today=TODAY), [])

    def test_consultancy_is_never_featured(self):
        rows = [_cat("eoi", opportunity_name="Request for expressions of interest",
                     brief_description="consulting services required")]
        self.assertEqual(F.featured(rows, PREFS, today=TODAY), [])

    def test_limit_is_respected(self):
        rows = [_cat("r%d" % i) for i in range(20)]
        self.assertEqual(len(F.featured(rows, PREFS, today=TODAY, limit=3)), 3)


class ShapeTests(unittest.TestCase):
    def test_item_shape_matches_the_rail_contract(self):
        it = F.featured([_cat("x")], PREFS, today=TODAY)[0]
        for k in ("uid", "title", "funder", "amount", "currency", "deadline",
                  "days_until", "geo", "link", "_score", "_why"):
            self.assertIn(k, it, k)

    def test_catalog_fields_are_mapped(self):
        it = F.featured([_cat("x", opportunity_name="Malaria call",
                              funder_name="ACME", grant_amount=250000)],
                        PREFS, today=TODAY)[0]
        self.assertEqual(it["title"], "Malaria call")
        self.assertEqual(it["funder"], "ACME")
        self.assertEqual(it["amount"], 250000)

    def test_no_preferences_still_returns_something(self):
        # A brand-new tenant with nothing configured must still see discovery content.
        out = F.featured([_cat("a"), _cat("b")], {}, today=TODAY)
        self.assertTrue(out)

    def test_empty_catalog_is_safe(self):
        self.assertEqual(F.featured([], PREFS, today=TODAY), [])
        self.assertEqual(F.featured(None, PREFS, today=TODAY), [])



class DiversityTests(unittest.TestCase):
    """One prolific funder must not take every slot — a card showing the same funder five
    times has stopped being discovery."""

    def test_at_most_two_per_funder_when_alternatives_exist(self):
        rows = [_cat("big%d" % i, funder_name="Mega Programme") for i in range(6)]
        rows += [_cat("alt%d" % i, funder_name="Funder %d" % i) for i in range(4)]
        out = F.featured(rows, PREFS, today=TODAY)
        from collections import Counter
        top = Counter(i["funder"] for i in out)
        self.assertLessEqual(top["Mega Programme"], 2)
        self.assertGreaterEqual(len({i["funder"] for i in out}), 3)

    def test_cap_relaxes_rather_than_returning_a_short_card(self):
        # If a single funder is ALL there is, better to fill the card than show one item.
        rows = [_cat("only%d" % i, funder_name="Sole Funder") for i in range(5)]
        self.assertEqual(len(F.featured(rows, PREFS, today=TODAY)), 5)

if __name__ == "__main__":
    unittest.main(verbosity=2)
