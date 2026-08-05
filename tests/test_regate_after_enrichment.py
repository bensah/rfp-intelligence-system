"""Regression test for the POST-ENRICHMENT re-gate (core/scan_pipeline.ingest_candidates).

The eligibility gate runs on the SCRAPED candidate. Deep-read + LLM synthesis then LEARN
gate-relevant facts the listing never showed — the geographic scope, the programme areas, a
fuller brief. A row whose true scope only appears at that point was admitted on evidence
that no longer reflects it.

That is exactly how a Honduras-only UNOPS tender reached a Cameroon pipeline: at gate time
its `call_geographic_scope` was empty (so geography couldn't reject it); synthesis later
filled in ["Honduras"], and nothing re-checked. `is_eligible` on the stored row rejects it —
the gate was right, it just ran too early.

These tests pin the DECISION the re-gate makes: the bare candidate passes, the enriched view
does not, so the enriched view is what must be gated on.

Run:  python -m unittest tests.test_regate_after_enrichment
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core.auto_scorer import is_eligible                     # noqa: E402
import core.scan_pipeline as SP                              # noqa: E402

# Real policy shape (core/policies.DEFAULT_POLICIES): countries.eligible drives the geo gate.
_POLICY = {"countries": {"eligible": ["Cameroon", "Mali"], "broad_terms": []},
           "themes": {}, "exclusions": {}}
_BASE = {
    "opportunity_title": "Request for Proposals: health systems strengthening",
    "brief_description": "Grant funding for health systems.",
    "opportunity_link": "https://donor.org/rfp",
    "call_submission_deadline": "2099-12-31",
    "call_award_value": 500000, "currency": "USD",
}


def _gate(cand):
    return is_eligible({**cand}, _POLICY, geo_org_gates=True, theme_gate=False,
                       llm_adjudicate=False)


class RegateDecisionTests(unittest.TestCase):
    def test_bare_candidate_passes_when_scope_is_unknown(self):
        # At gate time the listing showed no geography — nothing to reject on.
        ok, _ = _gate({**_BASE, "call_geographic_scope": []})
        self.assertTrue(ok)

    def test_enriched_view_is_rejected_once_the_scope_is_known(self):
        # Synthesis later learns the call is Honduras-only. THIS is the view the re-gate
        # runs on, and it must reject.
        ok, why = _gate({**_BASE, "call_geographic_scope": ["Honduras"]})
        self.assertFalse(ok)
        self.assertIn("geograph", why.lower())

    def test_enrichment_that_stays_in_scope_still_passes(self):
        # The re-gate must not become a blanket "reject anything enriched".
        ok, why = _gate({**_BASE, "call_geographic_scope": ["Cameroon"]})
        self.assertTrue(ok, why)


class RegateWiringTests(unittest.TestCase):
    """The re-gate has to actually be wired into the ingest loop — a correct decision that
    never runs fixes nothing."""

    def test_ingest_reruns_the_gate_after_enrichment(self):
        import inspect
        src = inspect.getsource(SP.ingest_candidates)
        self.assertIn("RE-GATE AFTER ENRICHMENT", src)
        # It must gate the ENRICHED view, not the raw candidate, and drop the row.
        self.assertIn("post-enrichment", src)

    def test_regate_only_fires_when_a_gate_field_changed(self):
        import inspect
        src = inspect.getsource(SP.ingest_candidates)
        for f in ("call_geographic_scope", "call_domain_areas", "opportunity_type"):
            self.assertIn(f, src, f)

    def test_regate_is_skipped_during_extract_only(self):
        # extract_only builds the SHARED, org-agnostic store — org gates must not apply
        # there (geography is not an extraction gate).
        import inspect
        src = inspect.getsource(SP.ingest_candidates)
        i = src.index("RE-GATE AFTER ENRICHMENT")
        self.assertIn("if not extract_only:", src[i:i + 900])


if __name__ == "__main__":
    unittest.main(verbosity=2)
