"""A published list of eligible countries outranks a regional keyword.

The SGCI/STISA call names nineteen eligible countries on its own page, under a
`COUNTRIES` heading. The pipeline stored its scope as
`['Africa', 'Sub-Saharan Africa']` - 'Africa' off the title ("advancing Africa's
science ..."), the other off the PROGRAMS line ("Science Granting Councils
Initiative in Sub-Saharan Africa"). Both contain the tenant's countries, so every
geography gate passed a call the tenant cannot apply to, and the nineteen names
reached no field at all: `eligibility_countries` was `[]`.

A region is what you get when nobody reads the list, and it is strictly more
permissive than the list, so this error only ever runs one way - toward admitting
calls.

The page text in these tests is the real page with its whitespace collapsed, the
way `catalog_synthesis.page_text` delivers it. That matters: the heading ends up
inline, so there is no line structure to anchor on, and the labels here were read
off the real page rather than guessed - there is no "eligible countries" phrase
on it anywhere.
"""
from __future__ import annotations

import unittest

import core.auto_scorer as auto_scorer
from core import eligible_countries as EC

# The real page, whitespace-collapsed. Note the accented spellings and the
# curly apostrophe, both of which the page actually uses.
STISA_PAGE = (
    "Home Funding Supporting STISA 2034: SGCI Multilateral Research Call advancing "
    "Africa’s Science, Technology and Innovation priorities Open CALL FOR "
    "Expressions of interest DEADLINE Friday, September 25, 2026 - 23:59 ET "
    "PROGRAMS Education and Science Science Granting Councils Initiative in "
    "Sub-Saharan Africa TOPIC(S) Science and Technology "
    "COUNTRIES Botswana Burkina Faso Côte d’Ivoire Ethiopia Ghana Kenya "
    "Malawi Mozambique Namibia Nigeria Rwanda Senegal Sierra Leone South Africa "
    "Tanzania Togo Uganda Zambia Zimbabwe "
    "FUNDED BY Science Granting Councils Initiative in Sub-Saharan Africa (SGCI) "
    "BUDGET Ranging from CAD50,000 to CAD300,000 per consortium member "
    "POINT OF CONTACT x@example.org TYPE Grant STATUS Open Scope The SGCI "
    "Multilateral Research Call in support of STISA-2034 will fund collaborative, "
    "multi-country research projects across the continent. Eligibility "
    "Applications must be submitted by a consortium consisting of one lead "
    "applicant organization and two to four co-applicant organizations from "
    "participating SGCI countries."
)

THE_NINETEEN = {
    "Botswana", "Burkina Faso", "Côte d'Ivoire", "Ethiopia", "Ghana", "Kenya",
    "Malawi", "Mozambique", "Namibia", "Nigeria", "Rwanda", "Senegal",
    "Sierra Leone", "South Africa", "Tanzania", "Togo", "Uganda", "Zambia",
    "Zimbabwe",
}

# The gate reads policies["countries"]; this is the shape core.policies builds.
ORG_CAMEROON_MALI = {"countries": {"eligible": ["Cameroon", "Mali"],
                                   "broad_terms": [],
                                   "permissive_when_silent": True}}


