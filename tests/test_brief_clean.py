"""Regression tests for the brief-description sanitizer (core.records).

Guards that RAW attachment / legalese dumps never pass as a synthesised summary, while a
genuine clean brief is returned unchanged. The render guard, the scan_pipeline write choke
point, and both backfills all route through looks_raw_brief / clean_brief.

Run:  python -m unittest tests.test_brief_clean
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.records import looks_raw_brief, clean_brief      # noqa: E402

# The actual raw brief seen on the UNOPS record AS-260731-091301.
_UNOPS_RAW = ("[General_conditions_goods_and_services-Sep.-2024.pdf] General Conditions of "
              "Contract Contracts for the Provision of Goods and Services 1. Legal Status of "
              "the Parties: The United Nations Office for Project Services (UNOPS) and the "
              "Contractor 1.1. Pursuant, inter alia, to the Charter of the United Nations "
              "1.2. The Contractor shall have the legal status of an independent contractor.")
# The clean synthesised brief on the newer duplicate AS-260801-000114.
_UNOPS_CLEAN = ("The United Nations Office for Project Services (UNOPS) has issued a Request "
                "for Quotation to procure sterilisation equipment and neonatal incubators for "
                "health facilities. Suppliers are invited to quote for the listed items.")


class LooksRawTests(unittest.TestCase):
    def test_empty_is_raw(self):
        self.assertTrue(looks_raw_brief(None))
        self.assertTrue(looks_raw_brief(""))
        self.assertTrue(looks_raw_brief("   "))

    def test_attachment_tag_is_raw(self):
        self.assertTrue(looks_raw_brief(_UNOPS_RAW))
        self.assertTrue(looks_raw_brief("[terms.docx] Something about terms."))

    def test_legalese_and_clause_numbering_is_raw(self):
        self.assertTrue(looks_raw_brief(
            "General Conditions of Contract. 1. Scope 1.1 Applies. 2. Term 2.1 One year."))

    def test_all_caps_headings_are_raw(self):
        self.assertTrue(looks_raw_brief(
            "GENERAL CONDITIONS OF CONTRACT PROVISION OF GOODS AND SERVICES SECTION ONE"))

    def test_verbatim_prefix_of_raw_text_is_raw(self):
        rt = "The quick brown fox jumped over the lazy dog and then ran far away into town."
        self.assertTrue(looks_raw_brief(rt[:60], raw_text=rt))

    def test_clean_brief_is_not_raw(self):
        self.assertFalse(looks_raw_brief(_UNOPS_CLEAN))
        self.assertFalse(looks_raw_brief(
            "The Gates Foundation invites proposals for malaria vaccine research in Africa."))


class CleanBriefTests(unittest.TestCase):
    def test_raw_returns_empty(self):
        self.assertEqual(clean_brief(_UNOPS_RAW), "")
        self.assertEqual(clean_brief(None), "")

    def test_clean_passes_through(self):
        self.assertEqual(clean_brief(_UNOPS_CLEAN), _UNOPS_CLEAN)

    def test_strips_leading_attachment_tag_from_good_prose(self):
        good = ("[flyer.pdf] The Wellcome Trust is offering discovery awards for bold, "
                "long-term biomedical research across low- and middle-income countries.")
        out = clean_brief(good)
        self.assertFalse(out.startswith("[flyer.pdf]"))
        self.assertIn("Wellcome Trust", out)

    def test_html_is_stripped(self):
        self.assertEqual(
            clean_brief("<p>The fund supports climate and health work.</p>"),
            "The fund supports climate and health work.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
