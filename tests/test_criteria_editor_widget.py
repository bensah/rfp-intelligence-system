"""The component editor as a REAL Streamlit widget, driven by AppTest.

Everything here is about wiring that only breaks at runtime: which widget is rendered,
what it is preselected to, whether it is disabled, and whether changing it moves the
criterion label. A widget bug in this editor was previously found only by clicking through
the app by hand, so the wiring is worth holding down in CI.
"""
from __future__ import annotations

import sys
import textwrap
import unittest

import streamlit as _real_streamlit
from streamlit.testing.v1 import AppTest

# tests/test_tenant_isolation.py installs a FAKE `streamlit` module in sys.modules and
# leaves it there for the rest of the process (its own subjects resolve the module lazily,
# so it cannot restore the real one). AppTest does a function-local `import streamlit` on
# every run, so under the full suite it would pick up that stub and die with
# "module 'streamlit' has no attribute 'secrets'". Capture the real module at import time
# — this file sorts before test_tenant_isolation, so it is still real here — and reinstall
# it around each run. Order-independent either way.
_captured_streamlit = _real_streamlit


def setUpModule():
    global _stashed
    _stashed = sys.modules.get("streamlit")
    sys.modules["streamlit"] = _captured_streamlit


def tearDownModule():
    if _stashed is not None:
        sys.modules["streamlit"] = _stashed

# One script for every test; the fixture is chosen with a session_state flag so each test
# drives the same production function, `views.criteria_editor.render_component_editor`.
SCRIPT = textwrap.dedent("""
    import sys
    sys.path.insert(0, r"{root}")
    import streamlit as st
    from views.criteria_editor import render_component_editor

    FIXTURES = {{
        # MUST-1 as it really is on a call that imposes no qualification requirement:
        # six hard components, none of them activated by the derivation.
        "must1_nothing_imposed": ("qualification", "MUST 1", [
            dict(key="applicant_type", name="Eligible legal type", active=False,
                 score=None, met=None, hard=True),
            dict(key="entity_type", name="Entity type", active=False,
                 score=None, met=None, hard=True),
            dict(key="donor_hq_country", name="HQ country", active=False,
                 score=None, met=None, hard=True),
        ], "Not sure"),
        # MUST-5 with only the all-clear active, plus the SAM/UEI row that must stay
        # locked for a funder that is not a US government body.
        "must5_all_clear": ("cofinancing", "MUST 5", [
            dict(key="sam_uei", name="SAM.gov / UEI registration", active=False,
                 score=None, met=None, hard=True),
            dict(key="tax_exempt", name="Tax-exempt status", active=False,
                 score=None, met=None, hard=True),
            dict(key="compliance_all_clear", name="All requirements met", active=True,
                 score=1.0, met=True, hard=False),
        ], "Yes, fully met"),
        # PREFER-9 exactly as the live row had it: both components met, stored label
        # frozen at "Tight but doable, with a team".
        "prefer9_two_of_two": ("bid_effort", "PREFER 9", [
            dict(key="bid_time", name="Submitted on time", active=True,
                 score=1.0, met=True),
            dict(key="bid_team", name="Has a BD team", active=True,
                 score=None, met=True),
        ], "Ample time, sufficient resources"),
        # PREFER-8: the derivation is authoritative, so edits must NOT rename it.
        "prefer8_derivation_wins": ("competitiveness", "PREFER 8", [
            dict(key="comp_track", name="Track record", active=True,
                 score=1.0, met=True),
            dict(key="comp_age", name="Established", active=True,
                 score=None, met=True),
        ], "Moderate"),
    }}

    key, title, items, derived = FIXTURES[st.session_state["fixture"]]
    collect = {{}}
    lbl = render_component_editor("U1", key, title, items, derived, collect=collect)
    st.text(f"LABEL={{lbl}}")
    st.text(f"COLLECTED={{collect}}")
""")


def _app(fixture: str) -> AppTest:
    import pathlib
    root = str(pathlib.Path(__file__).resolve().parent.parent)
    at = AppTest.from_string(SCRIPT.format(root=root))
    at.session_state["fixture"] = fixture
    return at


def _label(at: AppTest) -> str:
    for t in at.text:
        if t.value.startswith("LABEL="):
            return t.value[len("LABEL="):]
    raise AssertionError("no LABEL emitted")


def _collected(at: AppTest) -> dict:
    for t in at.text:
        if t.value.startswith("COLLECTED="):
            return eval(t.value[len("COLLECTED="):])   # noqa: S307 - our own literal
    raise AssertionError("no COLLECTED emitted")


