"""Shared full-RFP editor dialog.

A normal importable module (NOT run via core.render_view). It exposes a single
`@st.dialog` — `render_rfp_editor(row, *, sb, user, is_admin)` — whose body is the
full 5-tab "Edit RFP" editor extracted from views/rfp_records.py so the Records,
Tracking, and Review screens all edit the SAME `rfp_submissions` row (keyed by
`uid`) through the tenant-scoped `get_client()` passed in as `sb`. Edits sync
automatically because every caller writes the same row.

The only difference from the original inline dialog is that `sb`, `user`, and
`is_admin` now arrive as function parameters instead of module globals. Callers
are expected to gate the button that opens this dialog to editors.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import streamlit as st

from core import dropdowns
from core.scorer import (score_submission, CRITERION_RESPONSES, default_response)


@st.dialog("Edit RFP", width="large")
def render_rfp_editor(row: dict, *, sb, user, is_admin: bool = False) -> None:
    st.markdown(f"**`{row['uid']}`** — {row.get('opportunity_title') if isinstance(row.get('opportunity_title'), str) else ''}")
    # Provenance line — who submitted this and when, so an editor can reach out.
    # Guard on type: a blank cell arrives as NaN (a float), which `or ""` won't
    # catch (NaN is truthy) and .strip() then breaks (AttributeError on float).
    _sb = row.get("submitted_by")
    _se = row.get("submitted_by_email")
    _sub_by = (_sb if isinstance(_sb, str) else "").strip() or "—"
    _sub_email = (_se if isinstance(_se, str) else "").strip()
    _sd = pd.to_datetime(row.get("search_date"), errors="coerce")
    _sd_str = _sd.strftime("%d %b %Y, %H:%M") if pd.notna(_sd) else "date unknown"
    _who = f"**{_sub_by}**" + (f" · {_sub_email}" if _sub_email else "")
    st.caption(f"📥 Submitted by {_who} · on {_sd_str}")
    tab_opp, tab_elig, tab_dec, tab_team, tab_award = st.tabs(
        ["Opportunity", "Eligibility", "Decision & Pipeline", "Team", "Award"]
    )

    def _date(v):
        if v is None or v == "" or (not isinstance(v, str) and pd.isna(v)):
            return None
        try:
            ts = pd.to_datetime(v, errors="coerce")
            if pd.isna(ts):
                return None
            return ts.date()
        except Exception:
            return None

    def _is_blank(v) -> bool:
        if v is None:
            return True
        if isinstance(v, str):
            return v == ""
        try:
            return bool(pd.isna(v))
        except (TypeError, ValueError):
            return False

    def _str(v) -> str:
        """Return '' for None / NaN / NaT so widgets don't render 'nan'."""
        return "" if _is_blank(v) else str(v)

    def _num(v) -> float:
        try:
            f = float(v)
            return 0.0 if pd.isna(f) else f
        except (TypeError, ValueError):
            return 0.0

    def _opt(label, key, options, current):
        opts = ["—"] + list(options)
        if _is_blank(current):
            current = None
        elif current not in opts:
            opts.append(current)
        idx = opts.index(current) if current in opts else 0
        return st.selectbox(label, opts, index=idx, key=f"edit_{key}_{row['uid']}")

    def _multi_options(predefined: list, current):
        """Merge predefined options with any stored values not yet in the list."""
        cur = list(current) if current else []
        extras = [v for v in cur if v not in predefined]
        return list(predefined) + extras

    def _multi_default(current):
        return list(current) if current else []

    def _val(v):
        return None if v in ("—", "", None) else v

    with tab_opp:
        c1, c2 = st.columns([2, 1])
        title_in = c1.text_input("Title *", value=_str(row.get("opportunity_title")), key=f"e_title_{row['uid']}")
        oppid_in = c2.text_input("Opportunity ID", value=_str(row.get("opportunity_id")), key=f"e_oid_{row['uid']}")
        funder_in = st.text_input("Funder *", value=_str(row.get("funding_agency")), key=f"e_fund_{row['uid']}")
        brief_in = st.text_area("Brief description", value=_str(row.get("brief_description")), height=110, key=f"e_brief_{row['uid']}")
        c3, c4, c5 = st.columns(3)
        dp = c3.date_input("Date posted", value=_date(row.get("date_posted")), key=f"e_dp_{row['uid']}")
        dl = c4.date_input("Submission deadline", value=_date(row.get("call_submission_deadline")), key=f"e_dl_{row['uid']}")
        ad = c5.date_input("Expected award", value=_date(row.get("expected_award_date")), key=f"e_ad_{row['uid']}")
        c6, c7, c8, c9 = st.columns(4)
        with c6:
            role_in = _opt("Applicant role", "role", dropdowns.get("applicant_role"), row.get("applicant_role"))
        with c7:
            win_in = _opt("Funding window", "win", dropdowns.get("funding_window"), row.get("funding_window"))
        with c8:
            tta_in = _opt("Time to award", "tta", dropdowns.get("time_to_award"), row.get("time_to_award"))
        with c9:
            fmt_in = _opt("Submission format", "fmt", dropdowns.get("submission_format"), row.get("submission_format"))
        c10, c11, c12 = st.columns([2, 1, 1])
        link_in = c10.text_input("Opportunity link", value=_str(row.get("opportunity_link")), key=f"e_link_{row['uid']}")
        val_in = c11.number_input("Estimated value", min_value=0.0, step=10000.0,
                                  value=_num(row.get("call_award_value")), key=f"e_val_{row['uid']}")
        cur_options = [c["code"] for c in dropdowns.load().get("currencies", [])]
        with c12:
            cur_in = _opt("Currency", "cur", cur_options, row.get("currency"))
        c13, c14 = st.columns(2)
        dur_in = c13.number_input("Duration (months)", min_value=0, step=1,
                                  value=int(_num(row.get("project_duration"))), key=f"e_dur_{row['uid']}")
        focus_in = c14.text_input("Focus theme", value=_str(row.get("focus_theme")), key=f"e_focus_{row['uid']}")
        geo_in = st.multiselect(
            "Geographic scope",
            _multi_options(dropdowns.get("call_geographic_scope"), row.get("call_geographic_scope")),
            default=_multi_default(row.get("call_geographic_scope")),
            key=f"e_geo_{row['uid']}",
        )
        prog_in = st.multiselect(
            "Program area(s)",
            _multi_options(dropdowns.get("call_domain_areas"), row.get("call_domain_areas")),
            default=_multi_default(row.get("call_domain_areas")),
            key=f"e_prog_{row['uid']}",
        )

    def _coerce_elig_edit(v) -> str:
        try:
            if v is None or pd.isna(v):
                return "Partial"
        except (TypeError, ValueError):
            pass
        s = str(v).strip().lower()
        if s in ("yes", "y", "true", "1"):
            return "True"
        if s in ("partial", "p"):
            return "Partial"
        if s in ("no", "n", "false", "0"):
            return "False"
        return "Partial"

    def _elig(label, key, criterion, current):
        # Per-criterion RICH responses — the SAME single source the Submit form
        # uses (core.scorer.CRITERION_RESPONSES), so Edit RFP matches it exactly.
        # default_response maps any stored value (legacy True/Partial/False OR a
        # rich label) to the matching option, so existing rows pre-select correctly.
        opts = CRITERION_RESPONSES.get(criterion) or list(dropdowns.get("eligibility_values"))
        default = (default_response(criterion, current)
                   if criterion in CRITERION_RESPONSES else _coerce_elig_edit(current))
        idx = opts.index(default) if default in opts else 0
        return st.selectbox(label, opts, index=idx, key=f"edit_{key}_{row['uid']}")

    with tab_elig:
        fcol, _spacer = st.columns([1, 3])
        with fcol:
            feas_in = _opt("Feasibility", "feas", dropdowns.get("feasibility"), row.get("feasibility"))
        gl, gr = st.columns(2)
        with gl:
            m1 = _elig("MUST 1 — Legal status & qualification", "m1", "qualification", row.get("qualification"))
            m2 = _elig("MUST 2 — Strategic fit", "m2", "strategic_fit", row.get("strategic_fit"))
            m3 = _elig("MUST 3 — Implementation capacity", "m3", "capacity", row.get("capacity"))
            m4 = _elig("MUST 4 — Geographic fit", "m4", "geographic_fit", row.get("geographic_fit"))
            m5 = _elig("MUST 5 — Cofinancing & compliance", "m5", "cofinancing", row.get("cofinancing"))
        with gr:
            p6 = _elig("PREFER 6 — Funding quality", "p6", "funding_quality", row.get("funding_quality"))
            p7 = _elig("PREFER 7 — Donor relationship", "p7", "funder_relationship", row.get("funder_relationship"))
            p8 = _elig("PREFER 8 — Competitiveness", "p8", "competitiveness", row.get("competitiveness"))
            p9 = _elig("PREFER 9 — Bid effort", "p9", "bid_effort", row.get("bid_effort"))
        decline_in = st.radio(
            "Decline flags present?", ["No", "Yes"], horizontal=True,
            index=1 if row.get("decline_flags_present") else 0, key=f"e_decline_{row['uid']}",
        )
        risks_in = st.text_area("Key risks", value=_str(row.get("key_risks")), height=90, key=f"e_risks_{row['uid']}")
        st.caption(
            f"Stored alignment score: **{_num(row.get('alignment_score')):.1f}** · "
            f"Auto-decision: **{_str(row.get('auto_recommendation')) or '—'}**. "
            "Save will recompute these from the values above."
        )

    with tab_dec:
        c1, c2 = st.columns([1, 3])
        with c1:
            dec_in = _opt("Decision", "dec", dropdowns.get("decisions"), row.get("decision"))
        rat_in = c2.text_area("Rationale", value=_str(row.get("decision_note")), height=70, key=f"e_rat_{row['uid']}")
        c_sub, c_stage, c_prog = st.columns([1, 1, 1])
        submissions_in = c_sub.number_input(
            "Submissions",
            min_value=0, step=1,
            value=int(_num(row.get("submissions")) or 0),
            key=f"e_subs_{row['uid']}",
            help="How many times this RFP was actually submitted to the donor. "
                 "0 until submitted; only a Completed (submitted) RFP should read 1+.",
        )
        with c_stage:
            stage_in = _opt("Stage", "stage", dropdowns.get("stages"), row.get("stage"))
        with c_prog:
            prog_status = _opt("Progress status", "ps", dropdowns.get("progress_status"), row.get("progress_status"))
        c5, c6 = st.columns(2)
        with c5:
            donor_dec = _opt("Donor decision", "dd", dropdowns.get("donor_decision"), row.get("donor_decision"))
        assigned = c6.text_input("Assigned to", value=_str(row.get("assigned_to")), key=f"e_assn_{row['uid']}")
        c7, c8 = st.columns(2)
        action_dl = c7.date_input("Action deadline", value=_date(row.get("action_deadline")), key=f"e_actdl_{row['uid']}")
        last_upd = c8.date_input("Last update", value=_date(row.get("last_update")), key=f"e_lu_{row['uid']}")
        next_a = st.text_input("Next action", value=_str(row.get("next_action")), key=f"e_na_{row['uid']}")
        remarks_in = st.text_area("Remarks", value=_str(row.get("remarks")), height=70, key=f"e_rem_{row['uid']}")

    with tab_team:
        team = list(dropdowns.get("team_members"))
        base_team = [m for m in team if m not in ("Other", "All")]
        # Names typed via "Other" → added to the roster on Save.
        _new_members: list[str] = []

        def _team_single(label, key, current):
            opts = ["—"] + base_team + ["Other"]
            cur = None if _is_blank(current) else current
            if cur and cur not in opts:
                opts.insert(1, cur)        # preserve a stored name off the roster
            sel = st.selectbox(label, opts,
                               index=opts.index(cur) if cur in opts else 0,
                               key=f"e_{key}_{row['uid']}")
            if sel == "Other":
                spec = (st.text_input(
                    f"↳ If other member, please specify ({label.lower()})",
                    key=f"e_{key}_oth_{row['uid']}") or "").strip()
                if spec:
                    _new_members.append(spec)
                return spec or None
            return None if sel in ("—", "") else sel

        def _team_multi(label, key, current, help=None):
            cur = (list(current) if isinstance(current, (list, tuple))
                   else [v.strip() for v in str(current).split(",") if v.strip()]
                   if current else [])
            extras = [v for v in cur if v not in base_team and v not in ("All", "Other")]
            opts = ["All"] + base_team + extras + ["Other"]
            sel = st.multiselect(label, opts,
                                 default=[d for d in cur if d in opts],
                                 key=f"e_{key}_{row['uid']}", help=help)
            chosen = [s for s in sel if s not in ("All", "Other")]
            if "All" in sel:                # "All" = the whole roster
                chosen = list(base_team)
            if "Other" in sel:
                raw = st.text_input(
                    f"↳ If other, please specify additional {label.lower()} "
                    f"(comma-separated)", key=f"e_{key}_oth_{row['uid']}") or ""
                typed = [v.strip() for v in raw.split(",") if v.strip()]
                _new_members.extend(typed)
                chosen = chosen + typed
            return chosen or None

        lead = _team_single("Proposal lead", "lead", row.get("proposal_lead"))
        contribs = _team_multi("Contributors", "contrib", row.get("contributors"))
        reviewers = _team_multi("Reviewers", "rev", row.get("reviewers"))
        support = _team_multi(
            "Support", "supp", row.get("support_roles"),
            help="e.g. tech / finance / compliance")

    with tab_award:
        c1, c2 = st.columns(2)
        doa = c1.date_input("Date of approval", value=_date(row.get("date_of_approval")), key=f"e_doa_{row['uid']}")
        secured = c2.number_input("Amount secured", min_value=0.0, step=10000.0,
                                  value=_num(row.get("amount_secured")), key=f"e_sec_{row['uid']}")
        c3, c4 = st.columns(2)
        with c3:
            cur_sec = _opt(
                "Currency secured", "cursec",
                [c["code"] for c in dropdowns.load().get("currencies", [])],
                row.get("currency_secured"),
            )
        po = c4.text_input("Donor program officer", value=_str(row.get("donor_program_officer")), key=f"e_po_{row['uid']}")
        c5, c6 = st.columns(2)
        ko = c5.date_input("Kick-off date", value=_date(row.get("kickoff_date")), key=f"e_ko_{row['uid']}")
        ns = c6.text_input("Next step", value=_str(row.get("next_step")), key=f"e_ns_{row['uid']}")

    st.divider()
    bs, bd, bc = st.columns([1, 1, 1])
    save_pressed = bs.button("💾 Save changes", type="primary", width='stretch')
    delete_pressed = bd.button("🗑 Delete this RFP", width='stretch', disabled=not is_admin)
    cancel_pressed = bc.button("Cancel", width='stretch')

    if cancel_pressed:
        st.rerun()

    if delete_pressed:
        sb.table("rfp_submissions").delete().eq("uid", row["uid"]).execute()
        st.cache_data.clear()
        st.toast(f"Deleted {row['uid']}", icon="🗑")
        st.rerun()

    if save_pressed:
        if not title_in.strip() or not funder_in.strip():
            st.error("Title and Funder are required.")
            return
        # Grow the team roster with any names typed via "Other".
        if _new_members:
            try:
                from core import settings as _set
                _set.set_team_members((_set.get_team_members() or []) + _new_members)
            except Exception:
                pass
        # Eligibility values always come back as True / Partial / False (no "—")
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
        decline_bool = decline_in == "Yes"
        align, rec = score_submission(vals, decline_bool)
        update = {
            "opportunity_title": title_in.strip(),
            "opportunity_id": _val(oppid_in.strip()),
            "funding_agency": funder_in.strip(),
            "brief_description": _val(brief_in),
            "date_posted": dp.isoformat() if isinstance(dp, date) else None,
            "call_submission_deadline": dl.isoformat() if isinstance(dl, date) else None,
            "expected_award_date": ad.isoformat() if isinstance(ad, date) else None,
            "applicant_role": _val(role_in),
            "funding_window": _val(win_in),
            "time_to_award": _val(tta_in),
            "submission_format": _val(fmt_in),
            "opportunity_link": _val(link_in),
            "call_award_value": float(val_in) if val_in else None,
            "currency": _val(cur_in),
            "project_duration": int(dur_in) if dur_in else None,
            "focus_theme": _val(focus_in),
            "call_geographic_scope": geo_in or None,
            "call_domain_areas": prog_in or None,
            "feasibility": _val(feas_in),
            **vals,
            "decline_flags_present": decline_bool,
            "key_risks": _val(risks_in),
            "alignment_score": align,
            "auto_recommendation": rec,
            "submissions": int(submissions_in) if submissions_in else 1,
            "decision": _val(dec_in),
            "decision_note": _val(rat_in),
            "stage": _val(stage_in),
            "progress_status": _val(prog_status),
            "donor_decision": _val(donor_dec),
            "assigned_to": _val(assigned),
            "action_deadline": action_dl.isoformat() if isinstance(action_dl, date) else None,
            "last_update": last_upd.isoformat() if isinstance(last_upd, date) else None,
            "next_action": _val(next_a),
            "remarks": _val(remarks_in),
            "proposal_lead": lead,
            "contributors": contribs,
            "reviewers": reviewers,
            "support_roles": (", ".join(support) if support else None),
            "date_of_approval": doa.isoformat() if isinstance(doa, date) else None,
            "amount_secured": float(secured) if secured else None,
            "currency_secured": _val(cur_sec),
            "donor_program_officer": _val(po),
            "kickoff_date": ko.isoformat() if isinstance(ko, date) else None,
            "next_step": _val(ns),
            "decision_overridden_by": user.get("email"),
            "decision_overridden_at": datetime.now(timezone.utc).isoformat(),
        }
        # Invariant: Progress = "Completed" means the proposal was SUBMITTED to the donor,
        # so a donor decision is now pending. If the user marked Completed but left the
        # donor decision blank/Not submitted, default it to "Under Review" — otherwise the
        # row leaves Tracking (Completed) yet never enters Active Grants (which keys off
        # donor_decision), the exact gap that hid the lead-poisoning grant.
        if str(update.get("progress_status") or "").strip().lower() == "completed" \
                and str(update.get("donor_decision") or "").strip().lower() in ("", "not submitted"):
            update["donor_decision"] = "Under Review"
        sb.table("rfp_submissions").update(update).eq("uid", row["uid"]).execute()
        # ML Phase 1/3 — log the human decision as a labeled signal on save.
        # Captures CONFIRMATIONS (reviewer kept the recommended decision) as
        # well as overrides — logging only changes would bias the model toward
        # disagreement. log_decision dedups per record so repeated saves of the
        # same decision don't pile up.
        _new_dec = _val(dec_in)
        if _new_dec:
            try:
                from core import decision_log
                decision_log.log_decision({**row, **update}, _new_dec,
                                          by=user.get("email"))
            except Exception:
                pass
        st.cache_data.clear()
        st.toast(f"Saved {row['uid']} · score {align:.1f} → {rec}", icon="✅")
        st.rerun()
