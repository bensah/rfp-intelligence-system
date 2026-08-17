"""Programme areas: one vocabulary, and what a colleague declares is evidence.

FOUR CHANGES (owner, 2026-08-17), all on the same seam - what the organisation can claim
about a theme, and where MUST-2 reads it from.

1. The three places a PERSON records their programme areas were free text next to a graded
   taxonomy the rest of the app matches on. Live proof of why that mattered: the values
   actually stored are "Digital Technology", "Information Technology", "Country Programs" -
   and all three canonicalise to NOTHING, so none of them could ever line up with a call's
   themes. Same list everywhere now.

2. An area a user declares on their own account is worth 5 and OVERRIDES a lower tenant
   rating. A profile is edited by one person, occasionally, so a low rating there is as
   likely to mean "nobody has updated this" as "we are weak here"; a colleague naming an
   area is a person saying this is what they work on. The merge takes the MAXIMUM, so a
   declaration can only ever raise a rating - the profile stays the floor.

3. Domains / areas of expertise now inform MUST-2 ALONGSIDE strategy. They were a
   FALLBACK, consulted only when no strategy existed at all, so a team with a deep track
   record in an area and no strategy row for it scored MUST-2 as if it had no standing
   there. Track record keeps its separate role in PREFER-8 competitiveness, which asks a
   different question.

4. "Strategic priority areas (strategy -> strategic fit)" reads "Strategic priority areas
   of interest".

Run:  python -m unittest tests.test_program_areas_inform_must2
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("SUPABASE_URL", "https://dummy.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "sb_secret_dummy")

from core import criteria_derive as CD                                # noqa: E402
from core import program_area_classifier as PA                        # noqa: E402
from core import user_program_areas as UPA                            # noqa: E402

# Two real canonical keys from the shared taxonomy.
VACCINES = PA.key_for("Women & Children's Health", "Vaccines")
TB = PA.key_for("Infectious Diseases", "Tuberculosis")


class TheOrgCanClaimThreeThingsTests(unittest.TestCase):
    def test_strategy_alone_still_works(self):
        org = {"org_priority_areas": [VACCINES], "org_priority_ratings": {VACCINES: 4}}
        scores = CD._org_theme_scores(org)
        self.assertEqual(scores.get(VACCINES), 4.0)
        # The parent category is scored too, which is pre-existing behaviour: a call
        # classified only at category level ("Women & Children's Health") still has to
        # match. Asserted rather than ignored so a future change to it is visible here.
        self.assertEqual(scores.get(PA.category_full(VACCINES)), 4.0)

    def test_track_record_counts_alongside_strategy_not_only_as_a_fallback(self):
        # The change: TB has a track record but is not a stated priority. It used to be
        # invisible to MUST-2 purely because a strategy list existed.
        org = {"org_priority_areas": [VACCINES], "org_priority_ratings": {VACCINES: 4},
               "org_domain_expertise": [TB], "org_domain_ratings": {TB: 5}}
        scores = CD._org_theme_scores(org)
        self.assertEqual(scores.get(VACCINES), 4.0)
        self.assertEqual(scores.get(TB), 5.0)

    def test_the_higher_of_the_two_wins_for_the_same_area(self):
        org = {"org_priority_areas": [TB], "org_priority_ratings": {TB: 1},
               "org_domain_expertise": [TB], "org_domain_ratings": {TB: 5}}
        self.assertEqual(CD._org_theme_scores(org).get(TB), 5.0)

    def test_no_data_at_all_is_still_no_data(self):
        self.assertEqual(CD._org_theme_scores({}), {})


class ADeclarationIsHardEvidenceTests(unittest.TestCase):
    LOW = {"org_priority_areas": [TB], "org_priority_ratings": {TB: 2}}

    def test_it_overrides_a_lower_tenant_rating(self):
        declared = dict(self.LOW, org_user_declared_areas=[TB])
        self.assertEqual(CD._org_theme_scores(self.LOW).get(TB), 2.0)
        self.assertEqual(CD._org_theme_scores(declared).get(TB),
                         UPA.DECLARED_RATING)

    def test_it_changes_the_must2_verdict(self):
        rfp = {"call_domain_areas": [TB]}
        self.assertEqual(CD.derive_strategic_fit(self.LOW, rfp), "Limited priority")
        self.assertEqual(
            CD.derive_strategic_fit(dict(self.LOW, org_user_declared_areas=[TB]), rfp),
            "Strongly aligns")

    def test_it_can_only_raise_never_lower(self):
        high = {"org_priority_areas": [TB], "org_priority_ratings": {TB: 5}}
        self.assertEqual(
            CD._org_theme_scores(dict(high, org_user_declared_areas=[TB])).get(TB), 5.0)

    def test_it_works_when_the_profile_says_nothing_at_all(self):
        # The case that motivated it: updating a tenant profile is slow, so a declaration
        # must stand on its own.
        org = {"org_user_declared_areas": [TB]}
        self.assertEqual(CD._org_theme_scores(org).get(TB), UPA.DECLARED_RATING)
        self.assertEqual(CD.derive_strategic_fit(org, {"call_domain_areas": [TB]}),
                         "Strongly aligns")

    def test_a_whole_category_declaration_expands(self):
        org = {"org_user_declared_areas": ["Infectious Diseases"]}
        self.assertIn(TB, CD._org_theme_scores(org))


class ReadingWhatUsersDeclaredTests(unittest.TestCase):
    def test_free_text_is_split_on_commas_and_semicolons(self):
        self.assertEqual(UPA._split("Vaccines, Tuberculosis; Malaria & NTDs"),
                         ["Vaccines", "Tuberculosis", "Malaria & NTDs"])

    def test_a_list_is_accepted_as_it_stands(self):
        self.assertEqual(UPA._split([VACCINES, TB]), [VACCINES, TB])

    def test_blank_and_missing_values_yield_nothing(self):
        for v in (None, "", "  ", ",,", []):
            self.assertEqual(UPA._split(v), [])

    def test_todays_stored_free_text_resolves_to_nothing(self):
        # Exactly why the field had to become a dropdown: these are the live values, and
        # none of them is a programme area the matcher can use.
        self.assertEqual(
            PA.expand(["Digital Technology", "Information Technology",
                       "Country Programs"]), set())

    def test_a_declaration_is_worth_the_top_of_the_band(self):
        self.assertEqual(UPA.DECLARED_RATING, 5.0)


class TheDerivedKeyNeverPersistsTests(unittest.TestCase):
    def test_it_is_declared_on_the_default_profile(self):
        from core import org_profile as OP
        self.assertIn("org_user_declared_areas", OP.DEFAULT_PROFILE)

    def test_set_profile_strips_it(self):
        # Writing it back would freeze one moment's answer into the profile and then go
        # stale the instant somebody edits their own account.
        import inspect
        from core import org_profile as OP
        src = inspect.getsource(OP.set_profile)
        self.assertIn("org_user_declared_areas", src)
        self.assertIn("DERIVED keys never persist", src)


class TheUiUsesOneVocabularyTests(unittest.TestCase):
    def _src(self, rel):
        import io
        with io.open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8") as fh:
            return fh.read()

    def test_no_free_text_programme_area_input_remains(self):
        src = self._src("views/account_sections.py")
        self.assertNotIn('"Program areas", help="e.g.', src)
        self.assertEqual(src.count("_program_areas_field("), 4)   # 1 def + 3 call sites

    def test_the_form_safe_multiselect_exists(self):
        from core.program_area_select import program_area_multiselect
        self.assertTrue(callable(program_area_multiselect))

    def test_the_strategic_label_reads_as_the_owner_asked(self):
        for f in ("app_pages/organization.py", "views/org_setup.py"):
            src = self._src(f)
            self.assertIn("Strategic priority areas of interest", src)
            self.assertNotIn("Strategic priority areas (strategy", src)

    def test_no_programme_area_picker_sits_inside_an_st_form(self):
        """A multiselect inside st.form swallows the click on the submit button.

        Reported as "Create user button is stale". This file already carried the same
        failure from a different widget - a selectbox with accept_new_options in this very
        form - and the remedy that worked was moving the widget OUT. Held down here because
        the symptom is invisible in a unit test and only shows up as a dead button.
        """
        src = self._src("views/account_sections.py")
        lines = src.split("\n")
        offenders = []
        for i, ln in enumerate(lines):
            if "_program_areas_field(" not in ln or ln.lstrip().startswith("def "):
                continue
            indent = len(ln) - len(ln.lstrip())
            for j in range(i - 1, max(0, i - 60), -1):
                prev = lines[j]
                if not prev.strip():
                    continue
                ind = len(prev) - len(prev.lstrip())
                if ind >= indent:
                    continue
                if "with st.form(" in prev:
                    offenders.append((i + 1, prev.strip()[:40]))
                break
        self.assertEqual(offenders, [],
                         "programme-area picker(s) inside a form: %r" % (offenders,))

    def test_the_add_user_dialog_survives_a_rerun(self):
        """A dialog opened as `if st.button(): dlg()` is gone after one keystroke.

        Reproduced with AppTest: the dialog's own buttons vanish from the tree on the first
        widget interaction, because the trigger is False on that rerun and the dialog
        function is never called again. The Create-user handler lives inside that function,
        so nothing happens when it is clicked. A persistent open flag is what keeps it.
        """
        src = self._src("views/account_sections.py")
        self.assertIn('st.session_state["adu_open"] = True', src)
        self.assertIn('if st.session_state.get("adu_open"):', src)
        # And it must be cleared, or the dialog reopens after it finishes.
        self.assertIn('st.session_state.pop("adu_open", None)', src)
        self.assertIn('_auto_close_dialog(open_key="adu_open")', src)

    def test_the_users_table_delegates_status_to_the_tested_helper(self):
        src = self._src("views/account_sections.py")
        self.assertIn("from core.user_lifecycle import account_status", src)
        self.assertNotIn('disp["Status"] = disp["is_active"].map(', src)

    def test_saving_a_user_forgets_the_cached_declarations(self):
        # Otherwise an admin adds a colleague, looks at a call, and sees the old verdict
        # with no way to tell whether the declaration took effect.
        src = self._src("views/account_sections.py")
        self.assertIn("def _forget_declared_areas()", src)
        self.assertGreaterEqual(src.count("_forget_declared_areas()"), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AccountStatusTests(unittest.TestCase):
    """An invitation SENT is not an account in USE (owner, 2026-08-17).

    `users.is_active` is true from creation - it has to be, or the person could not sign in
    once they activate - so a status read off that column alone says "Active" for somebody
    who has never opened their invitation, and an admin cannot see who still owes them one.

    Behavioural, not structural, because the first version of this logic looked right and
    never fired: `None` arrives from pandas as the float `nan`, and `str(nan)` is "nan",
    which is truthy. A test that grepped the source for "Pending" passed happily while the
    branch was dead.
    """

    def _s(self, active, must_change, last_login):
        from core.user_lifecycle import account_status
        return account_status(active, must_change, last_login)

    def test_a_freshly_invited_account_is_pending(self):
        from core.user_lifecycle import STATUS_PENDING
        self.assertEqual(self._s(True, True, None), STATUS_PENDING)

    def test_a_nan_last_login_still_counts_as_never(self):
        # The bug: pandas turns a NULL timestamp into float('nan').
        from core.user_lifecycle import STATUS_PENDING
        self.assertEqual(self._s(True, True, float("nan")), STATUS_PENDING)
        self.assertEqual(self._s(True, True, "NaT"), STATUS_PENDING)
        self.assertEqual(self._s(True, True, ""), STATUS_PENDING)

    def test_once_they_have_signed_in_it_is_active(self):
        from core.user_lifecycle import STATUS_ACTIVE
        self.assertEqual(self._s(True, True, "2026-08-17T14:45"), STATUS_ACTIVE)

    def test_a_forced_rotation_on_a_used_account_is_not_pending(self):
        # Somebody who has signed in and been asked to rotate is an active user with a
        # chore, not an outstanding invitation.
        from core.user_lifecycle import STATUS_ACTIVE
        self.assertEqual(self._s(True, True, "2026-01-01"), STATUS_ACTIVE)

    def test_an_activated_account_is_active(self):
        from core.user_lifecycle import STATUS_ACTIVE
        self.assertEqual(self._s(True, False, "2026-08-01"), STATUS_ACTIVE)

    def test_deactivated_beats_everything(self):
        from core.user_lifecycle import STATUS_INACTIVE
        self.assertEqual(self._s(False, True, None), STATUS_INACTIVE)
        self.assertEqual(self._s(False, False, "2026-08-01"), STATUS_INACTIVE)

    def test_it_survives_a_real_pandas_frame(self):
        import pandas as pd
        from core.user_lifecycle import account_status, STATUS_PENDING
        df = pd.DataFrame([{"is_active": True, "must_change_password": True,
                            "last_login_at": None}])
        got = df.apply(lambda r: account_status(r.get("is_active"),
                                               r.get("must_change_password"),
                                               r.get("last_login_at")), axis=1)
        self.assertEqual(list(got), [STATUS_PENDING])