class TestMust1RendersLikeEveryOtherCriterion(unittest.TestCase):
    """#1 — it used to fall back to a criterion-level dropdown when no component was
    active, which is the whole reason it looked different."""

    def test_no_criterion_level_dropdown_is_offered(self):
        at = _app("must1_nothing_imposed").run()
        self.assertFalse(at.exception)
        # Every selectbox on screen is a COMPONENT (4 options: — / 0 / 0.5 / 1), never a
        # criterion response list ("Yes, fully", "Mostly, one item unclear", ...).
        for sb in at.selectbox:
            self.assertEqual(list(sb.options), ["—", "0", "0.5", "1"])

    def test_one_editable_row_per_component(self):
        at = _app("must1_nothing_imposed").run()
        self.assertEqual(len(at.selectbox), 3)
        for sb in at.selectbox:
            self.assertFalse(sb.disabled)

    def test_every_unmeasured_component_starts_at_a_dash(self):
        at = _app("must1_nothing_imposed").run()
        for sb in at.selectbox:
            self.assertEqual(sb.value, "—")

    def test_nothing_is_collected_until_a_human_sets_something(self):
        at = _app("must1_nothing_imposed").run()
        self.assertEqual(_collected(at), {"qualification": {}})


class TestSettingAGreyedComponentActivatesItLive(unittest.TestCase):
    """The chosen semantics: a reviewer asserting a requirement applies makes it count,
    and the label moves before anything is saved."""

    def test_setting_one_component_moves_the_label(self):
        at = _app("must1_nothing_imposed").run()
        self.assertEqual(_label(at), "Not sure")
        at.selectbox[0].set_value("0.5").run()
        self.assertFalse(at.exception)
        self.assertEqual(_label(at), "Mostly, one item unclear")

    def test_a_zero_on_a_hard_component_fails_the_criterion(self):
        at = _app("must1_nothing_imposed").run()
        at.selectbox[0].set_value("0").run()
        self.assertEqual(_label(at), "No, not eligible")

    def test_a_full_pass_reads_yes(self):
        at = _app("must1_nothing_imposed").run()
        at.selectbox[0].set_value("1").run()
        self.assertEqual(_label(at), "Yes, fully")

    def test_only_the_component_the_human_set_is_collected(self):
        at = _app("must1_nothing_imposed").run()
        at.selectbox[1].set_value("0.5").run()
        self.assertEqual(_collected(at), {"qualification": {"entity_type": 0.5}})

    def test_returning_a_component_to_the_dash_withdraws_the_verdict(self):
        at = _app("must1_nothing_imposed").run()
        at.selectbox[0].set_value("0").run()
        self.assertEqual(_label(at), "No, not eligible")
        at.selectbox[0].set_value("—").run()
        self.assertEqual(_label(at), "Not sure")
        # The clear is RECORDED as None rather than vanishing from the dict. It has to be:
        # a component the scan scored needs an explicit "do not score this" to persist, or
        # the derived value simply returns on the next render.
        self.assertEqual(_collected(at), {"qualification": {"applicant_type": None}})


class TestOnlyHumanSetValuesArePersisted(unittest.TestCase):
    """#2 — the old per-criterion dirty flag persisted the DERIVED score of every active
    component in a criterion as soon as one was edited, freezing the derivation for
    components nobody had looked at."""

    def test_editing_one_component_does_not_capture_its_neighbours(self):
        at = _app("prefer9_two_of_two").run()
        self.assertEqual(_collected(at), {"bid_effort": {}})
        at.selectbox[0].set_value("0.5").run()
        collected = _collected(at)["bid_effort"]
        self.assertEqual(collected, {"bid_time": 0.5})
        self.assertNotIn("bid_team", collected)      # untouched → derivation keeps it


class TestSamUeiStaysLocked(unittest.TestCase):
    """#5 — the single deliberate exception to "every component is editable"."""

    def test_sam_uei_is_disabled_and_every_other_row_is_not(self):
        at = _app("must5_all_clear").run()
        by_key = {sb.key: sb for sb in at.selectbox}
        self.assertTrue(by_key["qsel_U1_cofinancing_sam_uei"].disabled)
        self.assertFalse(by_key["qsel_U1_cofinancing_tax_exempt"].disabled)
        self.assertFalse(by_key["qsel_U1_cofinancing_compliance_all_clear"].disabled)

    def test_a_measured_component_shows_its_value_not_a_dash(self):
        at = _app("must5_all_clear").run()
        by_key = {sb.key: sb for sb in at.selectbox}
        self.assertEqual(by_key["qsel_U1_cofinancing_compliance_all_clear"].value, "1")
        # ...while the two the call never imposed read "—", NOT 0.0.
        self.assertEqual(by_key["qsel_U1_cofinancing_sam_uei"].value, "—")
        self.assertEqual(by_key["qsel_U1_cofinancing_tax_exempt"].value, "—")


