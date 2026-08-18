"""A workbook belongs to the tenant that uploaded it, and to nobody else.

THE LEAK: an uploaded workbook was written to the repo root and resolved with a glob over
that same directory — one filesystem path for the whole deployment. So a workbook uploaded
by one organisation showed up in every other tenant's Settings, under its owner's name, and
any admin pressing "Sync Excel" would have imported that organisation's pipeline into their
own. Visibility was the symptom; a cross-tenant data import was the consequence.

Run:  python -m unittest tests.test_workbook_isolation
"""
import os
import shutil
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_fake_st = types.ModuleType("streamlit")
_fake_st.session_state = {}
_fake_st.secrets = {}
sys.modules["streamlit"] = _fake_st

os.environ["SUPABASE_JWT_SECRET"] = "test-secret"          # multi-tenant ON
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")
os.environ.pop("EXCEL_SOURCE_PATH", None)

from core import excel_sync as xs        # noqa: E402

TENANT_A = "aaaaaaaa-0000-0000-0000-00000000000a"
TENANT_B = "bbbbbbbb-0000-0000-0000-00000000000b"


class _Base(unittest.TestCase):
    def setUp(self):
        _fake_st.session_state.clear()
        shutil.rmtree(xs.REPO_ROOT / xs.WORKBOOK_DIRNAME, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(xs.REPO_ROOT / xs.WORKBOOK_DIRNAME, ignore_errors=True)
        _fake_st.session_state.clear()

    def _as(self, tenant_id):
        _fake_st.session_state["tenant_id"] = tenant_id


class IsolationTests(_Base):
    def test_one_tenants_upload_is_invisible_to_another(self):
        self._as(TENANT_A)
        xs.save_tenant_workbook("Screener.xlsx", b"tenant A data")
        self.assertEqual(xs.resolve_excel_path()["source"], "tenant upload")

        self._as(TENANT_B)                                  # THE LEAK, in one assertion
        resolved = xs.resolve_excel_path()
        self.assertIsNone(resolved["resolved_path"])
        self.assertIsNone(resolved["source"])

    def test_each_tenant_resolves_its_own(self):
        self._as(TENANT_A)
        xs.save_tenant_workbook("A.xlsx", b"A")
        self._as(TENANT_B)
        xs.save_tenant_workbook("B.xlsx", b"B")
        self.assertEqual(xs.resolve_excel_path()["resolved_path"].read_bytes(), b"B")
        self._as(TENANT_A)
        self.assertEqual(xs.resolve_excel_path()["resolved_path"].read_bytes(), b"A")

    def test_a_tenant_less_session_resolves_nothing(self):
        # Nowhere private to read from is not a reason to fall back to something shared.
        self.assertIsNone(xs.resolve_excel_path()["resolved_path"])

    def test_saving_without_a_tenant_is_refused(self):
        with self.assertRaises(RuntimeError):
            xs.save_tenant_workbook("orphan.xlsx", b"x")


class RepoFallbackTests(_Base):
    """The repo-root glob is the mechanism that leaked. It survives only for single-tenant
    development, where there is no one to leak to."""

    def test_repo_root_is_ignored_under_multi_tenant(self):
        stray = xs.REPO_ROOT / "zz_stray_test_workbook.xlsx"
        stray.write_bytes(b"somebody else's data")
        try:
            self._as(TENANT_B)
            self.assertIsNone(xs.resolve_excel_path()["resolved_path"])
        finally:
            stray.unlink(missing_ok=True)

    def test_repo_root_still_works_single_tenant(self):
        from unittest import mock
        stray = xs.REPO_ROOT / "zz_stray_test_workbook.xlsx"
        stray.write_bytes(b"dev copy")
        try:
            with mock.patch.object(xs, "_multitenant", return_value=False):
                self.assertEqual(xs.resolve_excel_path()["source"], "repo fallback")
        finally:
            stray.unlink(missing_ok=True)


class StorageHygieneTests(_Base):
    def test_a_crafted_filename_cannot_escape_the_tenant_directory(self):
        self._as(TENANT_A)
        dest = xs.save_tenant_workbook("../../etc/evil.xlsx", b"x")
        self.assertEqual(dest.parent, xs.workbook_dir(TENANT_A))
        self.assertEqual(dest.name, "evil.xlsx")

    def test_a_non_xlsx_name_still_lands_as_xlsx(self):
        self._as(TENANT_A)
        self.assertTrue(xs.save_tenant_workbook("book", b"x").name.endswith(".xlsx"))

    def test_uploading_replaces_the_previous_master_workbook(self):
        self._as(TENANT_A)
        xs.save_tenant_workbook("old.xlsx", b"old")
        xs.save_tenant_workbook("new.xlsx", b"new")
        books = sorted(p.name for p in xs.workbook_dir(TENANT_A).glob("*.xlsx"))
        self.assertEqual(books, ["new.xlsx"])       # one master workbook, not a pile

    def test_the_storage_directory_is_gitignored(self):
        with open(os.path.join(_ROOT, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn(".workbooks/", fh.read())


class ManualScanSurfaceTests(unittest.TestCase):
    """Extraction is a platform job over a store shared by every tenant. Its button, its
    counters and its history are the developer tenant's, and showing them to a client
    tenant is what made a shared crawl look like another organisation's activity."""

    def setUp(self):
        with open(os.path.join(_ROOT, "app_pages", "admin.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_upload_is_stored_per_tenant(self):
        self.assertIn("save_tenant_workbook", self.src)
        self.assertNotIn("_dest = _RR / _up.name", self.src)     # the old shared write

    def test_the_extraction_button_is_hidden_not_disabled(self):
        self.assertIn("if _ext_slot is not None else False", self.src)
        self.assertNotIn("disabled=not _dev_admin", self.src)

    def test_counters_and_history_are_developer_only(self):
        self.assertIn("if _ext and not _dev_admin:", self.src)
        self.assertIn("if _dev_admin and extr_logs.empty:", self.src)

    def test_a_client_tenant_is_told_when_the_store_was_refreshed(self):
        self.assertIn("last refreshed by the system administrator", self.src)
        self.assertIn("Eligibility Scan", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ManualScanLayoutTests(unittest.TestCase):
    """Manual Scan is three sub-tabs: Search | Eligibility Scan | Excel Sync.

    The split exists so search — the thing every tenant does constantly — is reachable
    where people look for scanning tools, WITHOUT rearranging the working scan page around
    it. So these assertions are as much about what stayed put as what moved."""

    def setUp(self):
        with open(os.path.join(_ROOT, "app_pages", "admin.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_three_sub_tabs_exist_in_order(self):
        self.assertIn('["🔍 Search", "🎯 Eligibility Scan", "📊 Excel Sync"]', self.src)

    def test_search_reuses_the_existing_engine_rather_than_a_second_one(self):
        # It hands off to the results page on the same contract the header 🔍 uses: query
        # in the URL, session copy as fallback. No second relevance ranking to maintain.
        self.assertIn('st.session_state["site_search_query"] = _q', self.src)
        self.assertIn('st.switch_page("app_pages/search.py")', self.src)
        for helper in ("search_opportunities", "search_donors"):
            self.assertNotIn(helper, self.src)      # results are NOT re-implemented here

    def test_the_excel_ui_lives_in_the_excel_sub_tab(self):
        self.assertIn("_excel_area = _t_excel.container()", self.src)
        self.assertIn("_xls_slot = _excel_area.empty() if _show_excel else None", self.src)
        self.assertIn("with _excel_area:", self.src)

    def test_the_scan_controls_and_histories_stayed_together(self):
        scan = self.src.split("with _t_scan:", 1)[1].split("with _excel_area:", 1)[0]
        for kept in ("admin_match_btn", "Eligible funding history", "Extraction history"):
            self.assertIn(kept, scan, kept)