class TheReportedCall(unittest.TestCase):
    def test_all_nineteen_countries_are_read_off_the_page(self):
        found, label = EC.extract(STISA_PAGE)
        self.assertEqual(set(found), THE_NINETEEN)
        self.assertEqual(len(found), 19)
        # Anchored on the bare metadata heading, which is what the page uses.
        self.assertEqual(label.lower(), "countries")

    def test_the_tenants_countries_are_not_in_the_list(self):
        found, _ = EC.extract(STISA_PAGE)
        self.assertNotIn("Cameroon", found)
        self.assertNotIn("Mali", found)

    def test_the_run_stops_at_the_end_of_the_list(self):
        # "... Zimbabwe FUNDED BY Science Granting Councils ..." must not be
        # walked into. Nothing outside the published list may appear.
        found, _ = EC.extract(STISA_PAGE)
        self.assertEqual(set(found) - THE_NINETEEN, set())

    def test_the_regions_are_dropped_once_countries_are_published(self):
        found, _ = EC.extract(STISA_PAGE)
        scope = EC.drop_broad_when_listed(["Africa", "Sub-Saharan Africa"], found)
        self.assertNotIn("Africa", scope)
        self.assertNotIn("Sub-Saharan Africa", scope)
        self.assertEqual(set(scope), THE_NINETEEN)

    def test_the_call_now_fails_the_applicant_country_gate(self):
        """End to end: the verdict the owner said this row should have had."""
        found, _ = EC.extract(STISA_PAGE)
        before = {"eligibility_countries": [],
                  "call_geographic_scope": ["Africa", "Sub-Saharan Africa"]}
        self.assertEqual(
            auto_scorer.applicant_country_mismatch_reject(before, ORG_CAMEROON_MALI)[0],
            False, "precondition: with no list extracted, nothing can reject it")

        after = {"eligibility_countries": found,
                 "call_geographic_scope": EC.drop_broad_when_listed(
                     ["Africa", "Sub-Saharan Africa"], found)}
        rejects, why = auto_scorer.applicant_country_mismatch_reject(
            after, ORG_CAMEROON_MALI)
        self.assertTrue(rejects)
        self.assertIn("outside the organisation's registered geography", why)


class AccentsAndApostrophes(unittest.TestCase):
    """One unreadable spelling must not truncate the list.

    The walk stops at the first token it cannot read, so before diacritics were
    folded, "Cote d'Ivoire" ended the run and nineteen countries became two. A
    truncated list is worse than none: it is still treated as the published rule,
    and a short list biases toward rejecting.
    """

    def test_either_spelling_of_cote_divoire_is_found(self):
        for spelling in ("Côte d’Ivoire", "Cote d'Ivoire",
                         "Côte d'Ivoire", "Ivory Coast"):
            with self.subTest(spelling=spelling):
                found, _ = EC.extract(
                    f"Eligible countries: Ghana, {spelling}, Kenya.")
                self.assertEqual(len(found), 3, f"{spelling} broke the run")

    def test_an_unknown_spelling_inside_a_list_is_stepped_over(self):
        # "Congo-Brazaville" and "CAR" both appear in the live store spelled in
        # ways the vocabulary does not carry.
        found, _ = EC.extract(
            "Eligible countries: Ghana, Congo-Brazaville, Kenya, Senegal.")
        self.assertIn("Ghana", found)
        self.assertIn("Kenya", found)
        self.assertIn("Senegal", found)

    def test_the_skip_budget_does_not_run_into_the_next_section(self):
        found, _ = EC.extract(
            "COUNTRIES Ghana Kenya Senegal FUNDED BY The Wellcome Trust of London "
            "BUDGET up to USD 200,000 in India and Brazil")
        self.assertEqual(set(found), {"Ghana", "Kenya", "Senegal"})


class WorkGeographyIsNotEligibility(unittest.TestCase):
    """Where the money is spent is a different question from who may apply.

    Pooling them is the mistake `auto_scorer.applicant_countries` exists to
    prevent: a Finnish scheme's ~130-market work geography genuinely includes the
    tenant's country while applicants must be Finland-registered.
    """

    VETOED = (
        "Target countries Kenya Uganda Tanzania Rwanda receive the funding.",
        "Our partner countries Kenya Uganda Tanzania Malawi implement activities.",
        "Beneficiary countries Kenya Uganda Tanzania Malawi Ghana will benefit.",
        "Implementation countries Kenya Uganda Tanzania Ghana Malawi.",
        "Priority countries Kenya Uganda Tanzania Ghana Malawi.",
        "Programme countries Kenya Uganda Tanzania Ghana Malawi.",
    )

    def test_a_work_geography_label_publishes_no_eligibility_list(self):
        for text in self.VETOED:
            with self.subTest(text=text[:40]):
                self.assertEqual(EC.extract(text), ([], ""))