def _row_html(at: AppTest, name: str) -> str:
    """The rendered label cell for one component row."""
    for m in at.markdown:
        if name in m.value and "padding-top:0.5rem" in m.value:
            return m.value
    raise AssertionError(f"no rendered row for {name!r}")


class TestGreyingFollowsTheValue(unittest.TestCase):
    """Owner 2026-08-10: a component is greyed exactly when it has NO value. "—" is greyed;
    0 / 0.5 / 1 is live. Reading `active` off the pre-edit item meant a row the reviewer had
    just scored kept rendering greyed and captioned "not required" with its own value
    sitting in the box beside it."""

    def test_a_dash_row_is_greyed(self):
        at = _app("must1_nothing_imposed").run()
        html = _row_html(at, "Entity type")
        self.assertIn("color:#aaa", html)
        self.assertIn("not required", html)

    def test_scoring_a_greyed_row_ungreys_it(self):
        at = _app("must1_nothing_imposed").run()
        by_key = {sb.key: sb for sb in at.selectbox}
        by_key["qsel_U1_qualification_entity_type"].set_value("1").run()
        html = _row_html(at, "Entity type")
        self.assertNotIn("color:#aaa", html)          # no longer greyed
        self.assertNotIn("not required", html)        # it IS required now — by the reviewer
        self.assertIn("set by you", html)

    def test_every_allowed_value_activates_the_row(self):
        for val in ("0", "0.5", "1"):
            with self.subTest(value=val):
                at = _app("must1_nothing_imposed").run()
                by_key = {sb.key: sb for sb in at.selectbox}
                by_key["qsel_U1_qualification_entity_type"].set_value(val).run()
                html = _row_html(at, "Entity type")
                self.assertNotIn("color:#aaa", html)

    def test_returning_to_the_dash_greys_it_again(self):
        at = _app("must1_nothing_imposed").run()
        by_key = {sb.key: sb for sb in at.selectbox}
        by_key["qsel_U1_qualification_entity_type"].set_value("1").run()
        self.assertNotIn("color:#aaa", _row_html(at, "Entity type"))
        by_key = {sb.key: sb for sb in at.selectbox}
        by_key["qsel_U1_qualification_entity_type"].set_value("—").run()
        html = _row_html(at, "Entity type")
        self.assertIn("color:#aaa", html)
        self.assertIn("not required", html)

    def test_a_system_scored_row_is_not_greyed(self):
        # Same rule for the scan/cron's own values — no separate rendering for those.
        at = _app("must5_all_clear").run()
        self.assertNotIn("color:#aaa", _row_html(at, "All requirements met"))

    def test_the_selected_value_survives_the_rerun(self):
        at = _app("must1_nothing_imposed").run()
        by_key = {sb.key: sb for sb in at.selectbox}
        by_key["qsel_U1_qualification_entity_type"].set_value("0.5").run()
        by_key = {sb.key: sb for sb in at.selectbox}
        self.assertEqual(by_key["qsel_U1_qualification_entity_type"].value, "0.5")


class TestPrefer8IsNamedByItsOwnModel(unittest.TestCase):
    """Owner 2026-08-10: the derivation is authoritative for PREFER-6 / PREFER-8. The
    components stay editable and are still recorded — they just don't rename the
    criterion — and the editor must SAY so, or a reviewer sets a value, watches the label
    sit still, and concludes the editor is broken."""

    def test_editing_a_component_does_not_rename_the_criterion(self):
        at = _app("prefer8_derivation_wins").run()
        self.assertEqual(_label(at), "Moderate")
        at.selectbox[0].set_value("0").run()
        self.assertFalse(at.exception)
        self.assertEqual(_label(at), "Moderate")      # the model still names it

    def test_the_edit_is_still_recorded(self):
        at = _app("prefer8_derivation_wins").run()
        at.selectbox[0].set_value("0").run()
        self.assertEqual(_collected(at), {"competitiveness": {"comp_track": 0.0}})

    def test_the_editor_explains_why_the_label_does_not_move(self):
        at = _app("prefer8_derivation_wins").run()
        blob = " ".join(c.value for c in at.caption)
        self.assertIn("weighted model", blob)
        self.assertIn("do **not** rename", blob)

    def test_a_normal_criterion_carries_no_such_caption(self):
        at = _app("prefer9_two_of_two").run()
        blob = " ".join(c.value for c in at.caption)
        self.assertNotIn("do **not** rename", blob)


class TestPrefer9NoLongerFreezes(unittest.TestCase):
    """#3 — the live symptom: 2/2 components met, label stuck on "Tight but doable"."""

    def test_the_label_follows_the_components(self):
        at = _app("prefer9_two_of_two").run()
        self.assertEqual(_label(at), "Ample time, sufficient resources")
        self.assertNotEqual(_label(at), "Tight but doable, with a team")

    def test_dropping_the_time_component_reads_tight(self):
        at = _app("prefer9_two_of_two").run()
        at.selectbox[0].set_value("0.5").run()
        self.assertEqual(_label(at), "Tight but doable, with a team")


