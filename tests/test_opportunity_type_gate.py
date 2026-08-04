"""Regression tests for the OPPORTUNITY-TYPE gate + detector (the screening leak).

Two clearly-ineligible calls reached a health-grant pipeline:
  * a UNOPS tender to BUY surgical microscopes (Spanish, Honduras);
  * an AfDB consultancy EOI on trade-based money laundering (COMESA).

Neither is a funding call a grant-seeking org can pursue. They got in because
`opportunity_type` was NULL on everything (nothing populated it except the EU F&T scanner)
and the only type check in is_eligible tested `== "announcement"`, so a NULL skipped the
gate entirely. `detect_opportunity_type` now classifies every candidate, the gate FAILS
CLOSED on the detected type when the column is empty, and each excluded class honours its
own policies['exclusions'] flag.

Run:  python -m unittest tests.test_opportunity_type_gate
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.type_detect import (                                    # noqa: E402
    detect_opportunity_type, OPPORTUNITY_TYPES, OPPORTUNITY_TYPE_EXCLUSIONS,
)
import core.auto_scorer as A                                      # noqa: E402

_POLICY = {"geographies": ["Cameroon", "Mali"], "themes": {}, "exclusions": {}}


_SIGNAL = {"call_submission_deadline": "2099-12-31", "call_award_value": 500000,
           "currency": "USD"}


def _gate(cand, policy=None):
    """Gate a candidate that ALREADY carries a valid call signal (deadline + award), so the
    earlier not-an-rfp gate can't fire first and mask the type verdict under test."""
    return A.is_eligible({**_SIGNAL, **cand}, policy or _POLICY, geo_org_gates=True,
                         theme_gate=False, llm_adjudicate=False)


class DetectorTests(unittest.TestCase):
    def test_procurement_host_wins_regardless_of_language(self):
        # The leaked row was Spanish, so no English label could have matched — the HOST is
        # the language-independent signal.
        self.assertEqual(detect_opportunity_type({
            "opportunity_title": "Adquisición de equipo médico hospitalario",
            "opportunity_link": "https://www.ungm.org/Public/Notice/309409"}), "Procurement")

    def test_non_english_tender_label(self):
        for title in ("Licitación pública para equipos", "Avis d'appel d'offres"):
            self.assertEqual(detect_opportunity_type({"opportunity_title": title}),
                             "Procurement", title)

    def test_consultancy_eoi(self):
        self.assertEqual(detect_opportunity_type({
            "opportunity_title": "EOI - COMESA Baseline Assessments on TBML",
            "brief_description": "The Bank invites request for expressions of interest for "
                                 "consulting services."}), "Consultancy")

    def test_genuine_funding_call(self):
        self.assertEqual(detect_opportunity_type({
            "opportunity_title": "Request for Proposals: Global Health",
            "brief_description": "grant funding"}), "Grant/funding call")

    def test_procurement_solicitation_family(self):
        self.assertEqual(detect_opportunity_type(
            {"opportunity_title": "x", "solicitation_type": "ITB"}), "Procurement")

    def test_unknown_returns_none(self):
        self.assertIsNone(detect_opportunity_type({"opportunity_title": "Newsletter"}))

    def test_vocabulary_covers_every_excluded_class(self):
        for cls in OPPORTUNITY_TYPE_EXCLUSIONS:
            self.assertIn(cls, OPPORTUNITY_TYPES, cls)


class GateTests(unittest.TestCase):
    def test_procurement_is_rejected(self):
        ok, why = _gate({
            "opportunity_title": "Adquisición de equipo médico hospitalario para el "
                                 "Hospital Escuela Universitario - GRUPO 2",
            "brief_description": "UNOPS has issued a solicitation to obtain quotations for "
                                 "surgical microscopes. Bidders must submit a fixed-price "
                                 "quote before the deadline.",
            "opportunity_link": "https://www.ungm.org/Public/Notice/1",
            "call_geographic_scope": ["Cameroon"]})
        self.assertFalse(ok)
        self.assertIn("procurement", why.lower())

    def test_consultancy_is_rejected(self):
        ok, why = _gate({
            "opportunity_title": "EOI - Multinational - COMESA Baseline Assessments on "
                                 "Trade Based Money Laundering - TBML",
            "brief_description": "The African Development Bank invites request for "
                                 "expressions of interest for consulting services to carry "
                                 "out a baseline assessment.",
            "opportunity_link": "https://afdb.org/en/documents/eoi-comesa",
            "call_geographic_scope": ["Cameroon"]})
        self.assertFalse(ok)
        self.assertIn("consultancy", why.lower())

    def test_null_type_fails_closed_on_the_detected_type(self):
        # The actual leak: opportunity_type NULL used to SKIP the gate.
        ok, _ = _gate({"opportunity_type": None,
                       "opportunity_title": "Invitation to bid for laboratory equipment",
                       "call_geographic_scope": ["Cameroon"]})
        self.assertFalse(ok)

    def test_genuine_grant_call_still_passes(self):
        ok, why = _gate({"opportunity_title": "Request for Proposals: health systems",
                         "brief_description": "Grant funding for health systems.",
                         "opportunity_link": "https://donor.org/rfp",
                         "call_geographic_scope": ["Cameroon"]})
        self.assertTrue(ok, why)

    def test_a_tenant_can_re_admit_a_class(self):
        pol = {**_POLICY, "exclusions": {"reject_consultancies": False}}
        ok, _ = _gate({
            "opportunity_title": "EOI - Multinational - COMESA Baseline Assessments on "
                                 "Trade Based Money Laundering - TBML",
            "brief_description": "The African Development Bank invites request for "
                                 "expressions of interest for consulting services to carry "
                                 "out a baseline assessment.",
            "opportunity_link": "https://afdb.org/en/documents/eoi-comesa",
            "call_geographic_scope": ["Cameroon"]}, pol)
        self.assertTrue(ok)


class RegionalBlocTests(unittest.TestCase):
    def test_comesa_does_not_contain_cameroon(self):
        from core.geographies import expand
        self.assertNotIn("cameroon", {c.lower() for c in expand(["COMESA"])})

    def test_cemac_does_contain_cameroon(self):
        from core.geographies import expand
        self.assertIn("cameroon", {c.lower() for c in expand(["CEMAC"])})


class ThemeSelfConfirmationTests(unittest.TestCase):
    def test_gate_text_excludes_the_apps_own_theme_label(self):
        # focus_theme is minted by auto_score itself; gating on it let the system confirm
        # its own guess (a procurement tender labelled "Cross-cutting (Health)" then
        # satisfied a health theme gate on the word inside that label).
        txt = A._full_text({"opportunity_title": "t", "brief_description": "d",
                            "focus_theme": "Cross-cutting (Health)"})
        self.assertNotIn("cross-cutting", txt)

    def test_generic_commerce_words_are_not_a_health_area(self):
        from core.program_area_classifier import classify_program_areas
        areas = classify_program_areas(
            "Procurement of consulting services, supply chain and market access.")
        self.assertNotIn("Cross-cutting - Market Shaping", areas)

    def test_real_health_market_shaping_still_classifies(self):
        from core.program_area_classifier import classify_program_areas
        self.assertIn("Cross-cutting - Market Shaping", classify_program_areas(
            "Pooled procurement and price negotiation for essential medicines access."))


if __name__ == "__main__":
    unittest.main(verbosity=2)