class NotEveryMentionIsAList(unittest.TestCase):
    def test_prose_that_merely_counts_countries(self):
        self.assertEqual(
            EC.extract("The programme has supported work in 12 countries. "
                       "Kenya has seen strong uptake."), ([], ""))

    def test_a_bare_heading_needs_more_than_one_name(self):
        # "COUNTRIES Kenya" is as likely to be prose as a list.
        self.assertEqual(EC.extract("COUNTRIES Kenya FUNDED BY X"), ([], ""))

    def test_an_explicit_label_accepts_a_single_country(self):
        # The real Finland case: eligibility_countries == ['Finland'].
        found, label = EC.extract("Eligible country: Finland")
        self.assertEqual(found, ["Finland"])
        self.assertTrue(label)

    def test_an_income_tier_is_not_a_country_list(self):
        self.assertEqual(
            EC.extract("Open to applicants worldwide in all low- and "
                       "middle-income countries."), ([], ""))

    def test_a_page_with_no_geography(self):
        self.assertEqual(
            EC.extract("Grants of up to USD 200,000 for pandemic preparedness."),
            ([], ""))

    def test_blank_input(self):
        for bad in (None, "", "   "):
            with self.subTest(bad=bad):
                self.assertEqual(EC.extract(bad), ([], ""))

    def test_an_implausibly_long_list_is_not_a_rule_about_applicants(self):
        from core import geographies as geo
        many = " ".join(geo.COUNTRIES[:EC.MAX_PLAUSIBLE + 10])
        found, _ = EC.extract("Eligible countries: " + many)
        self.assertEqual(found, [], "a continental work geography must stand down")


class GenuineLabels(unittest.TestCase):
    CASES = (
        ("Eligible countries: Kenya, Uganda, Tanzania.", 3),
        ("Applicants must be based in Kenya, Uganda or Tanzania.", 3),
        ("Open to institutions from Ghana, Nigeria and Senegal.", 3),
        ("Applicants from the following countries: Ghana, Kenya, Malawi.", 3),
        ("Countries of eligibility: Ghana, Kenya, Malawi, Senegal.", 4),
        ("COUNTRIES Botswana Ghana Kenya Malawi", 4),
    )

    def test_each_label_anchors_the_list(self):
        for text, want in self.CASES:
            with self.subTest(text=text[:44]):
                found, label = EC.extract(text)
                self.assertEqual(len(found), want, f"{text!r} -> {found}")
                self.assertTrue(label)


class DropBroadWhenListed(unittest.TestCase):
    def test_nothing_changes_without_a_published_list(self):
        scope = ["Africa", "Sub-Saharan Africa", "Kenya"]
        self.assertEqual(EC.drop_broad_when_listed(scope, []), scope)

    def test_income_tiers_and_worldwide_go_too(self):
        out = EC.drop_broad_when_listed(
            ["Low- and middle-income countries (LMICs)", "Global / worldwide",
             "Global South"], ["Kenya", "Ghana"])
        self.assertEqual(set(out), {"Kenya", "Ghana"})

    def test_a_country_already_in_scope_is_not_duplicated(self):
        out = EC.drop_broad_when_listed(["Kenya", "Sub-Saharan Africa"],
                                        ["Kenya", "Ghana"])
        self.assertEqual(out.count("Kenya"), 1)
        self.assertEqual(set(out), {"Kenya", "Ghana"})

    def test_it_tolerates_none(self):
        self.assertEqual(EC.drop_broad_when_listed(None, ["Kenya"]), ["Kenya"])
        self.assertEqual(EC.drop_broad_when_listed(None, []), [])