if __name__ == "__main__":
    unittest.main()


class TestAnExplicitDashClearsTheComponent(unittest.TestCase):
    """Owner 2026-08-10, reported on a live RFP: setting "Co-financing / pre-finance
    capacity" from 0.5 back to "—" appeared to do nothing — the row stayed dark and kept
    counting, while the box sat on "—".

    Cause: the touched-flag was set to `value != DASH`, so choosing "—" ERASED the record
    that the reviewer had touched it. The derived 0.5 came back, but Streamlit kept showing
    the session value "—" (it ignores `index` for a keyed widget), so the display and the
    score disagreed. "—" now means CLEARED: not scored, greyed, out of the denominator."""

    def test_clearing_a_scan_scored_component_greys_it(self):
        at = _app("must5_all_clear").run()
        by = {sb.key: sb for sb in at.selectbox}
        k = "qsel_U1_cofinancing_compliance_all_clear"
        self.assertEqual(by[k].value, "1")                       # scored by the scan
        self.assertNotIn("color:#aaa", _row_html(at, "All requirements met"))
        by[k].set_value("—").run()
        self.assertFalse(at.exception)
        html = _row_html(at, "All requirements met")
        self.assertIn("color:#aaa", html)                         # now greyed
        self.assertIn("cleared by you", html)

    def test_the_box_and_the_styling_agree_after_clearing(self):
        at = _app("must5_all_clear").run()
        by = {sb.key: sb for sb in at.selectbox}
        by["qsel_U1_cofinancing_compliance_all_clear"].set_value("—").run()
        by = {sb.key: sb for sb in at.selectbox}
        # The widget still shows "—" AND the row is styled as unscored — the two used to
        # disagree, which is what made the edit look inert.
        self.assertEqual(by["qsel_U1_cofinancing_compliance_all_clear"].value, "—")
        self.assertIn("color:#aaa", _row_html(at, "All requirements met"))

    def test_clearing_is_recorded_so_it_can_be_persisted(self):
        at = _app("must5_all_clear").run()
        by = {sb.key: sb for sb in at.selectbox}
        by["qsel_U1_cofinancing_compliance_all_clear"].set_value("—").run()
        # None = cleared. It must be PRESENT (not absent), or the save can't persist it.
        self.assertEqual(_collected(at),
                         {"cofinancing": {"compliance_all_clear": None}})

    def test_clearing_every_component_hands_the_label_back_to_the_derivation(self):
        # A clear says "the components should not drive this", not "the system is wrong
        # about the criterion" — so with nothing left to count the DERIVED label shows and
        # the row is styled unscored. The reviewer removed a measurement, not a verdict.
        at = _app("must5_all_clear").run()
        by = {sb.key: sb for sb in at.selectbox}
        by["qsel_U1_cofinancing_compliance_all_clear"].set_value("—").run()
        self.assertEqual(_label(at), "Yes, fully met")            # the derived label
        self.assertIn("color:#aaa", _row_html(at, "All requirements met"))

    def test_a_cleared_component_can_be_scored_again(self):
        at = _app("must5_all_clear").run()
        k = "qsel_U1_cofinancing_compliance_all_clear"
        {sb.key: sb for sb in at.selectbox}[k].set_value("—").run()
        self.assertIn("color:#aaa", _row_html(at, "All requirements met"))
        {sb.key: sb for sb in at.selectbox}[k].set_value("0.5").run()
        html = _row_html(at, "All requirements met")
        self.assertNotIn("color:#aaa", html)
        self.assertIn("set by you", html)
        self.assertEqual(_label(at), "Partial, with effort")

    def test_clearing_one_component_leaves_the_others_alone(self):
        at = _app("must1_nothing_imposed").run()
        # Score BOTH first — setting a widget to the value it already holds fires no
        # change event, so a row already sitting on "—" cannot be "cleared".
        {sb.key: sb for sb in at.selectbox}["qsel_U1_qualification_applicant_type"]             .set_value("1").run()
        {sb.key: sb for sb in at.selectbox}["qsel_U1_qualification_entity_type"]             .set_value("1").run()
        {sb.key: sb for sb in at.selectbox}["qsel_U1_qualification_entity_type"]             .set_value("—").run()
        self.assertEqual(_collected(at), {"qualification": {"applicant_type": 1.0,
                                                           "entity_type": None}})
        self.assertNotIn("color:#aaa", _row_html(at, "Eligible legal type"))
        self.assertIn("color:#aaa", _row_html(at, "Entity type"))
        self.assertEqual(_label(at), "Yes, fully")     # only the scored one counts
