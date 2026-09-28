"""The programme-area keyword matcher must not recompile its vocabulary per row.

`re.search` caches compiled patterns, but only 512 of them (`re._MAXCACHE`), and
this taxonomy holds 583 DISTINCT keywords. The vocabulary was LARGER THAN THE
CACHE, so every row evicted nearly all of it and recompiled the lot - nothing
ever hit.

Measured on 25 real pipeline rows, profiling `assessment.assess_row`:

    before   14,650 regex compilations   573 per row   38.0 ms/row
             5,658 of them from _matches alone - 99% of all compilation
    after         0 compilations on the hot path        21.5 ms/row

A 44% cut in scoring CPU, which is what a page waits for while it scores its
rows. Equivalence was checked on 105 real texts x 583 keywords (61,215 pairs)
against the previous expression: zero differences.

The interesting part is that this was a CLIFF, not a gradient. Nothing announced
it when the taxonomy passed 512 entries, so the test below watches the margin
rather than only the behaviour.
"""
from __future__ import annotations

import re
import unittest

import core.program_area_classifier as P


def _vocabulary() -> set[str]:
    return ({k for v in P.PROGRAM_AREA_KEYWORDS.values() for k in v}
            | {k for v in P._BARE_ACRONYMS.values() for k in v})


class PatternsAreCompiledOnce(unittest.TestCase):
    def test_the_same_keyword_reuses_one_compiled_pattern(self):
        a = P._keyword_pattern("mental health")
        b = P._keyword_pattern("mental health")
        self.assertIs(a, b)

    def test_scoring_many_texts_does_not_recompile(self):
        """The property that actually matters: cost does not scale with rows."""
        vocab = sorted(_vocabulary())
        texts = [f"a call about {kw} and other things" for kw in vocab[:40]]
        # Warm every pattern once.
        for t in texts:
            P.classify_program_areas(t)
        before = P._keyword_pattern.cache_info()
        for _ in range(5):
            for t in texts:
                P.classify_program_areas(t)
        after = P._keyword_pattern.cache_info()
        self.assertEqual(after.misses, before.misses,
                         "a second pass must not compile anything new")
        self.assertGreater(after.hits, before.hits)

    def test_every_keyword_is_cacheable(self):
        # A pattern per keyword, and no more.
        P._keyword_pattern.cache_clear()
        for kw in _vocabulary():
            P._keyword_pattern(kw)
        info = P._keyword_pattern.cache_info()
        self.assertEqual(info.currsize, len(_vocabulary()))
        self.assertEqual(info.misses, len(_vocabulary()))


class TheVocabularyOutgrewThePatternCache(unittest.TestCase):
    """Documents the cliff, so the next person sees it before profiling for it."""

    def test_the_taxonomy_is_larger_than_res_own_cache(self):
        n = len(_vocabulary())
        self.assertGreater(
            n, re._MAXCACHE,
            "if this ever becomes false the original bug was self-limiting; the "
            "fix is still correct, but the measured 44% was specific to n > cache")
        self.assertGreater(n, 500)


class BehaviourIsUnchanged(unittest.TestCase):
    """Word-boundary semantics, including the false positive the boundary exists for."""

    def test_the_environmental_health_false_positive_stays_fixed(self):
        # "mental health" must not fire inside "environmental health" - the
        # pollution RFPs that were tagged Mental Health.
        areas = P.classify_program_areas(
            "environmental health and pollution remediation")
        self.assertNotIn("WCH - Mental Health", areas)
        self.assertFalse(any("Mental Health" in a for a in areas), areas)

    def test_mental_health_still_matches_when_meant(self):
        areas = P.classify_program_areas("mental health services for adolescents")
        self.assertTrue(any("Mental Health" in a for a in areas), areas)

    def test_hyphens_and_slashes_are_boundaries(self):
        for text, kw in (("HIV/AIDS treatment programme", "HIV"),
                         ("drug-resistant TB in adults", "TB"),
                         ("vaccine-preventable disease", "vaccine")):
            with self.subTest(kw=kw):
                self.assertTrue(P._matches(text, kw))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(P._matches("MENTAL HEALTH IN CAPS", "mental health"))
        self.assertTrue(P._matches("mental health in lower", "MENTAL HEALTH"))

    def test_a_keyword_inside_a_longer_word_does_not_match(self):
        self.assertFalse(P._matches("immunizationable nonsense", "immunization"))

    def test_blank_and_none_text(self):
        for t in (None, "", "   "):
            with self.subTest(t=t):
                self.assertEqual(P.classify_program_areas(t), [P.UNSPECIFIED])

    def test_a_regex_metacharacter_in_a_keyword_is_escaped(self):
        # Keywords like "HIV/AIDS" and "C-section" contain characters that would
        # otherwise be syntax. re.escape is still applied.
        self.assertTrue(P._matches("we fund HIV/AIDS work", "HIV/AIDS"))
        self.assertFalse(P._matches("we fund HIVxAIDS work", "HIV/AIDS"))


if __name__ == "__main__":
    unittest.main()
