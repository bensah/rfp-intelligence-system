"""The theme gate must be able to see the page, and its arbiter must actually run.

THE REPORTED CASE. The UBS Optimus / Outcomes Accelerator "Cohort 5" call for proposals —
outcomes-based financing for mental-health outcomes in low- and middle-income countries —
was never surfaced. It was found through a LinkedIn share.

Discovery was one half (the funder never published it on their own domain; it lives on a
programme microsite, the same shape as Fondation Pierre Fabre -> odess.io). But registering
the source would NOT have been enough, and that is what this file pins.

WHY THE GATE REJECTED A MENTAL-HEALTH CALL ON THEME. `_full_text` — everything the theme
gate reads — is title + brief_description + geographic scope + funder. It does not include
`_page_text`. That narrowness is deliberate and correct in one direction: the required list
contains bare words like "health", and a 20k-char procurement page that says "health and
safety" once is not a health call. But it also means the gate never reads the page. On this
call the extracted brief (594 chars) happened to capture the eligibility paragraph — "UBS
Optimus Foundation", "Who can apply" — and never once said health, so `_full_text` came to
652 chars with no required hit while the page said "mental health" 34 times.

Measured on the live store: 56 of 544 Open rows have no required hit in the narrow fields
and one in raw_text. They are a MIX — mostly Horizon Europe calls (batteries, photonics,
textile circularity) that mention health incidentally, which is exactly what the narrow
text was protecting against, plus real ones like an immunisation-supplies notice ("au
profit du PEV"). Regex cannot separate those, which is why the rescue routes to the judge
rather than widening the pattern.

AND THE ARBITER WAS NEVER RUNNING. `theme_eligible(..., llm_theme=True)` had a parameter, a
working code path, and no caller anywhere in the repository. The theme gate has been
regex-only over a ~650-char blob since it was written. It is now wired to the existing
`llm_adjudicate` opt-in that run_screening already passes.

Spot-checked against the live judge, which got all four right: the Outcomes Accelerator
call and the PEV notice on-theme; battery storage and textile circularity off-theme.

Run:  python -m unittest tests.test_theme_reads_the_page
"""
import io
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import auto_scorer as A                                     # noqa: E402

_POL = {"themes": {"required_any": ["health", "global health"],
                   "excluded_any": ["clinical trial"]}}

# The real shape: a brief that misses the keyword, a page full of it.
_BRIEF = ("The Fifth Cohort is now open and accepting applications. See below for "
          "details! Eligibility: requirements of the UBS Optimus Foundation and the "
          "donors to the Outcomes Accelerator if selected. Who can apply.")
_PAGE = ("Outcomes Accelerator Cohort 5. The Outcomes Accelerator seeks innovative "
         "proposals that apply outcomes-based financing to improve mental health "
         "outcomes, incorporating early intervention, community-based delivery and "
         "systems integration at scale. " * 6)


def _cand(**kw):
    c = {"opportunity_title": "Pipeline Acceleration Opportunities",
         "brief_description": _BRIEF, "_page_text": _PAGE, "notes": "",
         "funding_agency": "UBS Optimus Foundation", "call_geographic_scope": [],
         "call_domain_areas": [], "call_submission_deadline": "2026-10-02"}
    c.update(kw)
    return c


class TheNarrowBlobIsWhyItFailedTests(unittest.TestCase):
    def test_full_text_does_not_include_the_page(self):
        # Pinning the CAUSE, not endorsing it: the rescue below exists because of this.
        blob = A._full_text(_cand())
        self.assertNotIn("mental health", blob)
        self.assertIn("mental health", _PAGE.lower())

    def test_regex_only_still_rejects(self):
        # Fails to the OLD behaviour when no arbiter is available — nothing is widened
        # without something that can tell incidental from real.
        ok, why = A.theme_eligible(_cand(), _POL, llm_theme=False)
        self.assertFalse(ok)
        self.assertIn("no required theme keyword", why)


class ThePageTextRescueTests(unittest.TestCase):
    def _judge(self, relevant):
        j = mock.MagicMock()
        j.is_enabled.return_value = True
        j.judge.return_value = {"theme_relevant": relevant}
        return j

    def test_a_page_hit_with_no_brief_hit_reaches_the_judge(self):
        j = self._judge(True)
        with mock.patch.dict(sys.modules, {"core.llm_judge": j}):
            ok, why = A.theme_eligible(_cand(), _POL, llm_theme=True)
        self.assertTrue(ok)
        self.assertIn("LLM", why)
        self.assertTrue(j.judge.called, "the judge must be consulted, not bypassed")

    def test_the_judge_can_still_rule_it_incidental(self):
        # The battery / textile / photonics case: 'health and safety' on a 20k page.
        j = self._judge(False)
        with mock.patch.dict(sys.modules, {"core.llm_judge": j}):
            ok, why = A.theme_eligible(_cand(), _POL, llm_theme=True)
        self.assertFalse(ok)
        self.assertIn("incidental", why)

    def test_a_page_with_nothing_on_theme_never_reaches_the_judge(self):
        # 270 of 544 live rows are in this state; spending a call on them is waste.
        j = self._judge(True)
        with mock.patch.dict(sys.modules, {"core.llm_judge": j}):
            ok, _ = A.theme_eligible(
                _cand(brief_description="Supply of office furniture.",
                      _page_text="Tender for the supply of office furniture. " * 20),
                _POL, llm_theme=True)
        self.assertFalse(ok)
        self.assertFalse(j.judge.called, "no theme signal anywhere → no LLM call")

    def test_raw_text_is_accepted_as_the_page(self):
        # Store rows carry the page as raw_text, crawl candidates as _page_text.
        c = _cand(_page_text="")
        c["raw_text"] = _PAGE
        j = self._judge(True)
        with mock.patch.dict(sys.modules, {"core.llm_judge": j}):
            self.assertTrue(A.theme_eligible(c, _POL, llm_theme=True)[0])


class TheArbiterIsWiredTests(unittest.TestCase):
    def test_llm_adjudicate_turns_on_the_theme_judge(self):
        # THE BUG: llm_theme had no caller, so this path had never run in production.
        src = io.open(os.path.join(_ROOT, "core", "auto_scorer.py"),
                      encoding="utf-8").read()
        call = src[src.index("ok, reason = theme_eligible(candidate, policies"):]
        call = call[:call.index(")") + 1]
        self.assertIn("llm_adjudicate", call,
                      "the theme judge must follow the existing LLM opt-in")

    def test_screening_passes_that_opt_in(self):
        src = io.open(os.path.join(_ROOT, "core", "scan_pipeline.py"),
                      encoding="utf-8").read()
        self.assertIn("llm_adjudicate=True", src,
                      "run_screening is the caller that enables adjudication")


if __name__ == "__main__":
    unittest.main(verbosity=2)
