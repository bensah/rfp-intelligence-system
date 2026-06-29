"""Shared 'Organization Details & Preferences' editor.

Extracted verbatim from app_pages/admin.py (2026-06-19) so the Settings page AND
the Organization Details edit overlay render the IDENTICAL four-tab form
(Profile / Bid Fitness / Team Members / Scan Preferences) from ONE source.
Call render_org_setup(user, sb) inside any container (a page tab or an st.dialog).
"""
from __future__ import annotations

import subprocess  # noqa: F401
import sys  # noqa: F401
from datetime import date, datetime, timezone  # noqa: F401
from io import StringIO  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

import pandas as pd  # noqa: F401
import streamlit as st

from core import excel_sync, settings  # noqa: F401
from core import permissions  # noqa: F401
from core.criteria_derive import ROUTE_OPTIONS as _ROUTE_OPTIONS
from db.supabase_client import get_client, safe_execute  # noqa: F401


def render_org_setup(user, sb):
    """Render the full Organization Details & Preferences editor (4 tabs)."""
    st.subheader("Organization Details & Preferences")
    st.caption("Org profile, bid-fitness inputs and team — used across the app and the matching engine.")
    _ptab, _ftab, _ttab, _stab = st.tabs(["Profile", "Bid Fitness", "Team Members", "Scan Preferences"])
    with _ptab:
        st.subheader("Organization")
        st.caption(
            "RFPIS ships as a multi-tenant product — the deploying-org profile "
            "below stamps every page header, the Report dashboard, and outgoing "
            "email digests. Defaults are intentional placeholders — fill these "
            "in the first time you set up the app for your organization."
        )

        _org = settings.get_org()
        oc1, oc2 = st.columns(2)
        org_name = oc1.text_input(
            "Organization name", value=_org.get("org_name", ""),
            help="Full name shown in page captions and the Report header. "
                 "e.g. 'Acme Foundation — Business Development Team'.",
        )
        org_short = oc2.text_input(
            "Short name / abbreviation", value=_org.get("org_short", ""),
            help="Compact label used in page titles, grant lead/sub defaults, "
                 "and scan-log displays. e.g. 'Acme BD'.",
        )
        oc3, oc4 = st.columns(2)
        org_country = oc3.text_input(
            "Primary country", value=_org.get("org_country", ""),
            help="Country the deploying org operates from. Used in the Report "
                 "geographic context. e.g. your primary country of operation.",
        )
        org_team = oc4.text_input(
            "Team / department", value=_org.get("org_team", ""),
            help="The team within the org running the screening. e.g. "
                 "'Business Development Team'.",
        )
        oc5, oc6 = st.columns(2)
        org_email = oc5.text_input(
            "Contact email", value=_org.get("org_contact_email", ""),
            help="Distribution list for digest emails + the From / Reply-To "
                 "address on outgoing notifications.",
        )
        org_website = oc6.text_input(
            "Website (optional)", value=_org.get("org_website", ""),
            help="Public URL — surfaces in the Report footer and exported PDFs.",
        )
        # ---- Eligibility gates used by the scan classifier ------------------
        st.markdown("**Eligibility gates** — drive the scanner's hard screens and "
                    "the donor-intelligence compliance check.")
        eg1, eg2 = st.columns(2)
        us_entity = eg1.checkbox(
            "We are a US-based entity",
            value=str(_org.get("org_is_us_entity", "false")).lower() == "true",
            help="When unchecked (a non-US deployment), the scanner rejects "
                 "US-domestic-only RFPs (e.g. 'open to US-based applicants only'). "
                 "Check this for a US-based organization.",
        )
        _board_opts = {
            "Unknown — don't apply this gate": "",
            "Yes — we have a local board": "yes",
            "No — we don't have one": "no",
        }
        _board_cur = str(_org.get("org_has_local_board", "") or "").lower()
        _board_labels = list(_board_opts)
        _board_idx = next((i for i, v in enumerate(_board_opts.values()) if v == _board_cur), 0)
        local_board = eg2.selectbox(
            "Locally-constituted Board of Directors?",
            _board_labels, index=_board_idx,
            help="If 'No', donors whose intel requires a local board become a hard "
                 "MUST-4 disqualifier during scoring. 'Unknown' leaves the gate off.",
        )
        bd_team = st.checkbox(
            "We have a Business Development / Fundraising / Resource Mobilization team",
            value=str(_org.get("org_has_bd_team", "false")).lower() == "true",
            key="org_has_bd_team",
            help="Feeds the Bid-effort feasibility score (PREFER 9).",
        )
        # ---- Logo: file uploader stored as base64 in app_settings -----------
        # The file is encoded inline into the settings table — no filesystem
        # dependency, so it survives Streamlit Cloud container restarts (where
        # local uploads would be wiped). Legacy `org_logo_url` is still
        # respected as a fallback by get_org_logo() / the Report header for any
        # install that pasted a hosted URL before this changed.
        st.markdown("**Logo** (optional) — uploaded to the app and persisted "
                    "in the settings table. Renders in the Report header.")
        current_logo_bytes, _current_logo_mime = settings.get_org_logo()
        lcol_preview, lcol_uploader, lcol_clear = st.columns([1, 3, 1])
        with lcol_preview:
            if current_logo_bytes:
                st.image(current_logo_bytes, width=110, caption="Current logo")
            else:
                st.caption("_No logo uploaded yet._")
        with lcol_uploader:
            new_logo_file = st.file_uploader(
                "Upload a new logo (PNG / JPG / WebP / GIF / SVG, ≤2 MB recommended)",
                type=["png", "jpg", "jpeg", "webp", "gif", "svg"],
                key="org_logo_uploader",
                help="The file is encoded and stored in the app_settings table. "
                     "Use a transparent-background PNG ≤200px tall for the "
                     "cleanest Report header.",
            )
        with lcol_clear:
            # Vertical spacer so the button aligns with the uploader, not its label.
            st.markdown("<div style='height:1.85rem'></div>", unsafe_allow_html=True)
            if current_logo_bytes and st.button(
                "🗑 Remove", key="org_logo_remove",
                help="Delete the stored logo. Falls back to no-logo until you upload another.",
            ):
                settings.clear_org_logo(updated_by=user.get("email"))
                st.success("Logo removed.")
                st.rerun()

        if st.button("💾 Save organization profile", type="primary",
                     key="save_org_profile"):
            settings.set_org({
                "org_name":          org_name.strip(),
                "org_short":         org_short.strip(),
                "org_country":       org_country.strip(),
                "org_team":          org_team.strip(),
                "org_contact_email": org_email.strip(),
                "org_website":       org_website.strip(),
                "org_is_us_entity":  "true" if us_entity else "false",
                "org_has_local_board": _board_opts[local_board],
                "org_has_bd_team":   "true" if bd_team else "false",
            }, updated_by=user.get("email"))
            # Save the uploaded logo (if any) alongside the text fields so a
            # single button click captures everything.
            if new_logo_file is not None:
                file_bytes = new_logo_file.read()
                if len(file_bytes) > 5 * 1024 * 1024:
                    st.warning(
                        f"⚠ Logo is {len(file_bytes) / 1024 / 1024:.1f} MB — "
                        "consider downsizing. Stored anyway."
                    )
                settings.set_org_logo(
                    file_bytes,
                    new_logo_file.type or "image/png",
                    updated_by=user.get("email"),
                )
            st.success("Organization profile saved.")
            st.rerun()

    with _ftab:
        st.subheader("Organization fit profile — bid/no-bid matching")
        st.caption(
            "The structured record of WHO you are, matched against each RFP's "
            "requirements to answer the eligibility questions objectively — and to "
            "make the decision model per-organization. Fill once; update as the org "
            "changes. (Identity/branding lives in the section above.)"
        )
        from core import org_profile as _orgp
        from core import geographies as _geo
        from core.program_area_select import program_area_matrix_editor
        from core.partners import NONPROFIT_PARTNERS, DONOR_PORTALS, clean_portal_url
        _prof = _orgp.get_profile()

        # Controlled vocabularies — SAME lists the Donor Intelligence profiles use,
        # so org values match donor values directly (no fuzzy mapping):
        #   geography      -> geographies.GEO_OPTIONS  (donor donor_geographic_scope)
        #   program areas  -> program_area_classifier  (donor priority_program_areas)
        #   funders/portal -> the donor_intel catalog (donor + website)
        #   non-profit partners -> core.partners.NONPROFIT_PARTNERS
        _LANGS = ["English", "French", "Portuguese", "Spanish", "Arabic", "Swahili"]
        _donor_names = []
        _portals = sorted({p for p in map(clean_portal_url, DONOR_PORTALS) if p})
        try:
            _dn = sb.table("donor_intel").select("donor, website").execute().data or []
            _donor_names = sorted({(d.get("donor") or "").strip()
                                   for d in _dn if (d.get("donor") or "").strip()})
            # Clean every seed + catalog website to a bare host, then de-duplicate.
            _portals = sorted({p for p in (
                set(map(clean_portal_url, DONOR_PORTALS))
                | {clean_portal_url(d.get("website")) for d in _dn}
            ) if p and "." in p})
        except Exception:
            pass

        def _ms(container, label, options, key, *, help=""):
            """Donor-aligned multi-select. Pre-selects stored values; accept_new_options
            lets the team add anything outside the controlled list."""
            cur = [str(x) for x in (_prof.get(key) or [])]
            opts = sorted(set(str(o) for o in options) | set(cur))
            return container.multiselect(label, opts, default=cur,
                                         accept_new_options=True,
                                         key=f"orgp_{key}", help=help)

        fp1, fp2 = st.columns(2)
        _legal_opts = ["nonprofit", "government", "higher_ed", "for_profit",
                       "individual", "tribal"]
        _legal_cur = _prof.get("legal_type", "nonprofit")
        legal_type = fp1.selectbox(
            "Legal type", _legal_opts,
            index=_legal_opts.index(_legal_cur) if _legal_cur in _legal_opts else 0,
            format_func=_orgp.legal_type_label,
            help="Applicant category — matched against each call's eligible-applicant "
                 "rules (qualification, MUST-1 item A).")
        # Entity type (MUST-1 item B) sits beside Legal type — together they are the
        # "Eligibility type" component. Data-validation: Individual legal_type ⇒
        # entity_type forced to Individual; any other legal_type hides Individual.
        if legal_type == "individual":
            _entity_opts = ["individual"]
            _entity_cur = "individual"
        else:
            _entity_opts = ["", "grassroot_local", "multi_country"]
            _entity_cur = _prof.get("entity_type", "") or ""
            if _entity_cur not in _entity_opts:
                _entity_cur = ""
        entity_type = fp2.selectbox(
            "Entity type", _entity_opts,
            index=_entity_opts.index(_entity_cur),
            format_func=_orgp.entity_type_label,
            help="Registration/entity category — matched when a call requires a "
                 "grassroots/local vs multi-country vs individual applicant "
                 "(qualification, MUST-1 item B). Replaces the old grassroots / "
                 "multi-country checkboxes; it also feeds competitiveness.")
        fp3, fp4 = st.columns(2)
        founding_year = fp3.number_input(
            "Founding year", min_value=1800, max_value=2100,
            value=int(_prof["founding_year"]) if _prof.get("founding_year") else 2000,
            step=1, help="Track-record length (strategic fit).")
        _cofin_opts = list(_orgp.COFINANCING_LEVELS)
        _cofin_cur = _prof.get("cofinancing_capacity", "limited")
        cofin = fp4.selectbox(
            "Co-financing capacity", _cofin_opts,
            index=_cofin_opts.index(_cofin_cur) if _cofin_cur in _cofin_opts else 1,
            help="Can you meet match / cost-share requirements? (cofinancing).")

        # Capacity inputs (MUST 3). Labels spell out the DISTINCT meanings — the
        # earlier terse labels let "annual budget" read as "max grant", which
        # mis-set the capacity bar. These four feed the multi-factorial capacity
        # model (annual throughput + biggest grant + range + track-record depth,
        # stretched by founding year / org stage).
        fb1, fb2 = st.columns(2)
        annual_budget = fb1.number_input(
            "Annual budget managed (USD/yr, 0 = unset)", min_value=0, step=100000,
            value=int(_prof["annual_budget_usd"]) if _prof.get("annual_budget_usd") else 0,
            help="Total funds the org MANAGES/spends per YEAR (annual throughput) — "
                 "NOT one grant. A multi-year grant counts only the portion used "
                 "that year. Capacity (MUST 3).")
        largest_grant = fb2.number_input(
            "Largest SINGLE grant ever (USD, 0 = unset)", min_value=0, step=100000,
            value=int(_prof["largest_grant_usd"]) if _prof.get("largest_grant_usd") else 0,
            help="Biggest single grant received from ONE donor over its full life "
                 "(distinct from annual budget). Capacity (MUST 3).")
        fb3, fb4 = st.columns(2)
        lowest_grant = fb3.number_input(
            "Smallest grant managed (USD, 0 = unset)", min_value=0, step=10000,
            value=int(_prof["lowest_grant_usd"]) if _prof.get("lowest_grant_usd") else 0,
            help="Smallest grant the org has run — range awareness for capacity.")
        n_grants = fb4.number_input(
            "Number of grants managed (0 = unset)", min_value=0, step=1,
            value=int(_prof["number_of_grants_managed"]) if _prof.get("number_of_grants_managed") else 0,
            help="How many grants delivered to date — track-record DEPTH; more "
                 "grants raises how far past your largest grant you can credibly stretch.")

        _FUNDING_HELP = ("Drives funding quality (PREFER 6) AND the capacity slice "
                         "for big pooled RFPs. Low = smallest award worth applying "
                         "for (skip anything below). Mid = ideal/sweet spot. Max = "
                         "top of the comfortable range (above it is still a strong "
                         "fit, but needs more resources to pursue). Leave 0 for "
                         "absolute defaults.")
        st.markdown("**Preferred award size (USD)**", help=_FUNDING_HELP)
        ft1, ft2, ft3 = st.columns(3)
        ftl = ft1.number_input("Target — low (won't apply below this)", min_value=0, step=50000,
            value=int(_prof["funding_target_low"]) if _prof.get("funding_target_low") else 0)
        ftm = ft2.number_input("Target — mid (sweet spot / ideal)", min_value=0, step=50000,
            value=int(_prof["funding_target_mid"]) if _prof.get("funding_target_mid") else 0)
        ftx = ft3.number_input("Target — max (top of comfort range)", min_value=0, step=50000,
            value=int(_prof["funding_target_max"]) if _prof.get("funding_target_max") else 0)

        st.markdown("**Eligibility facts**",
                    help="Matched to each donor's documented conditions for "
                         "qualification (MUST-1).")
        eq1, eq2, eq3, eq4 = st.columns(4)
        org_independent = eq1.checkbox(
            "Independent entity", value=bool(_prof.get("org_is_independent_entity", True)),
            key="orgp_independent",
            help="NOT a branch/affiliate of a larger INGO. Some funders exclude affiliates.")
        org_sam_uei = eq2.checkbox(
            "Holds SAM.gov / UEI", value=bool(_prof.get("org_has_sam_uei", False)),
            key="orgp_sam_uei")
        org_tax_exempt = eq3.checkbox(
            "Tax-exempt (501c3 / equiv.)", value=bool(_prof.get("org_tax_exempt", False)),
            key="orgp_tax_exempt")
        _stage_opts = ["established", "early-stage"]
        _stage_cur = _prof.get("org_stage", "established")
        org_stage = eq4.selectbox(
            "Org stage", _stage_opts,
            index=_stage_opts.index(_stage_cur) if _stage_cur in _stage_opts else 0,
            help="Some funders fund early-stage organisations only (e.g. DRK).")
        has_pi = st.checkbox(
            "Has a well-established PI (Principal Investigator)",
            value=bool(_prof.get("has_established_pi", False)),
            key="orgp_has_established_pi",
            help="Check if the org can field a well-established PI. Satisfies a call "
                 "that requires an individual / PI based in an in-scope country "
                 "(qualification, MUST-1 item E). A FOREIGN-PI requirement is instead "
                 "met via an affiliated partner (type + status + country) in the "
                 "partners table below.")

        st.markdown("**Compliance credentials**",
                    help="Hard pre-acquire gates the org must ALREADY hold. Each is "
                         "matched to a donor requirement of the same name (cofinancing "
                         "& compliance, MUST-5): if a donor requires it and the box is "
                         "unchecked, that requirement scores 0. SAM.gov/UEI and "
                         "tax-exempt above are part of the same set.")
        cc1, cc2, cc3 = st.columns(3)
        has_audited_financials = cc1.checkbox(
            "Audited financials", value=bool(_prof.get("has_audited_financials", False)),
            key="orgp_has_audited_financials",
            help="Recent independently audited financial statements available.")
        has_audit_report = cc2.checkbox(
            "Audit report on file", value=bool(_prof.get("has_audit_report", False)),
            key="orgp_has_audit_report",
            help="A formal external audit report can be provided.")
        has_safeguarding_policy = cc3.checkbox(
            "Safeguarding / PSEA policy",
            value=bool(_prof.get("has_safeguarding_policy", False)),
            key="orgp_has_safeguarding_policy",
            help="A safeguarding / PSEA policy is in place.")
        cc4, cc5, cc6 = st.columns(3)
        has_partner_mou = cc4.checkbox(
            "Partner MOU(s)", value=bool(_prof.get("has_partner_mou", False)),
            key="orgp_has_partner_mou",
            help="Signed MOU(s) with implementing partner(s) in place.")
        has_govt_mou = cc5.checkbox(
            "Government MOU", value=bool(_prof.get("has_govt_mou", False)),
            key="orgp_has_govt_mou",
            help="Signed MOU with the host-government authority in place.")
        has_govt_endorsement = cc6.checkbox(
            "Govt endorsement letter",
            value=bool(_prof.get("has_govt_endorsement", False)),
            key="orgp_has_govt_endorsement",
            help="A host-government endorsement / support letter can be obtained "
                 "when a donor requires it.")
        # Authorized-signatory — NOT a yes/no: list the donors we've ALREADY obtained
        # an authorized-signatory sign-off from; a call that requires it scores 1 only
        # if its donor is in this list (e.g. Wellcome Trust).
        authorized_signatory_donors = _ms(
            st, "Authorized signatory obtained from (donors)", _donor_names,
            "authorized_signatory_donors",
            help="Donors you have already secured an authorized-signatory sign-off "
                 "from. Matched by name to a call that requires one (MUST-5).")
        # Funding routes the org can RECEIVE through — matched (≥1 overlap) to the
        # call/donor's offered routes; no overlap → that MUST-5 gate scores 0.
        _route_labels = [lbl for _, lbl in _ROUTE_OPTIONS]
        _tok2lbl = {tok: lbl for tok, lbl in _ROUTE_OPTIONS}
        _lbl2tok = {lbl: tok for tok, lbl in _ROUTE_OPTIONS}
        _cur_routes = [_tok2lbl.get(t, t) for t in (_prof.get("org_funding_routes") or [])]
        _sel_routes = st.multiselect(
            "Funding routes we can receive through", _route_labels,
            default=[l for l in _cur_routes if l in _route_labels],
            key="orgp_org_funding_routes",
            help="How the org can RECEIVE funds (grant, procurement, loan, as a "
                 "subrecipient/partner, government/CCM channel, direct). A call whose "
                 "only route(s) the org can't use scores 0 on the funding-route gate.")
        org_funding_routes = [_lbl2tok[l] for l in _sel_routes if l in _lbl2tok]

        # ── Geography & languages (moved up, right after the eligibility facts) ──
        geo1, geo2 = st.columns(2)
        registrations_sel = _ms(geo1, "Countries registered", _geo.COUNTRIES,
            "org_registered_countries",
            help="Legal-registration jurisdictions (qualification).")
        countries_op_sel = _ms(geo2, "Countries of operation", _geo.GEO_OPTIONS,
            "org_operating_countries",
            help="Where you operate directly — same geo vocabulary as donor scope "
                 "(geographic fit).")
        langs_sel = _ms(st, "Proposal languages", _LANGS, "proposal_languages",
            help="Languages you can write a competitive bid in (bid effort).")

        # ── Competitiveness (moved to right after Proposal languages) ────────
        st.markdown("**Competitiveness**",
                    help="Drives PREFER 8 against each donor's requirements (org age + "
                         "grassroots / board / co-financing / multi-country / HQ match).")
        st.caption("Grassroots/local vs multi-country is now set by **Entity type** "
                   "(top of this form) — one field drives both eligibility (MUST-1) "
                   "and competitiveness.")
        _hq_opts = ["(none)"] + list(_geo.COUNTRIES)
        _hq_cur = _org.get("org_hq_country") or "(none)"
        hq_country = st.selectbox(
            "HQ country", _hq_opts,
            index=_hq_opts.index(_hq_cur) if _hq_cur in _hq_opts else 0,
            key="orgp_hq_country",
            help="Matching the donor's HQ country boosts competitiveness. Distinct "
                 "from 'We are a US-based entity' in the Organization section above.")

        # TWO distinct, separately-graded matrices on the SAME shared taxonomy:
        #  • Domains / areas of expertise = TRACK RECORD (history of implementing) →
        #    feeds COMPETITIVENESS (how well-placed we are to win in that exact area).
        #  • Strategic priority areas = STRATEGY (where we want to grow, footprint or
        #    not) → feeds STRATEGIC FIT (MUST-2), matched to donor priorities.
        domains_sel, domain_ratings = program_area_matrix_editor(
            "Domains / areas of expertise (track record)",
            _prof.get("org_domain_expertise"), _prof.get("org_domain_ratings"), "orgp_domains",
            help="Where you have demonstrated experience — grade 0–5 how strong your "
                 "track record is (e.g. malaria 5 = many funded/ongoing projects; "
                 "health workforce 1 = a minor past project). Drives competitiveness.")
        priorities_sel, priority_ratings = program_area_matrix_editor(
            "Strategic priority areas (strategy)",
            _prof.get("org_priority_areas"), _prof.get("org_priority_ratings"),
            "orgp_priority_areas",
            help="Where your strategy says you want to work — even with no footprint "
                 "yet (e.g. nutrition 5 = a top priority you're pursuing). Drives "
                 "strategic fit (MUST-2), matched to each donor's graded priorities.")

        # ── Affiliated partners & collaborators — ONE table (name · type · country) ──
        st.markdown("**Affiliated Partners and Collaborators** (private, non-profit, "
                    "donors, etc.)",
                    help="Type + country power donor conditions that require a SPECIFIC "
                         "partner (e.g. NIHR → a UK academic institution); any partner "
                         "also counts toward geographic 'via a partner' fit.")
        import pandas as _pd
        _PARTNER_TYPES = ["Nonprofit / NGO", "Academic / research institutions",
                          "For-profit / private", "Government", "Multilateral / UN",
                          "Bilateral / development agency", "Philanthropy / foundation"]
        _STATUS_OPTS = ["Donor", "Implementing Partner", "Collaborator"]

        def _status_list(v):
            """Coerce a stored status value to a clean list of valid options."""
            if isinstance(v, (list, tuple)):
                return [str(x).strip() for x in v if str(x).strip() in _STATUS_OPTS]
            s = str(v or "").strip()
            return [p.strip() for p in s.split(",") if p.strip() in _STATUS_OPTS]

        def _pc(v):
            try:
                if v is None or _pd.isna(v):
                    return ""
            except (TypeError, ValueError):
                pass
            s = str(v).strip()
            return "" if s.lower() in ("nan", "none") else s

        # Seed the table from the structured `partners` PLUS the legacy flat lists
        # (one-time merge — each legacy partner becomes a row with its type filled;
        # country left blank to complete). On save, everything is stored in
        # `partners` and the flat lists are consolidated away.
        _seed = [dict(p) for p in (_prof.get("partners") or []) if isinstance(p, dict)]
        for _p in _seed:
            _p["status"] = _status_list(_p.get("status"))
        _seen = {_pc(p.get("name")).lower() for p in _seed}

        def _merge(names, ptype):
            for n in (names or []):
                nm = str(n).strip()
                if nm and nm.lower() not in _seen:
                    _seen.add(nm.lower())
                    _seed.append({"name": nm, "type": ptype, "status": [], "country": ""})

        _merge(_prof.get("trusted_partners"), "Nonprofit / NGO")
        _merge(_prof.get("trusted_for_profit_partners"), "For-profit / private")
        _merge(_prof.get("trusted_academic_institutions"), "Academic / research institutions")

        # Column ORDER here = display order: name · type · STATUS · country.
        _pbase = _pd.DataFrame(_seed, columns=["name", "type", "status", "country"])
        for _c in ("name", "type", "country"):
            _pbase[_c] = _pbase[_c].astype("string")
        _pbase["status"] = _pbase["status"].apply(lambda v: v if isinstance(v, list) else [])
        _ped = st.data_editor(
            _pbase, num_rows="dynamic", hide_index=True, width="stretch",
            key="orgp_partners_tbl",
            column_config={
                "name": st.column_config.TextColumn("Partner name", width="large"),
                "type": st.column_config.SelectboxColumn("Type", options=_PARTNER_TYPES, width="medium"),
                "status": st.column_config.MultiselectColumn(
                    "Status", options=_STATUS_OPTS, width="medium",
                    help="One or more roles this org plays for you — Donor (funds you), "
                         "Implementing Partner (delivers with you), Collaborator."),
                "country": st.column_config.SelectboxColumn("Country", options=list(_geo.COUNTRIES), width="medium"),
            })
        partners_struct = []
        for _r in _ped.to_dict("records"):
            _nm = _pc(_r.get("name"))
            if _nm:
                partners_struct.append({"name": _nm, "type": _pc(_r.get("type")),
                                        "status": _status_list(_r.get("status")),
                                        "country": _pc(_r.get("country"))})

        # Active Donors — current grantee relationships (MUST-1 item I). Same donor
        # vocabulary as the funder history below so values match donor intel directly.
        active_donors_sel = _ms(st, "Active Donors — donors currently funding us",
            _donor_names, "active_donors",
            help="Donors with an OPEN/active grant to the org right now. If a call bars "
                 "CURRENT grantees, listing the donor here disqualifies us "
                 "(qualification, MUST-1 item I). Past/closed grants go under "
                 "'Donors we've already won grants / awards from' below.")

        # ── Funders & donor registrations (registrations swapped to here) ────
        fr1, fr2 = st.columns(2)
        funders_sel = _ms(fr1, "Donors we've already won grants / awards from",
            _donor_names, "funder_history",
            help="Pick from the Donor Intelligence catalog (or type to add) — "
                 "past / current funders (funder relationship).")
        donor_regs_sel = _ms(fr2, "Donor portal registration active",
            _portals, "donor_registrations",
            help="Donor portals where your registration is active (e.g. Grants.gov, "
                 "SAM.gov, wellcome.org) — each one listed is true for that donor. "
                 "Pick or type to add (qualification).")

        if st.button("💾 Save fit profile", type="primary", key="save_org_fit_profile"):
            _orgp.set_profile({
                "legal_type": legal_type,
                "entity_type": entity_type,
                "founding_year": int(founding_year) or None,
                "cofinancing_capacity": cofin,
                "annual_budget_usd": int(annual_budget) or None,
                "largest_grant_usd": int(largest_grant) or None,
                "lowest_grant_usd": int(lowest_grant) or None,
                "number_of_grants_managed": int(n_grants) or None,
                "funding_target_low": int(ftl) or None,
                "funding_target_mid": int(ftm) or None,
                "funding_target_max": int(ftx) or None,
                "org_is_independent_entity": bool(org_independent),
                "org_has_sam_uei": bool(org_sam_uei),
                "org_tax_exempt": bool(org_tax_exempt),
                "org_stage": org_stage,
                "has_established_pi": bool(has_pi),
                "has_audited_financials": bool(has_audited_financials),
                "has_audit_report": bool(has_audit_report),
                "has_safeguarding_policy": bool(has_safeguarding_policy),
                "has_partner_mou": bool(has_partner_mou),
                "has_govt_mou": bool(has_govt_mou),
                "has_govt_endorsement": bool(has_govt_endorsement),
                "authorized_signatory_donors": authorized_signatory_donors,
                "org_funding_routes": org_funding_routes,
                "partners": partners_struct,
                "org_domain_expertise": domains_sel,
                "org_domain_ratings": domain_ratings,
                "org_priority_areas": priorities_sel,
                "org_priority_ratings": priority_ratings,
                "org_operating_countries": countries_op_sel,
                # Partners now live in the single `partners` table above. The flat
                # lists are kept (consolidated) only for back-compat readers:
                # trusted_partners = every partner name (geographic 'via a partner'
                # fit), and the for-profit/academic lists collapse into it.
                "trusted_partners": [p["name"] for p in partners_struct],
                "trusted_for_profit_partners": [],
                "trusted_academic_institutions": [],
                "org_registered_countries": registrations_sel,
                "donor_registrations": donor_regs_sel,
                "funder_history": funders_sel,
                "active_donors": active_donors_sel,
                "proposal_languages": langs_sel,
            }, updated_by=user.get("email"))
            # Competitiveness inputs live in org settings (partial upsert — set_org
            # only touches these keys, leaving branding fields alone). Grassroots /
            # multi-country are DERIVED from entity_type (single source of truth) so
            # the existing competitiveness reads and the Organization view keep working.
            settings.set_org({
                "org_is_grassroot": "true" if entity_type == "grassroot_local" else "false",
                "org_is_multi_country": "true" if entity_type == "multi_country" else "false",
                "org_hq_country": "" if hq_country == "(none)" else hq_country,
            }, updated_by=user.get("email"))
            st.success("Fit profile saved.")
            st.rerun()

    with _ttab:
        st.subheader("Team members")
        st.caption(
            "Your roster — one name per line. These replace the 'Team Member 1..N' "
            "placeholders in the Proposal lead / Contributors / Reviewers dropdowns "
            "(on both the Submit and Edit forms). Stored privately in app settings — "
            "never in the public repo. 'Other' is added automatically so a one-off "
            "name can still be typed in."
        )
        # Defensive: never let the optional team editor crash the whole Admin
        # panel (e.g. during a hot-reload where core.settings is momentarily stale).
        _current_members = (getattr(settings, "get_team_members", lambda: None)() or [])
        _members_text = st.text_area(
            "One team member per line",
            value="\n".join(_current_members),
            height=180, key="team_members_editor",
            placeholder="Jane Doe\nJohn Smith\nAmina Bello",
        )
        if st.button("💾 Save team members", key="save_team_members"):
            names = [ln.strip() for ln in _members_text.splitlines() if ln.strip()]
            settings.set_team_members(names, updated_by=user.get("email"))
            st.success(f"Saved {len(names)} team member(s).")
            st.rerun()

    with _stab:
        # ------------------------------------------------------------------
        # Eligibility policies (drives scan-time country/theme filter +
        # auto-scoring of the 9 MUST/PREFER criteria).
        # ------------------------------------------------------------------
        st.subheader("Scan Preferences - Eligibility & Auto-Scoring")
        st.caption(
            "**Stage 1 — Eligibility gate (applied during scan):** RFPs that don't "
            "match country or theme are NOT inserted. **Stage 2 — Auto-scoring "
            "(applied to inserted RFPs):** each of the 9 MUST/PREFER criteria gets "
            "an auto-assigned Yes/Partial/No based on keyword matching at the "
            "configured rigor level. Reviewers can override anything on the Review tab. "
            "This whole block is the configurable algorithm — change it to point "
            "the system at a different organisation's priorities."
        )
        from core import policies as _pol
        _live = _pol.get_policies()

        # Render any save/reset banner from the previous rerun.
        _pol_msg = st.session_state.pop("pol_save_msg", None)
        if _pol_msg:
            st.success(_pol_msg)

        # --- Tag-chip widget helper ---------------------------------------------
        # Wraps st.multiselect with accept_new_options where supported (Streamlit
        # 1.41+). On older versions we fall back to a plain multiselect — the
        # user can still pick from the option pool, just not type new values.
        def _tag_input(label: str, values: list[str], *, options: list[str] | None = None,
                        key: str, help: str | None = None):
            opts = list(options or [])
            # Make sure every currently-selected value is in the options list,
            # otherwise multiselect will silently drop it.
            for v in values:
                if v not in opts:
                    opts.append(v)
            try:
                return st.multiselect(
                    label, options=opts, default=list(values),
                    accept_new_options=True, key=key, help=help,
                )
            except TypeError:
                # Older Streamlit without accept_new_options — degrade to plain.
                return st.multiselect(
                    label, options=opts, default=list(values), key=key, help=help,
                )

        pol_tabs = st.tabs(["Countries", "Themes", "Criteria", "Search terms", "Currency", "JSON (advanced)"])

        with pol_tabs[0]:
            from core import geographies as _geo
            # BROAD_GEOGRAPHIES is newer than UN_REGIONS/INCOME_TIERS; fall back to
            # composing it so a stale cached module (Streamlit Cloud before a reboot)
            # can't hard-crash the Settings page.
            _broad_opts = getattr(_geo, "BROAD_GEOGRAPHIES", None) or (
                list(getattr(_geo, "UN_REGIONS", [])) + list(getattr(_geo, "INCOME_TIERS", [])))
            _tag_input(
                "Eligible countries",
                list(_live["countries"]["eligible"]),
                options=list(getattr(_geo, "COUNTRIES", [])),
                key="pol_countries_eligible",
                help="Exact countries the org works in — an RFP naming any of these "
                     "is admitted.",
            )
            _tag_input(
                "Broad-geography terms",
                list(_live["countries"]["broad_terms"]),
                options=list(_broad_opts),
                key="pol_countries_broad",
                help="High-level UN regions / income tiers. Each also admits its "
                     "member countries + synonyms (e.g. Sub-Saharan Africa admits a "
                     "call naming Kenya). Leave EMPTY for strict country-only matching.",
            )
            permissive = st.checkbox(
                "Permissive when geography unmentioned (recommended ON)",
                value=bool(_live["countries"].get("permissive_when_silent", True)),
                key="pol_countries_permissive",
                help="If the RFP says nothing about geography, treat as eligible. "
                     "Turn off to be strict (only RFPs that explicitly name your countries pass).",
            )

        with pol_tabs[1]:
            st.markdown(
                "**Required theme keywords** — RFP must mention ≥1 of these to be admitted. "
                "Type and press Enter to add."
            )
            _tag_input(
                "Required themes",
                list(_live["themes"]["required_any"]),
                key="pol_themes_required",
            )
            st.markdown(
                "**Excluded themes (HARD REJECT at scan time)** — RFPs matching any "
                "of these are dropped before insertion. Use this for off-mission "
                "topics like *clinical trial*, *preclinical*, etc."
            )
            _tag_input(
                "Excluded themes",
                list(_live["themes"].get("excluded_any") or []),
                key="pol_themes_excluded",
            )
            st.markdown("**Opportunity-type opt-outs** — title-based hard rejects. "
                        "Turn off if your org *does* pursue these.")
            _excl = _live.get("exclusions") or {}
            st.checkbox(
                "Reject training / education programs",
                value=bool(_excl.get("reject_training_only", True)),
                key="pol_excl_training",
                help="Drops 'X Training Center', 'Student Education Program', etc. — "
                     "capacity-building of trainees, not a grant to implement a project.")
            st.checkbox(
                "Reject loans / debt instruments",
                value=bool(_excl.get("reject_loans", True)),
                key="pol_excl_loans",
                help="Drops loans / concessional debt — most implementing orgs want "
                     "grants and awards, not money to repay.")
            st.checkbox(
                "Reject consultancy / contractor RFPs",
                value=bool(_excl.get("reject_consultancies", True)),
                key="pol_excl_consult",
                help="Drops 'X Consultants' / 'X Contractor' procurement — hiring a "
                     "person/firm to deliver a service, not an org project grant. "
                     "Turn off if your org pursues consultancies.")
            st.checkbox(
                "Reject reimbursement programs",
                value=bool(_excl.get("reject_reimbursement", True)),
                key="pol_excl_reimburse",
                help="Drops 'X Reimbursement Program' (e.g. Ryan White Part F Dental "
                     "Reimbursement) — repays a closed set of named existing "
                     "providers for incurred costs, not an open competitive grant.")

            st.markdown("---")
            st.markdown("**Who you are (applicant type)** — match the call's "
                        "published eligibility against your org type. A call open "
                        "*only* to types you're not (and with no open/unrestricted "
                        "type) is rejected as out of scope.")
            _elig = _live.get("eligibility") or {}
            _bucket_labels = {
                "nonprofit": "Nonprofit / NGO / civil society",
                "government": "Government / public sector",
                "school_district": "School district",
                "higher_ed": "University / college (higher education)",
                "for_profit": "For-profit / business",
                "individual": "Individual",
                "tribal": "Tribal organization",
            }
            _bucket_keys = list(_bucket_labels.keys())
            _cur = [b for b in (_elig.get("org_applicant_types") or ["nonprofit"])
                    if b in _bucket_labels]
            st.multiselect(
                "Your organization's applicant type(s)",
                options=_bucket_keys,
                default=_cur or ["nonprofit"],
                format_func=lambda b: _bucket_labels.get(b, b),
                key="pol_org_applicant_types",
                help="Most implementing orgs are Nonprofit / NGO. Pick all that apply.")
            st.checkbox(
                "Reject calls that don't admit your applicant type",
                value=bool(_elig.get("reject_applicant_type_mismatch", True)),
                key="pol_excl_applicant_mismatch",
                help="Conservative: only fires when a call publishes an explicit "
                     "eligible-applicant list that has no open type and none of "
                     "yours, or says it's invitation-only / current-grantees-only. "
                     "Calls with no published list are left alone.")

        with pol_tabs[2]:
            st.markdown(
                "**Crawl keyword assist (optional).** Criteria are auto-derived "
                "from the Organization profile (Setup → Bid Fitness); these terms "
                "refine the crawl against the RFP text — a **positive** term "
                "*confirms* the criterion when it can't be derived from the "
                "profile; a **negative** term is a *red flag* that forces it to "
                "**No** (for a MUST that screens the RFP out as Decline). Leave "
                "blank to rely purely on the derivation. Scores stay 2 / 1 / 0. "
                "(Out-of-capability hard-reject terms live under **Themes → Excluded**.)"
            )
            _crit_labels = {
                "qualification": "MUST 1 — Legal status & qualification",
                "strategic_fit": "MUST 2 — Strategic fit",
                "capacity": "MUST 3 — Implementation capacity",
                "geographic_fit": "MUST 4 — Geographic fit",
                "cofinancing": "MUST 5 — Cofinancing & compliance",
                "funding_quality": "PREFER 6 — Funding quality",
                "funder_relationship": "PREFER 7 — Donor relationship",
                "competitiveness": "PREFER 8 — Competitiveness",
                "bid_effort": "PREFER 9 — Bid effort",
            }
            # The tags below come from the SAVED policy. Use this to overwrite
            # just the criteria terms with the recommended code defaults (keeps
            # your countries / themes / exclusions). Clears the widget state so
            # the new terms render immediately.
            if st.button("↺ Reset criteria terms to recommended defaults",
                         key="reset_crit_terms",
                         help="Replaces only the per-criterion positive/negative "
                              "terms with the latest recommended set."):
                import copy as _copy
                _cur = _pol.get_policies()
                _cur["criteria"] = _copy.deepcopy(_pol.DEFAULT_POLICIES["criteria"])
                _pol.set_policies(_cur, updated_by=user.get("email"))
                for _ck in _crit_labels:
                    st.session_state.pop(f"pol_pos_{_ck}", None)
                    st.session_state.pop(f"pol_neg_{_ck}", None)
                st.session_state["pol_save_msg"] = (
                    "✓ Criteria terms reset to the recommended defaults.")
                st.rerun()

            for ckey, clabel in _crit_labels.items():
                rule = (_live.get("criteria") or {}).get(ckey, {}) or {}
                with st.expander(clabel, expanded=False):
                    col_pos, col_neg = st.columns(2)
                    with col_pos:
                        _tag_input(
                            "Positive terms (confirm)",
                            list(rule.get("positive") or []),
                            key=f"pol_pos_{ckey}",
                            help="Found in the RFP → confirms this criterion (Yes) "
                                 "when it can't be derived.")
                    with col_neg:
                        _tag_input(
                            "Negative terms (red flag → No)",
                            list(rule.get("negative") or []),
                            key=f"pol_neg_{ckey}",
                            help="Found in the RFP → forces this criterion to No.")

        with pol_tabs[3]:
            from core import web_search as _ws
            from core.settings import set_setting as _set_setting
            st.markdown(
                "**Search terms (MeSH / keyword library)** — when you run a *broad* "
                "web search (e.g. *health*) on the Search page, it fans out into one "
                "RFP-framed query per topic below. The built-in list is fixed; add "
                "your own to widen discovery."
            )
            st.caption("Built-in (" + str(len(_ws._HEALTH_PIVOTS)) + "): "
                       + " · ".join(_ws._HEALTH_PIVOTS))
            _custom = _tag_input(
                "Your custom search terms", _ws.custom_pivots(),
                key="pol_search_pivots",
                help="Each becomes its own RFP-framed query in a broad search. "
                     "Type a term (e.g. a MeSH heading) and press Enter.")
            if st.button("💾 Save search terms", key="save_search_pivots_btn"):
                import json as _json
                try:
                    _vals = [str(t).strip() for t in (_custom or []) if str(t).strip()]
                    _set_setting(_ws.SEARCH_PIVOTS_KEY, _json.dumps(_vals),
                                 updated_by=user.get("email"))
                    _clr = getattr(_ws.search, "clear", None)
                    if _clr:
                        _clr()  # drop cached searches so new terms apply immediately
                    st.success(f"✓ Saved {len(_vals)} custom search term(s) — "
                               "applied to the next broad web search.")
                except Exception as exc:
                    st.error(f"Couldn't save search terms: {exc}")

        with pol_tabs[4]:
            st.subheader("Currency exchange rates")
            st.caption(
                "USD rate = how many USD one unit of the currency converts to "
                "(e.g. GBP 1.33 means £1 = $1.33). **Click any cell to edit it.** "
                "Use the **+** at the bottom of the table to add a new currency, "
                "and the row trash icon to remove. Click **💾 Save currency rates** "
                "below when done."
            )
            from core import dropdowns as _d
            cur_list = _d.load().get("currencies", []) or []
            cur_df = pd.DataFrame([
                {
                    "Code": c.get("code") or "",
                    "Label": c.get("label") or "",
                    "Symbol": c.get("symbol") or "",
                    "Aliases": ", ".join(c.get("aliases") or []),
                    "USD rate": float(c.get("usd_rate") or 1.0),
                }
                for c in cur_list
            ])
            edited_cur = st.data_editor(
                cur_df,
                num_rows="dynamic",
                hide_index=True,
                width='stretch',
                column_config={
                    "Code":   st.column_config.TextColumn("Code", required=True, help="3-letter ISO code (USD, GBP, EUR, XAF, CAD, ...)"),
                    "Label":  st.column_config.TextColumn("Label"),
                    "Symbol": st.column_config.TextColumn("Symbol", help="$, £, €, C$, or blank"),
                    "Aliases": st.column_config.TextColumn("Aliases", help="Comma-separated legacy labels (e.g. 'GBP £, £')"),
                    "USD rate": st.column_config.NumberColumn(
                        "USD rate", min_value=0.0001, max_value=10000.0, step=0.0001, format="%.4f", required=True,
                    ),
                },
                key="fx_editor",
            )
            if st.button("💾 Save currency rates", type="primary"):
                new_list = []
                for _, row in edited_cur.iterrows():
                    code = (row.get("Code") or "").strip()
                    if not code:
                        continue
                    aliases = [a.strip() for a in (row.get("Aliases") or "").split(",") if a.strip()]
                    new_list.append({
                        "code": code,
                        "label": (row.get("Label") or code).strip() or code,
                        "symbol": (row.get("Symbol") or None) or None,
                        "aliases": aliases,
                        "usd_rate": float(row.get("USD rate") or 1.0),
                    })
                settings.set_currency_overrides(new_list, updated_by=user.get("email"))
                st.success(f"Saved {len(new_list)} currency entries.")
                st.rerun()
        with pol_tabs[5]:
            st.markdown(
                "**Raw JSON** — read-only preview. Use the other tabs to edit; "
                "this is here for export/debugging only."
            )
            st.code(__import__("json").dumps(_live, indent=2), language="json")

        pc1, pc2, _ = st.columns([1, 1, 4])
        if pc1.button("💾 Save preferences", type="primary", key="save_policies_btn"):
            def _list(key: str) -> list[str]:
                """Multiselect-backed session keys hold a list of strings;
                be defensive in case an older string value lingers."""
                v = st.session_state.get(key, [])
                if isinstance(v, str):
                    return [ln.strip() for ln in v.splitlines() if ln.strip()]
                return [str(x).strip() for x in (v or []) if str(x).strip()]

            new_pol = {
                "countries": {
                    "eligible": _list("pol_countries_eligible"),
                    "broad_terms": _list("pol_countries_broad"),
                    "permissive_when_silent": bool(st.session_state.get("pol_countries_permissive", True)),
                },
                "themes": {
                    "required_any": _list("pol_themes_required"),
                    "excluded_any": _list("pol_themes_excluded"),
                },
                "exclusions": {
                    "reject_training_only": bool(st.session_state.get("pol_excl_training", True)),
                    "reject_loans": bool(st.session_state.get("pol_excl_loans", True)),
                    "reject_consultancies": bool(st.session_state.get("pol_excl_consult", True)),
                    "reject_reimbursement": bool(st.session_state.get("pol_excl_reimburse", True)),
                },
                "eligibility": {
                    "org_applicant_types": list(
                        st.session_state.get("pol_org_applicant_types") or ["nonprofit"]),
                    "reject_applicant_type_mismatch": bool(
                        st.session_state.get("pol_excl_applicant_mismatch", True)),
                },
                # Per-criterion crawl-assist terms (positive/negative; no rigor).
                "criteria": {
                    ckey: {
                        "positive": _list(f"pol_pos_{ckey}"),
                        "negative": _list(f"pol_neg_{ckey}"),
                    }
                    for ckey in (
                        "qualification", "strategic_fit", "capacity",
                        "geographic_fit", "cofinancing", "funding_quality",
                        "funder_relationship", "competitiveness", "bid_effort")
                },
            }
            try:
                _pol.set_policies(new_pol, updated_by=user.get("email"))
                st.session_state["pol_save_msg"] = (
                    "✓ Policies saved. The next scan will use the new rules."
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Save failed: {exc}")
        if pc2.button("↺ Reset to defaults", key="reset_policies_btn"):
            _pol.reset_to_defaults(updated_by=user.get("email"))
            st.session_state["pol_save_msg"] = "✓ Reset to defaults."
            st.rerun()

