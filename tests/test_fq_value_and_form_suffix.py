"""Two false negatives: a missing award value, and a funder named with its legal form.

1. PREFER-6's "Award value stated by the call" read ✗ NOT MET whenever the value was
   blank — a measured verdict on our own extraction gap. The call that exposed it publishes
   its budget plainly on the funder's page; the ✗ was reporting OUR miss as the funder's
   silence. Its two siblings in the same function already return None for a missing value.

2. MUST-1 read "Not sure" on a funder whose donor record EXISTS, because the strict matcher
   requires an exact key hit and the funder string carried a legal form the donor record
   omits ("Global Health EDCTP3 Joint Undertaking" vs the record "Global Health EDCTP3").
   Stripping a trailing legal FORM and retrying an EXACT match fixes it without going
   fuzzy — which matters, because fuzzy containment on this table matches seven different
   UN agencies to a single unrelated consortium.
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

from core import criteria_derive as CD                      # noqa: E402
from core import donor_intel as DI                          # noqa: E402

ORG = {"org_min_target": 100000, "org_max_target": 5000000}


def _fq(rfp, org=ORG):
    return {f["key"]: f for f in CD._funding_quality_factors(rfp, org)}


class AMissingAwardValueIsNotAFailureTests(unittest.TestCase):
    def test_a_blank_value_reads_not_sure(self):
        it = _fq({})["fq_value"]
        self.assertFalse(it["active"])
        self.assertIsNone(it["met"])
        self.assertEqual(CD.component_mark(it)[0], "?")

    def test_it_no_longer_reads_as_a_measured_failure(self):
        self.assertNotEqual(CD.component_mark(_fq({})["fq_value"])[0], "✗")

    def test_a_stated_value_still_reads_met(self):
        it = _fq({"call_award_value": 250000, "currency": "USD"})["fq_value"]
        self.assertTrue(it["active"])
        self.assertTrue(it["met"])
        self.assertEqual(CD.component_mark(it)[0], "✓")

    def test_it_matches_how_its_siblings_treat_a_missing_value(self):
        # fq_floor / fq_ceiling already returned None for a blank value; fq_value was the
        # odd one out in its own function.
        fq = _fq({})
        for k in ("fq_floor", "fq_ceiling", "fq_value"):
            with self.subTest(component=k):
                self.assertIsNone(fq[k]["met"], k)

    def test_an_extraction_gap_cannot_cost_prefer6_a_point(self):
        from core import criteria_review as CR
        items = list(_fq({}).values())
        self.assertNotIn("fq_value", [i["key"] for i in CR.active_components(items)])

    def test_a_zero_value_is_still_not_a_stated_value(self):
        for raw in (0, "0", "", None):
            with self.subTest(raw=raw):
                self.assertIsNone(_fq({"call_award_value": raw})["fq_value"]["met"])


class StrippingALegalFormTests(unittest.TestCase):
    def test_a_trailing_joint_undertaking_is_stripped(self):
        self.assertEqual(DI._strip_form_suffix("global health edctp3 joint undertaking"),
                         "global health edctp3")

    def test_company_forms_are_stripped(self):
        for suf in ("ltd", "limited", "inc", "plc", "gmbh", "llc", "asbl"):
            with self.subTest(suffix=suf):
                self.assertEqual(DI._strip_form_suffix(f"acme health {suf}"), "acme health")

    def test_a_leading_article_is_stripped(self):
        self.assertEqual(DI._strip_form_suffix("the union"), "union")

    def test_names_that_DISTINGUISH_an_entity_are_never_stripped(self):
        # Pfizer is not the Pfizer Foundation; Wellcome is not the Wellcome Trust. Merging
        # them would score an RFP against the wrong funder's requirements.
        for name in ("pfizer foundation", "wellcome trust", "novo nordisk fund",
                     "some institute", "some association"):
            with self.subTest(name=name):
                self.assertEqual(DI._strip_form_suffix(name), "")

    def test_nothing_to_strip_returns_empty(self):
        for name in ("", "gates foundation x", "unitaid"):
            with self.subTest(name=name):
                self.assertEqual(DI._strip_form_suffix(name), "")

    def test_only_one_suffix_is_removed(self):
        # Two stacked forms is not a real name; one pass keeps the rule predictable.
        self.assertEqual(DI._strip_form_suffix("acme ltd inc"), "acme ltd")

    def test_a_suffix_alone_is_not_treated_as_a_name(self):
        # " ltd" needs something before it — the check requires a space-delimited tail.
        self.assertEqual(DI._strip_form_suffix("ltd"), "")


class TheMatcherStaysStrictTests(unittest.TestCase):
    """The suffix strip must remain EXACT matching. Fuzzy containment on the live table
    matches seven UN agencies to one unrelated consortium, and "Other" to a real donor —
    which is why the scoring path passes fuzzy=False."""

    def test_the_candidate_list_gains_the_stripped_form(self):
        # No DB needed: the candidate construction is what we assert.
        raw = "Global Health EDCTP3 Joint Undertaking"
        cands = [DI._norm(raw)]
        cands += [DI._strip_form_suffix(c) for c in list(cands)]
        self.assertIn("global health edctp3", cands)

    def test_it_composes_with_the_acronym_split(self):
        raw = "EDCTP3 - Global Health EDCTP3 Joint Undertaking"
        acr, name = DI.split_funder_prefix(raw)
        self.assertEqual(DI._strip_form_suffix(DI._norm(name)), "global health edctp3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