class TheCapMatchesTheData(unittest.TestCase):
    """The old cap of 5 sat below every genuine list in the store.

    Measured over all 112 store rows that publish an applicant-country list,
    raising it to 60 changes exactly two verdicts, both correct rejections
    (a 16-country research network and a 6-country European call, neither
    including the tenant's countries).
    """

    def test_a_nineteen_country_list_is_usable(self):
        self.assertGreater(auto_scorer._APPLICANT_COUNTRY_MAX, 19)

    def test_the_cap_still_stands_down_on_a_continental_work_geography(self):
        from core import geographies as geo
        many = list(geo.COUNTRIES[:130])
        rejects, _ = auto_scorer.applicant_country_mismatch_reject(
            {"eligibility_countries": many}, ORG_CAMEROON_MALI)
        self.assertFalse(rejects)

    def test_a_list_containing_a_region_is_still_unusable(self):
        """The guard that actually protects the mis-filed case."""
        rejects, _ = auto_scorer.applicant_country_mismatch_reject(
            {"eligibility_countries": ["EU Member States", "Iceland", "Norway",
                                       "Canada", "Israel", "Switzerland"]},
            ORG_CAMEROON_MALI)
        self.assertFalse(rejects)

    def test_a_sixteen_country_list_excluding_the_org_now_rejects(self):
        rejects, why = auto_scorer.applicant_country_mismatch_reject(
            {"eligibility_countries": [
                "Austria", "Belgium", "Canada", "Chile", "Estonia", "France",
                "Germany", "Lithuania", "Netherlands", "Portugal", "South Africa",
                "South Korea", "Spain", "Sweden", "Turkey", "Switzerland"]},
            ORG_CAMEROON_MALI)
        self.assertTrue(rejects, why)

    def test_the_finland_case_the_cap_was_written_for(self):
        """The mis-filed 130-country list is in a DIFFERENT column.

        Live store, the scheme named in the old comment: `eligibility_countries`
        is `["Finland"]` - length ONE - while `call_geographic_scope` carries 154
        entries. So the work geography the cap was credited with catching never
        appears in the column the cap reads, and a list of one is under any cap.
        The restriction still rejects, as it always did.
        """
        rejects, why = auto_scorer.applicant_country_mismatch_reject(
            {"eligibility_countries": ["Finland"]}, ORG_CAMEROON_MALI)
        self.assertTrue(rejects, why)

    def test_a_long_list_that_includes_the_org_stands_down(self):
        rejects, _ = auto_scorer.applicant_country_mismatch_reject(
            {"eligibility_countries": [
                "Burkina Faso", "Cameroon", "Madagascar", "Mali", "Senegal",
                "Ghana", "Kenya", "Benin", "Burundi", "Cambodia"]},
            ORG_CAMEROON_MALI)
        self.assertFalse(rejects, "the org IS eligible here")


class ColloquialCountryNames(unittest.TestCase):
    """Names funders still publish, which were reaching the gates as free text.

    An unrecognised country name reads as "silent geography" and passes
    permissively, so each of these was a small version of the same failure. They
    were found by this feature: an eligible-country list spelled "Ivory Coast" -
    the spelling in our own store rows - truncated at that word, because the walk
    stops where the vocabulary does.
    """

    ALIASES = {
        "Ivory Coast": "Côte d'Ivoire",
        "Cape Verde": "Cabo Verde",
        "Swaziland": "Eswatini",
        "Burma": "Myanmar",
        "Congo-Brazzaville": "Congo (Brazzaville)",
        "Congo-Kinshasa": "Congo (DRC)",
        "East Timor": "Timor-Leste",
        "Czech Republic": "Czechia",
        "Macedonia": "North Macedonia",
        "Holland": "Netherlands",
    }

    def test_each_resolves_to_its_canonical_country(self):
        from core import geographies as geo
        for alias, canon in self.ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(geo.canonical_geo(alias), canon)
                self.assertEqual(geo.canonical_geo(alias.lower()), canon)

    def test_they_are_found_inside_a_published_list(self):
        found, _ = EC.extract(
            "Eligible countries: Ivory Coast, Cape Verde, Swaziland, Ghana.")
        self.assertEqual(set(found),
                         {"Côte d'Ivoire", "Cabo Verde", "Eswatini", "Ghana"})


if __name__ == "__main__":
    unittest.main()
