"""Reusable RFP submission form.

Rendered by the standalone Submit page (app_pages/submit_rfp.py, opened from the
Home "Submit Discovered RFP" button) and reusable inside any container. Each
caller passes a unique `key_prefix` so widget keys don't collide when the form
is mounted in two places in the same Streamlit session.

Note: this is intentionally NOT wrapped in st.form — sections need to react to
selections (the Team section only shows for a Proceed/Park decision, and the
"Other → specify" inputs reveal on selection), which st.form would suppress.

DEDUP POLICY (per user request, 2026-06-05):
  The submit handler no longer blocks on duplicates. Every form submission
  inserts immediately so the submitter's act of capturing an RFP is never
  lost. The dedup layer still runs at DISPLAY time (`core/deduplicator.py`
  is consumed by Tracking / Report views) so KPIs aren't inflated by
  re-submissions — they just don't gate the insert anymore.

  Rationale: this is manual entry by team members whose primary friction
  cost is "did my submission go through". A duplicate-detected warning
  was a friction point with low signal (most "duplicates" were intentional
  re-submissions after a status change).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

import streamlit as st

from core import dropdowns
from core.review_week import review_week_label
from core.scorer import CRITERION_RESPONSES, score_submission
from core.uid_generator import generate_uid
from db.supabase_client import get_client


NONE = "—"


def render_submit_form(
    user: dict[str, Any],
    *,
    key_prefix: str = "page",
    on_success: Callable[[dict], None] | None = None,
) -> None:
    """Render the full RFP submission form.

    Args:
      user: dict with at least `name` and `email` (from login_gate / ensure_logged_in).
      key_prefix: appended to every widget key so the same form can be
                  mounted twice in one Streamlit page (e.g. both as a
                  standalone page AND inside a modal via @st.dialog).
      on_success: optional callback fired after a successful insert.
                  Receives the inserted row dict. Use this to dismiss
                  the modal, navigate, or refresh KPIs. If omitted, the
                  form renders an inline success message.
    """
    sb = get_client()

    # Dropdown sources — loaded fresh each render so admin edits propagate.
    team = dropdowns.get("team_members")
    donor_list = dropdowns.get("donors")
    elig = dropdowns.get("eligibility_values")
    feasibility = dropdowns.get("feasibility")
    applicant_roles = dropdowns.get("applicant_role")
    funding_windows = dropdowns.get("funding_window")
    time_to_award = dropdowns.get("time_to_award")
    submission_formats = dropdowns.get("submission_format")
    program_areas = dropdowns.get("call_domain_areas")
    geo_scope = dropdowns.get("call_geographic_scope")
    decisions = dropdowns.get("decisions")
    currencies = [c["code"] for c in dropdowns.load().get("currencies", [])]

    def _k(name: str) -> str:
        """Widget-key with prefix so concurrent mounts don't collide."""
        return f"sf_{key_prefix}_{name}"

    def _none_first(opts: list[str]) -> list[str]:
        return [NONE] + list(opts)

    # Carries the resolved "Other → specify" values out of the form context.
    # Local to this function call, so concurrent mounts don't trample each other.
    resolved: dict[str, object] = {}

    def _single_with_other(label: str, options: list[str], key: str,
                           help_text: str | None = None):
        full_key = _k(key)
        sel = st.selectbox(label, _none_first(options), key=full_key, help=help_text)
        if sel == "Other":
            spec = st.text_input(f"↳ Specify {label.split(' — ')[0]} *",
                                 key=f"{full_key}__other")
            resolved[key] = spec.strip() or None
        elif sel == NONE:
            resolved[key] = None
        else:
            resolved[key] = sel
        return resolved[key]

    def _multi_with_other(label: str, options: list[str], key: str,
                          help_text: str | None = None):
        full_key = _k(key)
        sel = st.multiselect(label, options, key=full_key, help=help_text)
        extras: list[str] = []
        if "Other" in sel:
            raw = st.text_input(
                f"↳ Other {label.split(' — ')[0].lower()}(s) — comma-separated *",
                key=f"{full_key}__other",
            )
            extras = [v.strip() for v in raw.split(",") if v.strip()]
        final = [v for v in sel if v != "Other"] + extras
        resolved[key] = final or None
        return final

    def _none(v):
        return None if v in (NONE, "", None) else v

    def _crit(label: str, key: str, criterion: str, desc: str = ""):
        """Per-criterion dropdown using the CURRENT response vocabulary
        (core.scorer.CRITERION_RESPONSES). Renders the question title + its
        guiding description (same wording as the MS Form) above the dropdown so
        the answer direction is unambiguous. Defaults to 'Not sure' (= unscored /
        missing) so the submitter sets only what they actually know."""
        opts = CRITERION_RESPONSES.get(criterion) or list(elig)
        didx = opts.index("Not sure") if "Not sure" in opts else 0
        st.markdown(f"**{label}**")
        if desc:
            st.caption(desc)
        return st.selectbox(label, opts, index=didx, key=_k(key),
                            label_visibility="collapsed")

    # ------------------------------------------------------------------
    # Form
    # ------------------------------------------------------------------
    # NOT an st.form: the Team section (5) must appear/disappear live as the Bid
    # decision changes, and the "Other → specify" inputs must reveal on selection
    # — st.form suppresses per-widget reruns, so a plain container is used and a
    # normal st.button drives submission.
    with st.container():
        st.caption(
            f"Submitting as **{user.get('name') or user.get('email')}** "
            f"(logged-in user). Sign out and back in to submit on behalf "
            f"of another team member."
        )

        st.subheader("1. Opportunity description")
        c1, c2 = st.columns([2, 1])
        title = c1.text_input("Opportunity title *", max_chars=300, key=_k("title"))
        opportunity_id = c2.text_input("Opportunity ID (optional)", key=_k("opp_id"))

        _single_with_other(
            "Donor *", donor_list, key="funder",
            help_text="The donor (full name) — matches the Donor Intelligence "
                      "catalog. Type to search; pick 'Other' if not listed.",
        )
        brief = st.text_area("Brief description", height=110, key=_k("brief"))

        c3, c4, c5 = st.columns(3)
        date_posted = c3.date_input("Date posted", value=None, key=_k("dp"))
        deadline = c4.date_input("Submission deadline", value=None, key=_k("dl"))
        award_date = c5.date_input("Expected award date", value=None, key=_k("aw"))

        c6, c7, c8, c9 = st.columns(4)
        with c6:
            role = st.selectbox("Applicant role", _none_first(applicant_roles),
                                key=_k("applicant_role"),
                                help="How the lead applicant participates: Prime "
                                     "(lead beneficiary), Sub (sub-recipient), or "
                                     "Technical (assists another applicant — e.g. a "
                                     "government on a Pandemic Fund bid — without being "
                                     "a beneficiary).")
        with c7:
            window = st.selectbox("Funding window", _none_first(funding_windows),
                                  key=_k("window"))
        with c8:
            tta = st.selectbox("Time to award", _none_first(time_to_award),
                               key=_k("tta"))
        with c9:
            _single_with_other("Submission format", submission_formats,
                               key="sub_format")

        c10, c11, c12 = st.columns([2, 1, 1])
        link = c10.text_input("Opportunity link (URL)", key=_k("link"))
        value = c11.number_input("Estimated value", min_value=0.0,
                                 step=10000.0, value=0.0, key=_k("value"))
        currency = c12.selectbox("Currency", _none_first(currencies),
                                 key=_k("currency"))

        c13, c14 = st.columns(2)
        duration = c13.number_input("Project duration (months)", min_value=0,
                                    step=1, value=0, key=_k("duration"))
        focus_theme = c14.text_input("Focus theme (optional)", key=_k("focus"))

        # Lead / Sub applicant — surfaced here (was only on the Grants page)
        # so the submitter can capture the partnership structure up-front.
        c15, c16 = st.columns(2)
        lead_applicant = c15.text_input(
            "Lead applicant *",
            key=_k("lead_app"),
            help="The organisation leading the application — e.g. your org, or a "
                 "government / partner you are supporting. Always required.",
        )
        sub_applicant = c16.text_input(
            "Sub applicant (optional)",
            key=_k("sub_app"),
            help="Sub-recipient / co-applicant, if one is known at the time you "
                 "log the opportunity.",
        )

        _multi_with_other("Geographic scope", geo_scope, key="geo")
        _multi_with_other("Program area(s)", program_areas, key="program")

        st.subheader("2. Eligibility")
        st.caption(
            "Answer each criterion with the best-fit option. **Not sure** leaves it "
            "unscored (treated as missing — it won't drag the score down). MUST 1–5 "
            "are hard; any clear **No** means decline."
        )

        grid_l, grid_r = st.columns(2)
        with grid_l:
            m1 = _crit(
                "MUST 1 — Legal status & qualification", "m1", "qualification",
                "Are we legally eligible to apply — applicant/legal type, HQ country, "
                "individual-vs-org award, and local registration? These are "
                "non-dynamic gates (can't change before the deadline). Compliance "
                "paperwork (SAM, tax, partner, board) now sits in MUST 5.")
            m2 = _crit(
                "MUST 2 — Strategic fit", "m2", "strategic_fit",
                "Does this RFP fit our defined strategic priorities?")
            m3 = _crit(
                "MUST 3 — Implementation capacity", "m3", "capacity",
                "Can we implement at the stated award size and scope (team, systems, "
                "a grant size we can manage)?")
            m4 = _crit(
                "MUST 4 — Geographic fit", "m4", "geographic_fit",
                "Does the funder's geographic scope align with where we currently "
                "have operations / programs or a trusted partner?")
            m5 = _crit(
                "MUST 5 — Cofinancing & compliance", "m5", "cofinancing",
                "Can we meet any co-financing / match and the funder's compliance "
                "requirements (SAM/UEI, tax-exempt, safeguarding, …)? Yes, fully met → "
                "we meet them all; Not met → at least one required item we can't meet "
                "(this gates the whole criterion).")
        with grid_r:
            p6 = _crit(
                "PREFER 6 — Funding quality", "p6", "funding_quality",
                "Is the award size attractive — within our preferred funding range "
                "(not too small to bother with, not beyond what we can absorb)?")
            p7 = _crit(
                "PREFER 7 — Donor relationship", "p7", "funder_relationship",
                "Do we have a relationship with this funder — a current/past grant, "
                "registration/contact on their portal, OR a shared partner who already "
                "works with this donor?")
            p8 = _crit(
                "PREFER 8 — Competitiveness", "p8", "competitiveness",
                "How well-positioned are we to win — track record in this exact "
                "area, incumbency, and fit to the funder's requirements "
                "(board / grassroots / co-financing / multi-country / HQ)?")
            p9 = _crit(
                "PREFER 9 — Bid effort", "p9", "bid_effort",
                "Is the bid effort feasible — enough time to the deadline and a "
                "business-development / proposal team to do it justice?")

        # Feasibility = the submitter's own subjective read on the opportunity —
        # the human counterpart to the auto-scan tier that derives Proceed/Park/
        # Decline from the criteria above. Not a scored criterion.
        c_f, _spacer = st.columns([1, 3])
        feas = c_f.selectbox(
            "Feasibility (your read)", _none_first(feasibility), key=_k("feas"),
            help="Your subjective gut-rating of how promising this RFP is — the "
                 "human analogue of the auto-scan's Proceed/Park/Decline tier. "
                 "Optional; not scored.")

        st.subheader("3. Decline flags & risks")
        c_df, _spacer2 = st.columns([1, 3])
        decline_flags = c_df.radio(
            "Decline flags present? *", ["No", "Yes"],
            horizontal=True, key=_k("decline_flags"),
        )
        key_risks = st.text_area("Key risks (one line)", height=80, key=_k("risks"))

        st.subheader("4. Initial decision (your recommendation)")
        c_d, c_dr = st.columns([1, 3])
        decision = c_d.selectbox("Bid decision *", _none_first(decisions),
                                 key=_k("decision"))
        rationale = c_dr.text_area("Decision rationale (1–2 lines)",
                                   height=80, key=_k("rationale"))

        # Section 5 (Team) is only relevant when we intend to pursue the bid —
        # show it for Proceed / Park, hide it for Decline (or an unset decision).
        _dec_lower = str(_none(decision) or "").strip().lower()
        show_team = _dec_lower.startswith("proceed") or _dec_lower.startswith("park")
        if show_team:
            st.subheader("5. Team")
            c_lead, _ = st.columns([1, 1])
            with c_lead:
                _single_with_other("Proposal lead *", team, key="prop_lead")
            _multi_with_other("Contributors", team, key="contributors")
            _multi_with_other("Reviewers", team, key="reviewers")

        submitted = st.button("Submit RFP", type="primary", key=_k("submit_btn"))

    # ------------------------------------------------------------------
    # Validate + build the row
    # ------------------------------------------------------------------
    if not submitted:
        return

    errors: list[str] = []
    if not title.strip():
        errors.append("Opportunity title is required.")
    if not resolved.get("funder"):
        errors.append("Funder is required.")
    if not (lead_applicant or "").strip():
        errors.append("Lead applicant is required.")
    if _none(decision) is None:
        errors.append("Decision is required.")
    # Proposal lead is only required when the Team section is shown (Proceed/Park).
    if show_team and not resolved.get("prop_lead"):
        errors.append("Proposal lead is required for a Proceed / Park decision.")
    if errors:
        st.error("Please fix the following:\n\n- " + "\n- ".join(errors))
        return

    vals = {
        "qualification": m1,
        "strategic_fit": m2,
        "capacity": m3,
        "geographic_fit": m4,
        "cofinancing": m5,
        "funding_quality": p6,
        "funder_relationship": p7,
        "competitiveness": p8,
        "bid_effort": p9,
    }
    decline_bool = decline_flags == "Yes"
    align, rec = score_submission(vals, decline_bool)
    submitter = user.get("name") or user.get("email") or "XX"
    uid = generate_uid(str(submitter))
    row = {
        "uid": uid,
        "form_id": uid,
        "source": "manual",
        "submitted_by": submitter,
        "submitted_by_email": user.get("email"),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "search_date": datetime.now(timezone.utc).isoformat(),
        "opportunity_id": _none(opportunity_id),
        "opportunity_title": title.strip(),
        "brief_description": _none(brief),
        "date_posted": date_posted.isoformat() if isinstance(date_posted, date) else None,
        "funding_agency": resolved.get("funder"),
        "call_geographic_scope": resolved.get("geo"),
        "call_domain_areas": resolved.get("program"),
        "focus_theme": _none(focus_theme),
        "opportunity_link": _none(link),
        "applicant_role": _none(role),
        "lead_applicant": _none(lead_applicant),
        "sub_applicant": _none(sub_applicant),
        "funding_window": _none(window),
        "call_submission_deadline": deadline.isoformat() if isinstance(deadline, date) else None,
        "expected_award_date": award_date.isoformat() if isinstance(award_date, date) else None,
        "time_to_award": _none(tta),
        "call_award_value": float(value) if value else None,
        "currency": _none(currency),
        "project_duration": int(duration) if duration else None,
        "submission_format": resolved.get("sub_format"),
        "feasibility": _none(feas),
        **vals,
        "decline_flags_present": decline_bool,
        "key_risks": _none(key_risks),
        "alignment_score": align,
        "auto_recommendation": rec,
        "decision": _none(decision),
        "decision_note": _none(rationale),
        "proposal_lead": resolved.get("prop_lead"),
        "contributors": resolved.get("contributors"),
        "reviewers": resolved.get("reviewers"),
        "review_week": review_week_label(),
    }

    # NO duplicate check here per the new policy. Insert immediately so
    # the submitter never loses their entry. Dedup is enforced at the
    # DISPLAY layer (core/deduplicator.py is run by Tracking + Report
    # views to suppress double-counting in KPIs).
    try:
        sb.table("rfp_submissions").insert(row).execute()
        # Tombstone in the permanent seen-ledger so this opportunity is
        # remembered (and never silently re-scanned in) even if later deleted.
        try:
            from core import seen_ledger
            seen_ledger.record_one(row, reason="manual")
        except Exception:
            pass
    except Exception as exc:
        st.error(f"Submit failed: {exc}")
        return

    if on_success is not None:
        on_success(row)
        return

    # Default inline success view (used by the standalone page).
    st.success(
        f"✅ Submitted as **{uid}** — alignment score {float(align):.1f}/100, "
        f"auto-recommendation **{rec}**."
    )
    st.caption(
        "Captured. Duplicate-detection runs at display time, so if this "
        "matches an existing record the dashboards will merge them "
        "automatically — no action needed from you."
    )
