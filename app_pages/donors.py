"""Donor Intelligence Mapping — donor metadata dashboard + CRUD.

Reads the `donor_intel` table (seeded via scripts/import_donor_intel.py).
Layout: summary cards → category chart → quick donor lookup (dropdown +
full detail with a greyed checkbox matrix) → filter/search → paginated table
with per-row edit / delete / share + CSV export.

Edit/delete is admin + super_user only; everyone else views + exports.
Auth + global header already ran in App.py; this file is content-only.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

from core import geographies as _geo
from core import permissions, settings
from core import program_area_classifier as _pa
from core.program_area_classifier import category_full as _pa_cat, subarea_label as _pa_sub
from core.program_area_select import program_area_matrix_editor, rating_bars_html
from core.partners import ALL_PARTNERS
from core.records import clean_df
from db.supabase_client import get_client

# Deploying org's country — substituted wherever a record uses the literal
# {country} placeholder (focus-country-agnostic data, resolved at display time).
# Blank org_country falls back to a readable phrase so nothing renders empty.
_COUNTRY = (settings.get_org().get("org_country") or "").strip() or "the focus country"

user = st.session_state["app_user"]
can_edit = permissions.is_admin(user)          # admin OR super_user only
sb = get_client()

st.title("Donor Intelligence Mapping")

# Flash message after a modal save/delete closes (so we're sure it landed).
_flash = st.session_state.pop("_donor_flash", None)
if _flash:
    st.success(_flash)
_flash_warn = st.session_state.pop("_donor_flash_warn", None)
if _flash_warn:
    st.warning(_flash_warn)

st.caption(
    "Country-agnostic donor metadata powering RFP eligibility screening. "
    "A ticked box = yes; unticked = no (blank stays unknown until edited)."
)


# Editable columns of the one-to-many donor_contacts table (focal persons +
# additional contacts). Order = display/edit order.
_CONTACT_COLS = ["contact_name", "role_title", "email", "phone",
                 "linkedin_url", "address", "is_official", "notes"]


@st.cache_data(ttl=60)
def _load() -> pd.DataFrame:
    res = get_client().table("donor_intel").select("*").order("donor").execute()
    return clean_df(pd.DataFrame(res.data or []))


@st.cache_data(ttl=60)
def _load_contacts(canonical_key: str) -> pd.DataFrame:
    """Focal-person / additional contacts for one donor (official channels
    first, then by name)."""
    res = (get_client().table("donor_contacts").select("*")
           .eq("canonical_key", canonical_key)
           .order("is_official", desc=True).order("contact_name").execute())
    return clean_df(pd.DataFrame(res.data or []))


df = _load()
if df.empty:
    st.info(
        "No donor records yet. Apply **migration 020** then run "
        "`python scripts/import_donor_intel.py`."
    )
    st.stop()


# ---------------------------------------------------------------------------
# Field classification + acronym-aware labels
# ---------------------------------------------------------------------------
_ACR = {
    "lmic": "LMIC", "ngo": "NGO", "hiv": "HIV", "aids": "AIDS", "tb": "TB",
    "ncd": "NCD", "ncds": "NCDs", "mnch": "MNCH", "srhr": "SRHR", "hss": "HSS",
    "ai": "AI", "usd": "USD", "uei": "UEI", "sam": "SAM", "mou": "MOU",
    "cv": "CV", "cvs": "CVs", "irb": "IRB", "ccm": "CCM", "dfi": "DFI",
    "us": "US", "mande": "M&E", "govt": "Govt",
}


_LABEL_OVERRIDES = {
    "donor_type": "Donor type (descriptive)",
    "donor_is_dual_role_implementer": "Also an implementer that publishes calls?",
    "donor_opportunity_listing_urls": "Opportunity listing URL(s)",
    "donor_local_board_required": "Local board members required",
    # MUST-5 cofinancing & compliance (migration 052). The portal URL reuses the
    # existing submission_portal_url (matched against the org's donor_registrations).
    "donor_govt_mou_required": "Government MOU required",
    "donor_funding_platform_registration_required": "Funding-platform registration required",
    # PREFER-8 competitiveness (migration 053)
    "donor_multi_country_encouraged": "Encourages multi-country proposals",
    "donor_summary_description": "Summary",
    "donor_values": "Values",
    "donor_strategy_url": "Strategy (URL)",
    "donor_projected_budget_period": "Budget period",
    # Strategic-intelligence fields (migration 029)
    "donor_strategic_priorities": "Strategic priorities",
    "donor_in_scope": "What they fund (in scope)",
    "donor_out_of_scope": "What they don't fund (out of scope)",
    "donor_selection_criteria": "Selection / evaluation criteria",
    "donor_funding_programs": "Funding programs / windows",
    "donor_eligibility_notes": "Who can apply / lead",
    "donor_application_deadlines": "Key dates / deadlines",
    "donor_submission_portal_url": "Submission portal (URL)",
    "donor_strategic_fit_notes": "Ideal applicant & what this funder rewards",
    "donor_gaps_risks": "Common pitfalls & disqualifiers",
    "donor_recommended_approach": "How to position a competitive application",
    "donor_funders_collaborators": "Funders & Collaborators",
    "donor_hq_country_required": "Applicant must be HQ'd in",
    "org_stage_required": "Org stage required",
    "donor_max_annual_budget": "Max annual budget (eligibility ceiling)",
    "donor_min_track_record": "Min largest grant managed (floor)",
    "donor_required_partner_type": "Required partner type",
    "donor_required_partner_country": "Required partner country",
    "donor_max_request_pct_of_budget": "Max request (% of project budget)",
    "donor_min_cofinancing_secured_pct": "Min co-financing secured (%)",
    "donor_independent_entity_required": "Independent entity required (no INGO affiliates)",
    "donor_welcome_registration_required": "Pre-registration / senior-leadership approval required",
    # MUST-1 rework (migration 049)
    "donor_entity_type_required": "Entity type required",
    "donor_registration_region": "Registration region/country required",
    "donor_requires_pi": "Requires an individual / PI",
    "donor_pi_country_scope": "PI base-country scope",
    "donor_max_prior_grant": "Max prior grant/award (eligibility ceiling)",
    "donor_prior_beneficiary_rule": "Prior-beneficiary rule",
}

# Partner-type vocabulary — shared with the org fit profile so a donor's
# required_partner_type matches the org's partner records.
_PARTNER_TYPE_OPTIONS = [
    "Nonprofit / NGO", "Academic / research institutions", "For-profit / private",
    "Government", "Multilateral / UN", "Bilateral / development agency",
    "Philanthropy / foundation",
]


def _pretty_choice(v: str) -> str:
    """Display a stored enum value nicely without changing what's stored:
    'reimbursement_only' → 'Reimbursement Only', 'high' → 'High', '' → '—'."""
    s = str(v or "").strip()
    return s.replace("_", " ").title() if s else "—"


def _label(col: str) -> str:
    # The DB columns carry a source-role prefix (donor_/call_/org_) for an auditable
    # schema, but those prefixes must NOT show in the form — "Donor tax exempt status
    # required" reads as wanting the DONOR's status, when it means "the donor REQUIRES
    # tax-exempt status from applicants". So strip the prefix for display only (owner
    # 2026-06-29). Schema is unchanged.
    if col in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[col]
    bare = re.sub(r"^(donor_|call_|org_)", "", col)
    if bare in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[bare]
    words = bare.replace("_", " ").split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in _ACR:
            out.append(_ACR[lw])
        elif i == 0:
            out.append(w.capitalize())
        else:
            out.append(lw)
    return " ".join(out)


# Acronyms that are legitimately uppercase and must NOT be sentence-cased when
# we tidy SHOUTED phrases from the source workbook (e.g. "HEALTH SYSTEMS
# STRENGTHENING" -> "Health systems strengthening", but keep "HIV", "MNCH").
_ACRONYMS = {
    "HIV", "AIDS", "TB", "MNCH", "HSS", "SRHR", "SRH", "NCD", "NCDS", "LMIC",
    "LMICS", "NGO", "NGOS", "CCM", "DFI", "UHC", "PHC", "WHO", "UN", "US", "EU",
    "UK", "AI", "HPV", "NTD", "NTDS", "RMNCAH", "RMNCH", "STI", "STIS", "WASH",
    "UNICEF", "UNAIDS", "UNFPA", "UNHCR", "UNESCO", "USAID", "CEPI", "GAVI",
    "COVID", "IRB", "MOU", "CSO", "CSOS", "PREP", "FP", "GBV", "R&D", "DR-TB",
    "GF", "AFD", "AFDB", "EC", "MCF", "BMGF",
}
_SEG_SPLIT_RE = re.compile(r"([.;]\s+)")


def _cap_first(s: str) -> str:
    """Capitalize the first alphabetic char only (don't touch the rest)."""
    for i, ch in enumerate(s):
        if ch.isalpha():
            return s[:i] + ch.upper() + s[i + 1:]
    return s


def _smart_token(tok: str) -> str:
    """Lowercase a SHOUTED all-caps word/compound, but keep real acronyms.
    A token is 'shouted' if it's all-caps and has an alpha run >=5 letters or
    is a hyphenated compound (CROSS-CUTTING) — otherwise it's treated as an
    acronym (HIV, MNCH, AfDB) and left alone."""
    lead = re.match(r"^[^A-Za-z]*", tok).group(0)
    rest = tok[len(lead):]
    trail = re.search(r"[^A-Za-z]*$", rest).group(0)
    core = rest[:len(rest) - len(trail)] if trail else rest
    if not core or not core.isupper() or core.upper() in _ACRONYMS:
        return tok
    alpha_runs = re.findall(r"[A-Z]+", core)
    longest = max((len(r) for r in alpha_runs), default=0)
    if longest >= 5 or ("-" in core and len(alpha_runs) >= 2):
        return lead + core.lower() + trail
    return tok


def _deshout(text: str) -> str:
    """Turn SHOUTED phrases from the source workbook into sentence case for
    display, preserving acronyms. Only segments that actually contained a
    shouted word get re-capitalised, so already-clean text is untouched."""
    if not text or not any(c.isupper() for c in text):
        return text
    out = []
    for seg in _SEG_SPLIT_RE.split(text):
        if not seg or _SEG_SPLIT_RE.fullmatch(seg):
            out.append(seg)
            continue
        seg2 = " ".join(_smart_token(t) for t in seg.split(" "))
        out.append(_cap_first(seg2) if seg2 != seg else seg)
    return "".join(out)


def _disp(v):
    """Clean display value: None for NaN / blank / 'nan' / 'none', else str.
    Also tidies SHOUTED all-caps phrases to sentence case (display only — the
    source data is untouched, so the edit form still shows the raw value)."""
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if s.lower() in ("nan", "none", "nat", ""):
        return None
    # Retire the {country} placeholder — it resolved to the org's single Primary
    # country (misleading). Neutralize to broad language; real per-donor
    # coverage now lives in the "Funded geographies" field.
    s = (s.replace("{country}'s ", "").replace("{country}'s", "")
          .replace("in {country}", "in priority countries")
          .replace("{country}", "priority countries"))
    return _deshout(s)


def _md(v):
    """Markdown-safe value — escape '$' so award amounts ($1K, $50M) don't
    render as LaTeX math."""
    d = _disp(v)
    return d.replace("$", r"\$") if d else None


def _title_name(row: dict) -> str:
    """Donor display name, appending the short code only when it isn't already
    part of the name (avoids 'African Development Bank (AfDB) (AfDB)')."""
    donor = _disp(row.get("donor")) or "Donor"
    short = _disp(row.get("donor_short")) or ""
    if short and short.lower() not in donor.lower():
        return f"{donor} ({short})"
    return donor


# Internal / provenance columns — never shown as flags, as editable text, or in
# the share/PDF export (award_size_basis is an internal provenance note, not
# donor-facing content).
# ---------------------------------------------------------------------------
# Donor-category normalization — collapse case / plural / synonym redundancies
# so "Multilateral" vs "Multilaterals", "Bilaterals" vs "Bilaterals /
# government development agencies", and the two casings of "…Philanthropies and
# Foundations" stop appearing as separate categories in the chart, filter and
# table. Display-only (DB values untouched); the edit form writes the canonical
# value going forward. Extend _CATEGORY_CANON as new variants show up.
# ---------------------------------------------------------------------------
_CATEGORIES = [
    "Bilaterals / government development agencies",
    "Multilaterals & development banks",   # UN agencies, MDBs/DFIs, GH partnerships
    "International philanthropies & foundations",
    "U.S. federal agencies",
    "Private sector / corporate",
    "Academic / research institutions",
]
_CATEGORY_CANON = {
    "bilaterals": "Bilaterals / government development agencies",
    "bilateral": "Bilaterals / government development agencies",
    "bilaterals / government development agencies": "Bilaterals / government development agencies",
    "government development agencies": "Bilaterals / government development agencies",
    "multilaterals": "Multilaterals & development banks",
    "multilateral": "Multilaterals & development banks",
    "multilaterals / global health partnerships": "Multilaterals & development banks",
    "multilateral / global health partnerships": "Multilaterals & development banks",
    "global health partnerships": "Multilaterals & development banks",
    "multilateral development bank / dfi": "Multilaterals & development banks",
    "multilateral development banks / dfis": "Multilaterals & development banks",
    "multilateral development bank": "Multilaterals & development banks",
    "development finance institution": "Multilaterals & development banks",
    "dfi": "Multilaterals & development banks",
    "multilaterals & development banks": "Multilaterals & development banks",
    "international philanthropies and foundations": "International philanthropies & foundations",
    "international philanthropies & foundations": "International philanthropies & foundations",
    "philanthropies and foundations": "International philanthropies & foundations",
    "philanthropy": "International philanthropies & foundations",
    "foundation": "International philanthropies & foundations",
    "foundations": "International philanthropies & foundations",
    "u.s. federal agencies": "U.S. federal agencies",
    "u.s. federal / bilateral agencies": "U.S. federal agencies",
    "us federal agencies": "U.S. federal agencies",
    "u.s. federal agency": "U.S. federal agencies",
    "private sector": "Private sector / corporate",
    "corporate": "Private sector / corporate",
    "academic": "Academic / research institutions",
    "research institution": "Academic / research institutions",
}


def _normalize_category(raw) -> str:
    """Map any raw donor_category to a canonical display value."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return "(uncategorised)"
    s = re.sub(r"\s+", " ", str(raw).strip())
    if not s:
        return "(uncategorised)"
    key = s.lower().rstrip(".")
    if key in _CATEGORY_CANON:
        return _CATEGORY_CANON[key]
    if key.endswith("s") and key[:-1] in _CATEGORY_CANON:  # plural fold
        return _CATEGORY_CANON[key[:-1]]
    return s  # unknown → keep cleaned original (at least case/space-trimmed)


_META = {"id", "updated_at", "created_at", "canonical_key", "category_clean",
         "online_source_check_status", "last_checked", "row_type",
         "award_size_basis"}
_SHORT_TEXT = ["donor", "donor_short", "donor_category", "donor_type", "donor_website",
               "donor_award_low", "donor_award_high",
               "donor_total_annual_funding_global", "donor_funding_mechanism"]
_LONG_TEXT = ["donor_aliases", "donor_geographic_scope", "donor_active_route_status",
              "donor_direct_local_org_eligible", "donor_priority_areas",
              "donor_verification_caveats", "donor_evidence_summary", "notes",
              "donor_source_urls", "donor_opportunity_listing_urls"]
# Institutional / official donor contact (one set per donor — the donor_intel
# row). The many focal-person contacts live in the donor_contacts table.
_CONTACT = ["donor_hq_address", "donor_hq_country", "donor_main_phone", "donor_general_email",
            "donor_linkedin_url", "donor_other_profile_urls",
            "donor_contact_persons", "donor_contact_emails", "donor_contact_phones",
            "donor_contact_linkedin_urls"]
_CHOICE = {
    "donor_prefinance_required": ["", "none", "partial", "reimbursement_only"],
    "donor_verification_level": ["", "high", "medium", "low"],
    "donor_is_dual_role_implementer": ["", "yes", "no"],
}
# Qualitative donor-intelligence profile (added to the edit form, migration 025).
# Listed here so these columns are treated as free text — NOT flags — once they
# exist in the table, and so the View / Share / PDF summaries include them.
_PROFILE = ["donor_founded", "donor_summary_description", "donor_mission", "donor_vision", "donor_values",
            "donor_strategy_url", "donor_total_awards", "donor_total_funding_to_date",
            "donor_current_awards", "donor_past_awards", "donor_projected_budget",
            "donor_projected_budget_period", "donor_funding_cycle", "donor_recent_activity",
            "donor_application_process", "donor_reporting_requirements",
            "donor_past_projects_json",
            # Strategic-intelligence fields (migration 029) — narrative + JSON.
            # Listed here so they're treated as text (not flags) and persist /
            # surface in the view, share and PDF.
            "donor_strategic_priorities", "donor_in_scope",
            "donor_out_of_scope", "donor_selection_criteria", "donor_funding_programs",
            "donor_funding_tiers_json", "donor_eligibility_notes", "donor_application_deadlines",
            "donor_submission_portal_url", "donor_strategic_fit_notes", "donor_gaps_risks",
            "donor_recommended_approach", "donor_priority_ratings", "donor_funders_collaborators",
            # Hard eligibility conditions (migration 032) — VALUED (not flags).
            "donor_hq_country_required", "org_stage_required", "donor_max_annual_budget",
            "donor_min_track_record", "donor_required_partner_type", "donor_required_partner_country",
            "donor_max_request_pct_of_budget", "donor_min_cofinancing_secured_pct",
            # MUST-1 rework conditions (migration 049) — VALUED text, not flags.
            "donor_entity_type_required", "donor_registration_region",
            "donor_requires_pi", "donor_pi_country_scope", "donor_max_prior_grant",
            "donor_prior_beneficiary_rule"]
# Columns kept for backward-compat but no longer surfaced anywhere (not edited,
# not shown in View, not in share/PDF). verification_level already captures data
# confidence, so the free-text "verification caveats" was redundant + confusing.
# funded_geographies is an unused column (the geo field is donor_geographic_scope).
_HIDDEN_FIELDS = {"donor_verification_caveats", "donor_funded_geographies"}

_NON_FLAG = (set(_META) | set(_SHORT_TEXT) | set(_LONG_TEXT)
             | set(_CHOICE) | set(_CONTACT) | set(_PROFILE) | _HIDDEN_FIELDS)

_FLAGS = [c for c in df.columns if c not in _NON_FLAG]
_ELIG = [
    "donor_ngo_eligible", "donor_for_profit_eligible", "donor_govt_or_ccm_route_required",
    "donor_grant_route", "donor_procurement_tender_route", "donor_loan_dev_finance_route",
    "donor_subrecipient_partner_possible", "donor_open_call_unsolicited",
    "donor_invitation_solicited", "donor_two_stage_application", "donor_online_portal_submission",
    "donor_lmic_africa_focus", "donor_global_multi_country_scope",
]
_FIT = [c for c in _FLAGS if c.endswith("_fit")]
_REQ = [c for c in _FLAGS if c not in _ELIG and c not in _FIT]
_FLAG_GROUPS = {
    "Eligibility & routes": [c for c in _ELIG if c in _FLAGS],
    "Program-area fit": _FIT,
    "Requirements & compliance": _REQ,
}

# Normalized category for the chart / filter / table (display-only).
df["category_clean"] = (df["donor_category"].map(_normalize_category)
                        if "donor_category" in df.columns else "(uncategorised)")

# ── Structured dropdown vocabularies for the donor edit form ────────────────
# Free text → finite choices. Multi fields accept typed custom values (covers
# "Other"); single fields use an explicit "Other (specify)" escape hatch.
# (Program areas now use the SHARED hierarchical taxonomy via
# core.program_area_select.program_area_rating_editor — same schema as the org
# fit profile — so the donor's *_fit flags are deprecated for the picker.)
FUNDING_MECHANISMS = [
    "Grants", "Loans / concessional finance", "Procurement / contracts",
    "Program-related investments (equity/debt)", "Technical assistance",
    "Co-financing", "Prizes / challenges", "In-kind / commodities",
]
ROUTE_STATUSES = ["Active", "Inactive", "On hold / paused", "Closed", "Unknown"]
LOCAL_ORG_ELIGIBLE = [
    "Yes — direct", "Yes — via competitive RFP / invited proposal",
    "Yes — via international partner only", "No", "Unknown",
]
# Structured vocabularies for the Intelligence section (each + "Other (specify)").
FUNDING_CYCLES = [
    "Rolling / open call (no fixed deadline)", "Annual",
    "Biannual (twice a year)", "Quarterly", "Multi-year cycle",
    "Ad hoc / by announcement", "Unknown",
]
APPLICATION_PROCESSES = [
    "Concept note → full proposal (two-stage)", "Full proposal (single-stage)",
    "Letter of inquiry / EOI first", "Online portal submission",
    "By invitation only", "Competitive tender / RFP",
    "Unsolicited proposals accepted", "Unknown",
]
REPORTING_REQUIREMENTS = [
    "Narrative + financial reports", "Quarterly reporting",
    "Semi-annual reporting", "Annual reporting",
    "Milestone / deliverable-based", "Final report only",
    "Independent audit required", "Unknown",
]
_OTHER = "Other (specify)"

# Founded-year + HQ-country dropdowns (avoid free-text typos). Years run from the
# current year back to 1800; "" is the unset option. HQ country reuses the shared
# country vocabulary. Callers append any legacy value not in the list so editing
# an existing donor never silently drops it.
_YEAR_OPTIONS = [""] + [str(_y) for _y in range(date.today().year, 1799, -1)]
_HQ_COUNTRY_OPTIONS = [""] + list(_geo.COUNTRIES)


def _to_list(v) -> list[str]:
    """Parse a stored value into a list — JSON list, else split legacy free text."""
    if v is None:
        return []
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "nat"):
        return []
    try:
        j = json.loads(s)
        if isinstance(j, list):
            return [str(x).strip() for x in j if str(x).strip()]
    except (ValueError, TypeError):
        pass
    # Split legacy free text on ; and , only — NOT "/", which appears inside
    # real values ("Procurement / contracts", "(equity/debt)").
    return [t.strip() for t in re.split(r"[;,]", s) if t.strip()]


def _na(v) -> bool:
    """True if v is pandas NA/NaN/NaT (safe on non-scalars)."""
    try:
        return v is not None and bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _cell(v):
    """Stripped string for a data-editor cell, or None. Safe against pandas
    NA / NaN (a bare `if cell` on a StringDtype NA raises 'ambiguous')."""
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s or None


# Fields stored as JSON lists — rendered as comma lists in share/export.
_LIST_FIELDS = {"donor_geographic_scope", "donor_priority_areas",
                "donor_funding_mechanism", "donor_funders_collaborators",
                # MUST-1 multi-selects (migration 049) — stored as JSON lists.
                "donor_hq_country_required", "donor_registration_region", "donor_required_partner_type",
                "donor_required_partner_country"}

# Unified partner/funder options for the "Funders & collaborators" picker — the
# shared partner vocabulary merged with the donor catalog (so any catalogued donor
# is also pickable as a funder/collaborator). Typed additions are accepted too.
_PARTNER_OPTIONS = list(dict.fromkeys(
    list(ALL_PARTNERS)
    + sorted(df["donor"].dropna().astype(str).str.strip().unique().tolist()
             if "donor" in df.columns else [])))


def _disp_field(row: dict, col: str):
    """Display string for share/export: list-fields joined with commas, else _disp."""
    if col in _LIST_FIELDS:
        vals = _to_list(row.get(col))
        return ", ".join(vals) if vals else None
    return _disp(row.get(col))


def _json_dicts(v) -> list[dict]:
    """Parse a JSON-array-of-objects column into a list of dicts (else [])."""
    try:
        j = json.loads(v or "[]")
    except (ValueError, TypeError):
        return []
    return [d for d in j if isinstance(d, dict)] if isinstance(j, list) else []


def _past_projects(row: dict) -> list[dict]:
    """Parsed past-projects list (only entries with a title), or []."""
    return [p for p in _json_dicts(row.get("donor_past_projects_json")) if p.get("title")]


def _funding_tiers(row: dict) -> list[dict]:
    """Parsed funding-tiers list (only entries with a name), or []."""
    return [t for t in _json_dicts(row.get("donor_funding_tiers_json")) if t.get("name")]


# Fields that count toward the data-completeness score (every donor-facing field).
# JSON/list fields count as documented when non-empty; flags count when explicitly
# yes/no (blank = undocumented). donor_short is excluded (optional acronym).
_COMPLETENESS_TEXT = [
    "donor", "donor_category", "donor_website", "donor_founded",
    "donor_general_email", "donor_main_phone", "donor_hq_country", "donor_hq_address",
    "donor_linkedin_url", "donor_other_profile_urls", "donor_summary_description", "donor_mission",
    "donor_vision", "donor_values", "donor_strategic_priorities", "donor_strategy_url",
    "donor_award_low", "donor_award_high", "donor_total_annual_funding_global",
    "donor_total_awards", "donor_total_funding_to_date", "donor_current_awards", "donor_past_awards",
    "donor_projected_budget", "donor_funding_programs", "donor_in_scope", "donor_out_of_scope",
    "donor_direct_local_org_eligible", "donor_active_route_status", "donor_prefinance_required",
    "donor_application_process", "donor_funding_cycle", "donor_reporting_requirements",
    "donor_application_deadlines", "donor_submission_portal_url", "donor_recent_activity",
    "donor_eligibility_notes", "donor_selection_criteria", "donor_strategic_fit_notes",
    "donor_gaps_risks", "donor_recommended_approach", "donor_verification_level",
    "donor_evidence_summary", "notes", "donor_source_urls",
]
_COMPLETENESS_JSON = ["donor_funding_tiers_json", "donor_past_projects_json"]


def _completeness(row: dict) -> tuple[int, int, int]:
    """Data-quality = % of donor fields that are documented (all fields counted).
    Returns (percent, populated, total)."""
    have = total = 0
    for c in _COMPLETENESS_TEXT:
        total += 1
        if _disp(row.get(c)):
            have += 1
    for c in list(_LIST_FIELDS) + _COMPLETENESS_JSON:
        total += 1
        ok = bool(_json_dicts(row.get(c))) if c.endswith("_json") else bool(_to_list(row.get(c)))
        if ok:
            have += 1
    # program_area_ratings is a JSON OBJECT (not a list) — documented if non-empty.
    total += 1
    try:
        _r = json.loads(row.get("donor_priority_ratings") or "{}")
    except (ValueError, TypeError):
        _r = {}
    if isinstance(_r, dict) and _r:
        have += 1
    # Flags — but NOT the deprecated *_fit program-area flags (program areas are
    # now captured in priority_program_areas + program_area_ratings).
    for c in _FLAGS:
        if c in _FIT:
            continue
        total += 1
        if str(row.get(c) or "").strip().lower() in ("yes", "no"):
            have += 1
    pct = round(100 * have / total) if total else 0
    return pct, have, total


# Dialog styling: breathing room, sticky header, and a clear section/sub-label
# hierarchy (section = bold dark-blue; sub-label = small uppercase grey).
_DIALOG_CSS = """
<style>
div[data-testid="stDialog"] div[role="dialog"]{padding-left:1.1rem;padding-right:1.1rem;}
.di-stick{position:sticky;top:0;z-index:999;background:var(--background-color,#fff);
  padding:.5rem 0 .55rem;margin:-.3rem 0 .7rem;border-bottom:2px solid #e2e8f0;}
.di-stick-title{font-size:1.35rem;font-weight:800;color:#0f3d6e;line-height:1.2;}
.di-stick-sub{font-size:.82rem;color:#64748b;margin-top:3px;}
.di-badge{display:inline-block;margin-top:7px;padding:2px 11px;border-radius:11px;
  color:#fff;font-size:.74rem;font-weight:700;}
.di-sec{font-size:1.07rem;font-weight:700;color:#0f3d6e;margin:-2px 0 9px;}
.di-sub{font-size:.74rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
  color:#64748b;margin:13px 0 5px;}
</style>
"""


def _inject_dialog_css() -> None:
    st.markdown(_DIALOG_CSS, unsafe_allow_html=True)


def _completeness_band(pct: int) -> tuple[str, str]:
    """(label, hex colour) for a completeness percentage."""
    if pct >= 75:
        return "High", "#16a34a"
    if pct >= 50:
        return "Moderate", "#d97706"
    return "Low", "#dc2626"


def _sec_header(emoji: str, title: str) -> None:
    st.markdown(f"<div class='di-sec'>{emoji}&nbsp;&nbsp;{title}</div>",
                unsafe_allow_html=True)


def _sub(text: str) -> None:
    st.markdown(f"<div class='di-sub'>{text}</div>", unsafe_allow_html=True)


def _project_line(p: dict) -> str:
    """One markdown bullet for a funded project: title — amount, year, country,
    stage. <desc>."""
    bits = []
    amt = p.get("amount")
    if amt not in (None, ""):
        cur = (p.get("currency") or "").strip()
        try:
            bits.append((f"{cur} " if cur else "") + f"{float(amt):,.0f}")
        except (TypeError, ValueError):
            bits.append(((f"{cur} " if cur else "") + str(amt)).strip())
    for k in ("year", "country", "stage"):
        if p.get(k):
            bits.append(str(p[k]).strip())
    head = f"- {str(p['title']).strip()}"
    if bits:
        head += f" — {', '.join(bits)}"
    desc = str(p.get("description") or "").strip()
    if desc:
        head += f". {desc}"
    link = str(p.get("link") or "").strip()
    if link:
        head += f" ({link})"
    return head


def _tier_line(t: dict) -> str:
    """One markdown bullet for a funding tier: **name** — amount, duration. <notes>."""
    seg = [str(t[k]).strip() for k in ("amount", "duration") if t.get(k)]
    head = f"- **{str(t.get('name', '')).strip()}**"
    if seg:
        head += f" — {', '.join(seg)}"
    notes = str(t.get("notes") or "").strip()
    if notes:
        head += f". {notes}"
    return head


def _summary_lines(row: dict) -> list[str]:
    """Sectioned markdown for share / PDF / download, laid out in 'Donor
    Intelligence Report' order so the A4-portrait PDF prints with clean section
    headings. Blank fields and empty sections are skipped — only what's known
    shows. Section-title lines (no leading '- ') render as PDF sub-headings."""
    lines = [f"# {_title_name(row)}", ""]

    def section(title: str, pairs: list[tuple[str, str]]) -> None:
        pairs = [(lbl, v) for lbl, v in pairs if v]
        if not pairs:
            return
        lines.append(title)
        for lbl, v in pairs:
            lines.append(f"- **{lbl}:** {v}")
        lines.append("")

    def flags(group: str) -> str | None:
        labs = []
        for c in _FLAG_GROUPS.get(group, []):
            if _yes(row.get(c)):
                labs.append(re.sub(r"\s*fit$", "", _label(c), flags=re.I)
                            if group == "Program-area fit" else _label(c))
        return ", ".join(labs) or None

    def choice(col: str) -> str | None:
        return _pretty_choice(row.get(col)) if _disp(row.get(col)) else None

    def listf(col: str) -> str | None:
        vals = _to_list(row.get(col))
        return ", ".join(vals) if vals else None

    _cat = _normalize_category(row.get("donor_category"))
    section("Overview", [
        ("Category", _cat if _cat != "(uncategorised)" else None),
        ("Donor type", _disp(row.get("donor_type"))),
        ("Also an implementer that publishes calls",
         _disp(row.get("donor_is_dual_role_implementer"))),
        ("Founded", _disp(row.get("donor_founded"))),
        ("Website", _disp(row.get("donor_website"))),
        ("HQ country", _disp(row.get("donor_hq_country"))),
        ("HQ address", _disp(row.get("donor_hq_address"))),
        ("General email", _disp(row.get("donor_general_email"))),
        ("Main phone", _disp(row.get("donor_main_phone"))),
        ("LinkedIn", _disp(row.get("donor_linkedin_url"))),
        ("Other profiles", _disp(row.get("donor_other_profile_urls"))),
    ])
    section("About & strategy", [
        ("Summary", _disp(row.get("donor_summary_description"))),
        ("Mission", _disp(row.get("donor_mission"))),
        ("Vision", _disp(row.get("donor_vision"))),
        ("Values", _disp(row.get("donor_values"))),
        (_label("donor_strategic_priorities"), _disp(row.get("donor_strategic_priorities"))),
        ("Strategy (URL)", _disp(row.get("donor_strategy_url"))),
    ])

    _lo, _hi = _disp(row.get("donor_award_low")), _disp(row.get("donor_award_high"))
    _award = f"{_lo or '—'} – {_hi or '—'}" if (_lo or _hi) else None
    _pb = _disp(row.get("donor_projected_budget"))
    if _pb:
        _per = _disp(row.get("donor_projected_budget_period"))
        _pb = f"{_pb} ({_per})" if _per else _pb
    section("Funding", [
        ("Award range", _award),
        ("Annual funding", _disp(row.get("donor_total_annual_funding_global"))),
        ("Total awards", _disp(row.get("donor_total_awards"))),
        ("Total funding to date", _disp(row.get("donor_total_funding_to_date"))),
        ("Current awards", _disp(row.get("donor_current_awards"))),
        ("Past awards", _disp(row.get("donor_past_awards"))),
        ("Projected budget", _pb),
        ("Funding mechanism", _disp_field(row, "donor_funding_mechanism")),
        (_label("donor_funders_collaborators"), _disp_field(row, "donor_funders_collaborators")),
        (_label("donor_funding_programs"), _disp(row.get("donor_funding_programs"))),
    ])
    _tiers = _funding_tiers(row)
    if _tiers:
        lines.append("Funding tiers / bands")
        lines.extend(_tier_line(t) for t in _tiers)
        lines.append("")

    # Scope & fit — strategic priority areas (graded), geographies, in/out scope.
    _areas_s = _to_list(row.get("donor_priority_areas"))
    try:
        _pa_ratings_s = json.loads(row.get("donor_priority_ratings") or "{}")
        _pa_ratings_s = _pa_ratings_s if isinstance(_pa_ratings_s, dict) else {}
    except (ValueError, TypeError):
        _pa_ratings_s = {}
    _scope_pairs = [(lbl, v) for lbl, v in (
        ("Funding scope — geographies", listf("donor_geographic_scope")),
        (_label("donor_in_scope"), _disp(row.get("donor_in_scope"))),
        (_label("donor_out_of_scope"), _disp(row.get("donor_out_of_scope"))),
    ) if v]
    if _areas_s or _scope_pairs:
        lines.append("Scope & fit")
        for _k, _v in sorted(_pa_ratings_s.items(), key=lambda kv: (-int(kv[1]), kv[0])):
            lines.append(f"- **Priority:** {_pa_sub(_k)} ({_pa_cat(_k)}) — {int(_v)}/5")
        for _a in _areas_s:
            if _a not in _pa_ratings_s:
                lines.append(f"- **Priority area:** {_pa_sub(_a)}")
        for _lbl, _v in _scope_pairs:
            lines.append(f"- **{_lbl}:** {_v}")
        lines.append("")
    section("Eligibility & process", [
        ("Eligibility & routes", flags("Eligibility & routes")),
        ("Requirements & compliance", flags("Requirements & compliance")),
        ("Direct local org eligible", _disp(row.get("donor_direct_local_org_eligible"))),
        ("Route status", _disp(row.get("donor_active_route_status"))),
        ("Prefinance", choice("donor_prefinance_required")),
        ("Application process", _disp(row.get("donor_application_process"))),
        ("Funding cycle", _disp(row.get("donor_funding_cycle"))),
        ("Reporting requirements", _disp(row.get("donor_reporting_requirements"))),
        (_label("donor_application_deadlines"), _disp(row.get("donor_application_deadlines"))),
        (_label("donor_submission_portal_url"), _disp(row.get("donor_submission_portal_url"))),
        ("Recent activity", _disp(row.get("donor_recent_activity"))),
        (_label("donor_eligibility_notes"), _disp(row.get("donor_eligibility_notes"))),
        (_label("donor_selection_criteria"), _disp(row.get("donor_selection_criteria"))),
        (_label("donor_hq_country_required"), listf("donor_hq_country_required")),
        (_label("org_stage_required"), _disp(row.get("org_stage_required"))),
        (_label("donor_max_annual_budget"), _disp_field(row, "donor_max_annual_budget")),
        (_label("donor_min_track_record"), _disp_field(row, "donor_min_track_record")),
        (_label("donor_required_partner_type"), listf("donor_required_partner_type")),
        (_label("donor_required_partner_country"), listf("donor_required_partner_country")),
        (_label("donor_max_request_pct_of_budget"), _disp(row.get("donor_max_request_pct_of_budget"))),
        (_label("donor_min_cofinancing_secured_pct"), _disp(row.get("donor_min_cofinancing_secured_pct"))),
        (_label("donor_entity_type_required"), choice("donor_entity_type_required")),
        (_label("donor_registration_region"), listf("donor_registration_region")),
        (_label("donor_requires_pi"), choice("donor_requires_pi")),
        (_label("donor_pi_country_scope"), choice("donor_pi_country_scope")),
        (_label("donor_max_prior_grant"), _disp_field(row, "donor_max_prior_grant")),
        (_label("donor_prior_beneficiary_rule"), choice("donor_prior_beneficiary_rule")),
    ])

    projects = _past_projects(row)
    if projects:
        lines.append(f"Track record — funded projects ({len(projects)})")
        lines.extend(_project_line(p) for p in projects)
        lines.append("")

    section("Strategic guidance", [
        (_label("donor_strategic_fit_notes"), _disp(row.get("donor_strategic_fit_notes"))),
        (_label("donor_gaps_risks"), _disp(row.get("donor_gaps_risks"))),
        (_label("donor_recommended_approach"), _disp(row.get("donor_recommended_approach"))),
    ])
    section("Sources & data quality", [
        ("Verification", choice("donor_verification_level")),
        ("Evidence summary", _disp(row.get("donor_evidence_summary"))),
        ("Notes", _disp(row.get("notes"))),
        ("Aliases", _disp(row.get("donor_aliases"))),
        ("Source URLs", _disp(row.get("donor_source_urls"))),
        ("Opportunity listing URL(s)", _disp(row.get("donor_opportunity_listing_urls"))),
    ])
    return lines


def _multi_with_options(label, options, current, *, key, help=None) -> list[str]:
    """Multiselect that persists across reruns and accepts typed custom entries.

    A `key` is REQUIRED: without it, Streamlit re-applies `default` every rerun
    and the user's choice is discarded. We seed session_state once, then build
    `options` to INCLUDE the live selection — otherwise a freshly-typed value
    (accept_new_options) isn't in `options` on the next run and gets dropped."""
    if key not in st.session_state:
        st.session_state[key] = _to_list(current)
    opts = list(dict.fromkeys(list(options) + list(st.session_state[key])))
    return st.multiselect(label, opts, key=key, accept_new_options=True, help=help)


def _single_with_other(label, options, current, *, key, help=None):
    """Selectbox with an 'Other (specify)' escape hatch → free-text input.
    Returns the chosen option, or the typed text when 'Other' is picked. Legacy
    free-text values that aren't in `options` preselect 'Other' with that text."""
    cur = (str(current).strip() if current and str(current).strip().lower()
           not in ("nan", "none", "nat") else "")
    base = [""] + list(options) + [_OTHER]
    known = cur in options
    idx = base.index(cur) if known else (base.index(_OTHER) if cur else 0)
    choice = st.selectbox(label, base, index=idx, key=f"{key}_sel", help=help)
    if choice == _OTHER:
        return (st.text_input(f"{label} — specify",
                              value=("" if known else cur),
                              key=f"{key}_other").strip() or None)
    return choice or None


def _yes(v) -> bool:
    return str(v).strip().lower() == "yes"


def _checkbox_matrix(row: dict, *, editable: bool, key_prefix: str,
                     groups: list[str] | None = None) -> dict:
    """Render the flag groups as a multi-column checkbox grid. Returns the
    edited {col: 'yes'|'no'|<original-if-untouched-blank>} when editable.
    `groups` limits rendering to the named flag groups (so the tabbed edit form
    can place each group on its own tab); None = all groups."""
    edited: dict = {}
    for group, cols in _FLAG_GROUPS.items():
        if groups is not None and group not in groups:
            continue
        if not cols:
            continue
        st.markdown(f"**{group}**")
        grid = st.columns(3)
        for i, c in enumerate(cols):
            orig = row.get(c)
            checked = grid[i % 3].checkbox(
                _label(c), value=_yes(orig),
                key=f"{key_prefix}_{c}", disabled=not editable,
            )
            if editable:
                if checked:
                    edited[c] = "yes"
                elif orig in (None, ""):
                    edited[c] = orig          # leave unknown untouched
                else:
                    edited[c] = "no"
    return edited


def _contact_line(cr: dict) -> str:
    """One markdown line for a focal-person contact ($ escaped)."""
    name = _disp(cr.get("contact_name")) or "(unnamed)"
    head = f"**{name}**"
    if _disp(cr.get("role_title")):
        head += f" — {_disp(cr.get('role_title'))}"
    if cr.get("is_official"):
        head += " · ✅ official"
    bits = []
    if _disp(cr.get("email")):
        bits.append(f"✉ {_disp(cr.get('email'))}")
    if _disp(cr.get("phone")):
        bits.append(f"☎ {_disp(cr.get('phone'))}")
    if _disp(cr.get("linkedin_url")):
        ln = _disp(cr.get("linkedin_url"))
        bits.append(f"[LinkedIn]({ln})" if ln.startswith("http") else f"LinkedIn: {ln}")
    if _disp(cr.get("address")):
        bits.append(_disp(cr.get("address")))
    if _disp(cr.get("notes")):
        bits.append(f"_{_disp(cr.get('notes'))}_")
    line = head + ("  \n" + " · ".join(bits) if bits else "")
    return line.replace("$", r"\$")


def _render_contacts(row: dict) -> None:
    """Detail-view contacts block: institutional channels + focal persons."""
    inst = [(c, _disp(row.get(c))) for c in _CONTACT if _disp(row.get(c))]
    contacts = _load_contacts(row["canonical_key"])
    if not inst and contacts.empty:
        return
    st.markdown("**📇 Contacts**")
    if inst:
        for col, v in inst:
            if v.startswith("http"):
                st.markdown(f"- **{_label(col)}:** [{v}]({v})")
            else:
                st.markdown(f"- **{_label(col)}:** {_md(row.get(col)) or v}")
    if not contacts.empty:
        if inst:
            st.caption("Focal persons & additional contacts")
        for _, cr in contacts.iterrows():
            st.markdown("- " + _contact_line(cr.to_dict()))


# ---------------------------------------------------------------------------
# Edit / delete / share dialogs
# ---------------------------------------------------------------------------
@st.dialog("Edit donor", width="large")
def _edit_dialog(row: dict) -> None:
    import html as _html
    _inject_dialog_css()
    # Sanitize pandas NaN → None up front so blank columns render as empty fields
    # in the form (otherwise `nan or ""` keeps the float NaN → shows literal "nan").
    row = {k: (None if _na(v) else v) for k, v in row.items()}
    _ename = _disp(row.get("donor")) or "New donor"
    _eshort = _disp(row.get("donor_short"))
    st.markdown(
        f"<div class='di-stick'><div class='di-stick-title'>{_html.escape(_ename)}"
        + (f" ({_html.escape(_eshort)})" if _eshort else "")
        + "</div><div class='di-stick-sub'>Editing donor intelligence · "
        + "app owners only — keep entries funder-centric & reusable across tenants"
        + "</div></div>", unsafe_allow_html=True)
    edited: dict = {}
    ck = row["canonical_key"]
    (t_id, t_about, t_fund, t_scope, t_elig,
     t_track, t_strat, t_contacts) = st.tabs([
        "🏷 Identity", "📖 About & strategy", "💰 Funding", "🎯 Scope & fit",
        "✅ Eligibility & process", "📚 Track record", "🧭 Strategic guidance",
        "📇 Contacts"])

    # ── Identity ─────────────────────────────────────────────────────────────
    with t_id:
        c1, c2 = st.columns(2)
        edited["donor"] = c1.text_input("Donor", row.get("donor") or "", key=f"donor_{ck}")
        edited["donor_short"] = c2.text_input("Donor Code", row.get("donor_short") or "",
                                              key=f"donor_short_{ck}")
        c3, c4 = st.columns(2)
        _cur_cat = _normalize_category(row.get("donor_category"))
        _cat_opts = list(_CATEGORIES)
        if _cur_cat not in _cat_opts and _cur_cat != "(uncategorised)":
            _cat_opts.append(_cur_cat)
        edited["donor_category"] = c3.selectbox(
            "Donor category", _cat_opts,
            index=(_cat_opts.index(_cur_cat) if _cur_cat in _cat_opts else 0),
            key=f"cat_{ck}")
        edited["donor_website"] = c4.text_input("Website", row.get("donor_website") or "", key=f"website_{ck}")
        # Donor type (descriptive — complements donor_category) + dual-role flag.
        dt1, dt2 = st.columns(2)
        edited["donor_type"] = dt1.text_input(
            _label("donor_type"), row.get("donor_type") or "", key=f"donor_type_{ck}",
            help="Richer descriptive label, e.g. 'Private Foundation & Philanthropic "
                 "Organization'. Complements (doesn't replace) Donor category.")
        _dual_opts = _CHOICE["donor_is_dual_role_implementer"]
        _cur_dual = str(row.get("donor_is_dual_role_implementer") or "").strip().lower()
        edited["donor_is_dual_role_implementer"] = dt2.selectbox(
            _label("donor_is_dual_role_implementer"), _dual_opts,
            index=_dual_opts.index(_cur_dual) if _cur_dual in _dual_opts else 0,
            key=f"dualrole_{ck}",
            help="Yes for implementers (the organisation, PATH, Sightsavers…) that ALSO publish "
                 "their own calls / sub-grants.")
        edited["donor_opportunity_listing_urls"] = st.text_area(
            _label("donor_opportunity_listing_urls"), row.get("donor_opportunity_listing_urls") or "",
            height=60, key=f"oppurls_{ck}",
            help="The donor's RFP / tender / grant LISTING page(s) — pipe-separated. "
                 "These feed the scan catalogue (Sources) via the listing-URL sync.")
        # Founded — year dropdown (append a legacy value not in the list so an
        # existing donor's value is never dropped on save).
        _cur_founded = str(row.get("donor_founded") or "").strip()
        _yr_opts = (_YEAR_OPTIONS if _cur_founded in _YEAR_OPTIONS
                    else _YEAR_OPTIONS + [_cur_founded])
        c5, _c6 = st.columns(2)
        edited["donor_founded"] = c5.selectbox(
            "Founded (year)", _yr_opts, index=_yr_opts.index(_cur_founded),
            key=f"founded_{ck}", help="Year the organisation was established.")
        # Funders & Collaborators — the funders/partners behind or alongside this
        # donor, picked from the shared partner+donor vocabulary (multi-select;
        # type to add). Replaces the old free-text "Parent / funded by".
        edited["donor_funders_collaborators"] = json.dumps(_multi_with_options(
            _label("donor_funders_collaborators"), _PARTNER_OPTIONS,
            row.get("donor_funders_collaborators"), key=f"fundcollab_{ck}",
            help="Who funds / partners with this donor (donors, philanthropies, pooled "
                 "funds, INGOs, …). Same list as the org 'Trusted partners' — type to add "
                 "a private firm or academic institution. If your org is in this list and "
                 "you apply, it lifts your competitiveness."))

        st.markdown("**Official / institutional contact**")
        ic1, ic2 = st.columns(2)
        edited["donor_general_email"] = ic1.text_input("General email", row.get("donor_general_email") or "",
                                                 key=f"general_email_{ck}")
        edited["donor_main_phone"] = ic2.text_input("Main phone", row.get("donor_main_phone") or "",
                                              key=f"main_phone_{ck}")
        ic3, ic4 = st.columns(2)
        _cur_hq = str(row.get("donor_hq_country") or "").strip()
        _hq_opts = (_HQ_COUNTRY_OPTIONS if _cur_hq in _HQ_COUNTRY_OPTIONS
                    else _HQ_COUNTRY_OPTIONS + [_cur_hq])
        edited["donor_hq_country"] = ic3.selectbox("HQ country", _hq_opts,
                                             index=_hq_opts.index(_cur_hq), key=f"hq_country_{ck}")
        edited["donor_linkedin_url"] = ic4.text_input("Donor LinkedIn",
                                                      row.get("donor_linkedin_url") or "",
                                                      key=f"donor_linkedin_url_{ck}")
        edited["donor_hq_address"] = st.text_area("HQ address", row.get("donor_hq_address") or "",
                                            height=60, key=f"hq_address_{ck}")
        edited["donor_other_profile_urls"] = st.text_area("Other profile URLs",
                                                    row.get("donor_other_profile_urls") or "",
                                                    height=60, key=f"other_profile_urls_{ck}")

    # ── About & strategy ─────────────────────────────────────────────────────
    with t_about:
        edited["donor_summary_description"] = st.text_area(
            "Summary / description", row.get("donor_summary_description") or "", height=90,
            key=f"summary_description_{ck}")
        _mv1, _mv2 = st.columns(2)
        edited["donor_mission"] = _mv1.text_area("Mission", row.get("donor_mission") or "", height=80,
                                           key=f"mission_{ck}")
        edited["donor_vision"] = _mv2.text_area("Vision", row.get("donor_vision") or "", height=80,
                                          key=f"vision_{ck}")
        edited["donor_values"] = st.text_area("Values", row.get("donor_values") or "",
                                              height=70, key=f"donor_values_{ck}")
        edited["donor_strategic_priorities"] = st.text_area(
            _label("donor_strategic_priorities"), row.get("donor_strategic_priorities") or "", height=120,
            key=f"strategic_priorities_{ck}",
            help="Current strategic priorities, rotating themes and the period they cover "
                 "(e.g. '2026 theme: AMR; 2026–2030 strategy; 4 Is framework').")
        edited["donor_strategy_url"] = st.text_input(
            "Donor strategy (URL)", row.get("donor_strategy_url") or "", key=f"strategy_url_{ck}",
            help="Link to the donor's published strategy document.")

    # ── Funding ──────────────────────────────────────────────────────────────
    with t_fund:
        st.markdown("**Funding footprint**")
        _ff1, _ff2 = st.columns(2)
        edited["donor_total_awards"] = _ff1.text_input("Total awards", row.get("donor_total_awards") or "",
                                                 key=f"total_awards_{ck}")
        edited["donor_total_funding_to_date"] = _ff2.text_input(
            "Total funding to date (amount)", row.get("donor_total_funding_to_date") or "",
            key=f"total_funding_to_date_{ck}")
        _ff3, _ff4 = st.columns(2)
        edited["donor_current_awards"] = _ff3.text_input("Current / active awards",
                                                   row.get("donor_current_awards") or "",
                                                   key=f"current_awards_{ck}")
        edited["donor_past_awards"] = _ff4.text_input("Past awards", row.get("donor_past_awards") or "",
                                                key=f"past_awards_{ck}")
        _pb1, _pb2 = st.columns([3, 2])
        edited["donor_projected_budget"] = _pb1.text_input(
            "Projected budget / published allocations", row.get("donor_projected_budget") or "",
            key=f"projbud_{ck}", help="The amount, e.g. '200 billion XAF' or '$5M'.")
        edited["donor_projected_budget_period"] = _pb2.text_input(
            "Period / end year", row.get("donor_projected_budget_period") or "",
            key=f"projbudper_{ck}", help="When it applies, e.g. '2024–2030' or 'by 2046'.")

        st.markdown("**Award size & mechanism**")
        fcols = st.columns(2)
        for j, col in enumerate(["donor_award_low", "donor_award_high",
                                 "donor_total_annual_funding_global"]):
            edited[col] = fcols[j % 2].text_input(_label(col), row.get(col) or "", key=f"{col}_{ck}")
        edited["donor_funding_mechanism"] = json.dumps(_multi_with_options(
            "Funding mechanism (type)", FUNDING_MECHANISMS, row.get("donor_funding_mechanism"),
            key=f"fm_{ck}", help="The kind of money — grants, loans, technical assistance, …"))
        edited["donor_funding_programs"] = st.text_area(
            _label("donor_funding_programs"), row.get("donor_funding_programs") or "", height=80,
            key=f"funding_programs_{ck}",
            help="Named schemes / windows, e.g. 'GHR Themed; Global Professorships; Fellowships'.")
        # (Funders & Collaborators is captured on the Identity tab.)

        st.markdown("**Funding tiers / bands / stages**")
        st.caption("One row per band/stage — e.g. NIHR Band 1/2/3 or DIV Stage 1/2/3.")
        try:
            _tiers = json.loads(row.get("donor_funding_tiers_json") or "[]")
            _tiers = _tiers if isinstance(_tiers, list) else []
        except (ValueError, TypeError):
            _tiers = []
        _tier_base = (pd.DataFrame(_tiers) if _tiers else pd.DataFrame()).reindex(
            columns=["name", "amount", "duration", "notes"])
        for _tc in ("name", "amount", "duration", "notes"):
            _tier_base[_tc] = _tier_base[_tc].astype("string")
        _tier_edited = st.data_editor(
            _tier_base, num_rows="dynamic", hide_index=True, width='stretch',
            key=f"tier_ed_{ck}",
            column_config={
                "name": st.column_config.TextColumn("Tier / band / stage"),
                "amount": st.column_config.TextColumn("Amount / ceiling"),
                "duration": st.column_config.TextColumn("Duration"),
                "notes": st.column_config.TextColumn("Notes / criteria", width="large"),
            })
        _tier_recs = []
        for _tr in _tier_edited.to_dict("records"):
            if not _cell(_tr.get("name")):
                continue
            _tier_recs.append({k: _cell(_tr.get(k))
                               for k in ("name", "amount", "duration", "notes")})
        edited["donor_funding_tiers_json"] = json.dumps(_tier_recs)

    # ── Scope & fit ──────────────────────────────────────────────────────────
    with t_scope:
        # Strategic priority areas — ONE matrix (pick area + grade 0–5) on the
        # SHARED taxonomy, identical to the org fit profile so the two correlate
        # into the strategic-fit score. Replaces the cascading dropdowns + the
        # old *_fit checkbox flags.
        _sel, _ratings = program_area_matrix_editor(
            "Strategic priority areas",
            row.get("donor_priority_areas"), row.get("donor_priority_ratings"),
            key_prefix=f"ppa_{ck}",
            help="The donor's funding priorities. Grade 0–5 how central each area is "
                 "to this funder — matched against your org's priorities for strategic fit.")
        edited["donor_priority_areas"] = json.dumps(_sel)
        edited["donor_priority_ratings"] = json.dumps(_ratings)
        st.divider()
        edited["donor_geographic_scope"] = json.dumps(_multi_with_options(
            "Funding scope — geographies (UN regions / tiers / countries)",
            _geo.GEO_OPTIONS, row.get("donor_geographic_scope"),
            key=f"fsg_{ck}",
            help="Where the donor funds. Drives the 'Funds in' filter + coverage view."))
        _sc1, _sc2 = st.columns(2)
        edited["donor_in_scope"] = _sc1.text_area(
            _label("donor_in_scope"), row.get("donor_in_scope") or "", height=170, key=f"in_scope_{ck}",
            help="What the donor DOES fund — eligible activities, study types, topics.")
        edited["donor_out_of_scope"] = _sc2.text_area(
            _label("donor_out_of_scope"), row.get("donor_out_of_scope") or "", height=170,
            key=f"out_of_scope_{ck}", help="What the donor explicitly does NOT fund.")

    # ── Eligibility & process ────────────────────────────────────────────────
    with t_elig:
        edited.update(_checkbox_matrix(
            row, editable=True, key_prefix=f"ed_{ck}",
            groups=["Eligibility & routes", "Requirements & compliance"]))
        st.divider()
        _r1, _r2 = st.columns(2)
        with _r1:
            edited["donor_direct_local_org_eligible"] = _single_with_other(
                "Direct local org eligible", LOCAL_ORG_ELIGIBLE,
                row.get("donor_direct_local_org_eligible"), key=f"dle_{ck}")
        with _r2:
            edited["donor_active_route_status"] = _single_with_other(
                "Route status", ROUTE_STATUSES, row.get("donor_active_route_status"), key=f"rstat_{ck}")
        _pc1, _pc2 = st.columns(2)
        with _pc1:
            _pf_opts = _CHOICE["donor_prefinance_required"]
            _cur_pf = row.get("donor_prefinance_required") or ""
            edited["donor_prefinance_required"] = st.selectbox(
                _label("donor_prefinance_required"), _pf_opts,
                index=_pf_opts.index(_cur_pf) if _cur_pf in _pf_opts else 0,
                key=f"prefinance_required_{ck}", format_func=_pretty_choice)
        with _pc2:
            edited["donor_funding_cycle"] = _single_with_other(
                "Funding cycle / timing", FUNDING_CYCLES, row.get("donor_funding_cycle"),
                key=f"fcyc_{ck}")
        _ap1, _ap2 = st.columns(2)
        with _ap1:
            edited["donor_application_process"] = _single_with_other(
                "Application process", APPLICATION_PROCESSES,
                row.get("donor_application_process"), key=f"appproc_{ck}")
        with _ap2:
            edited["donor_reporting_requirements"] = _single_with_other(
                "Reporting requirements", REPORTING_REQUIREMENTS,
                row.get("donor_reporting_requirements"), key=f"reprq_{ck}")
        _dl1, _dl2 = st.columns(2)
        edited["donor_application_deadlines"] = _dl1.text_input(
            _label("donor_application_deadlines"), row.get("donor_application_deadlines") or "",
            key=f"appdl_{ck}", help="Key dates, e.g. 'Stage 1 outline: 1pm UK, 8 Jul 2026'.")
        edited["donor_submission_portal_url"] = _dl2.text_input(
            _label("donor_submission_portal_url"), row.get("donor_submission_portal_url") or "",
            key=f"portal_{ck}")
        edited["donor_recent_activity"] = st.text_input(
            "Recent activity / last funded", row.get("donor_recent_activity") or "",
            key=f"recent_activity_{ck}", help="Free text — e.g. a year or last-funded note.")
        edited["donor_eligibility_notes"] = st.text_area(
            _label("donor_eligibility_notes"), row.get("donor_eligibility_notes") or "", height=90,
            key=f"elig_notes_{ck}",
            help="Who can be lead / co-applicant, partnership rules, registration constraints.")
        edited["donor_selection_criteria"] = st.text_area(
            _label("donor_selection_criteria"), row.get("donor_selection_criteria") or "", height=120,
            key=f"selcrit_{ck}", help="Evaluation criteria, relative weights, and what wins.")

        # Hard eligibility conditions → computed Qualification (MUST-1). Each is
        # checked ONLY when set here; any condition the applicant fails → ineligible.
        # (independent_entity_required + welcome_registration_required are yes/no and
        # appear as checkboxes in the Requirements & compliance group above.)
        st.divider()
        st.markdown("**Hard eligibility conditions** — feed Qualification (MUST-1). "
                    "Leave blank if the donor doesn't impose them.")
        _hs1, _hs2 = st.columns(2)
        with _hs1:
            edited["donor_hq_country_required"] = json.dumps(_multi_with_options(
                _label("donor_hq_country_required"), ["Any"] + list(_geo.COUNTRIES),
                row.get("donor_hq_country_required"), key=f"hqreq_{ck}",
                help="Countries the applicant may be HQ'd in — pick one or several, or "
                     "'Any' if HQ location is unrestricted. e.g. 'United States' for "
                     "US-501c3-only funders."))
        _stage_opts2 = ["", "any", "early-stage", "established"]
        _cur_st = str(row.get("org_stage_required") or "").strip()
        edited["org_stage_required"] = _hs2.selectbox(
            _label("org_stage_required"), _stage_opts2,
            index=_stage_opts2.index(_cur_st) if _cur_st in _stage_opts2 else 0,
            key=f"stagereq_{ck}", help="e.g. 'early-stage' for DRK-type funders.")
        _hb1, _hb2 = st.columns(2)
        edited["donor_max_annual_budget"] = _hb1.text_input(
            _label("donor_max_annual_budget"), row.get("donor_max_annual_budget") or "",
            key=f"maxbud_{ck}", help="Applicant's annual budget must be BELOW this (e.g. '$2M').")
        edited["donor_min_track_record"] = _hb2.text_input(
            _label("donor_min_track_record"), row.get("donor_min_track_record") or "",
            key=f"mintrk_{ck}", help="Applicant's largest grant must be ABOVE this (e.g. '$500k').")
        _pp1, _pp2 = st.columns(2)
        with _pp1:
            edited["donor_required_partner_type"] = json.dumps(_multi_with_options(
                _label("donor_required_partner_type"), ["Any"] + _PARTNER_TYPE_OPTIONS,
                row.get("donor_required_partner_type"), key=f"reqpt_{ck}",
                help="Required partner profile(s) — pick one or several, or 'Any' "
                     "(e.g. NIHR → Academic / research institutions)."))
        with _pp2:
            edited["donor_required_partner_country"] = json.dumps(_multi_with_options(
                _label("donor_required_partner_country"), ["Any"] + list(_geo.COUNTRIES),
                row.get("donor_required_partner_country"), key=f"reqpc_{ck}",
                help="Required partner country/countries — pick one or several, or "
                     "'Any' (e.g. NIHR → United Kingdom)."))
        _hc1, _hc2 = st.columns(2)
        edited["donor_max_request_pct_of_budget"] = _hc1.text_input(
            _label("donor_max_request_pct_of_budget"), row.get("donor_max_request_pct_of_budget") or "",
            key=f"maxpct_{ck}", help="e.g. '50' — request may be ≤50% of the project budget.")
        edited["donor_min_cofinancing_secured_pct"] = _hc2.text_input(
            _label("donor_min_cofinancing_secured_pct"), row.get("donor_min_cofinancing_secured_pct") or "",
            key=f"mincofin_{ck}", help="e.g. '25' — must have ≥25% secured from other sources.")

        # MUST-1 (Legal status & qualification) rework conditions — migration 049.
        # Each feeds one MUST-1 item; leave blank if the donor doesn't impose it.
        st.divider()
        st.markdown("**MUST-1 identity conditions** (entity type · registration "
                    "region · individual/PI · prior-grant ceiling · prior-beneficiary "
                    "rule). Each feeds a MUST-1 item only when set.")
        # Eligible legal TYPE is captured by the existing NGO / for-profit
        # eligibility checkboxes above (Eligibility & routes) — not duplicated here.
        _ent_opts = ["", "grassroot_local", "multi_country", "individual"]
        _cur_ent = str(row.get("donor_entity_type_required") or "").strip()
        _req_pi_opts = ["", "yes", "no"]
        _cur_rpi = str(row.get("donor_requires_pi") or "").strip().lower()
        _et1, _et2 = st.columns(2)
        edited["donor_entity_type_required"] = _et1.selectbox(
            _label("donor_entity_type_required"), _ent_opts,
            index=_ent_opts.index(_cur_ent) if _cur_ent in _ent_opts else 0,
            format_func=_pretty_choice, key=f"entreq_{ck}",
            help="Requires a grassroots/local vs multi-country vs individual applicant. "
                 "(Distinct from the broader 'multi-country scope' funding flag.)")
        edited["donor_requires_pi"] = _et2.selectbox(
            _label("donor_requires_pi"), _req_pi_opts,
            index=_req_pi_opts.index(_cur_rpi) if _cur_rpi in _req_pi_opts else 0,
            format_func=_pretty_choice, key=f"reqpi_{ck}",
            help="Does the call require a named individual / Principal Investigator "
                 "(vs an organisation)?")
        _broad_geo = list(getattr(_geo, "BROAD_GEOGRAPHIES", None)
                          or (list(getattr(_geo, "UN_REGIONS", []))
                              + list(getattr(_geo, "INCOME_TIERS", []))))
        _pi_scope_opts = ["", "donor_in_scope", "foreign"]
        _cur_pis = str(row.get("donor_pi_country_scope") or "").strip()
        _rr1, _rr2 = st.columns(2)
        with _rr1:
            edited["donor_registration_region"] = json.dumps(_multi_with_options(
                _label("donor_registration_region"),
                ["Any"] + _broad_geo + list(_geo.COUNTRIES),
                row.get("donor_registration_region"), key=f"regreg_{ck}",
                help="Where the applicant must be REGISTERED — pick broad terms (LMIC, "
                     "Sub-Saharan Africa, …) and/or specific countries, or 'Any'. Same "
                     "vocabulary as the org fit profile; blank → falls back to the call's "
                     "geographic scope, matched against the org's Countries registered / "
                     "operation."))
        edited["donor_pi_country_scope"] = _rr2.selectbox(
            _label("donor_pi_country_scope"), _pi_scope_opts,
            index=_pi_scope_opts.index(_cur_pis) if _cur_pis in _pi_scope_opts else 0,
            format_func=_pretty_choice, key=f"piscope_{ck}",
            help="in_scope = PI based in the implementation country (our own "
                 "well-established PI qualifies); foreign = PI required in the donor / "
                 "an OECD country (met via an affiliated partner).")
        _ps1, _ps2 = st.columns(2)
        edited["donor_max_prior_grant"] = _ps1.text_input(
            _label("donor_max_prior_grant"), row.get("donor_max_prior_grant") or "",
            key=f"maxpg_{ck}",
            help="Applicant ineligible if its LARGEST prior grant EXCEEDS this "
                 "(e.g. '500000'). A CEILING — distinct from the track-record floor.")
        _pbr_opts = ["", "eligible", "ineligible_current", "ineligible_previous",
                     "ineligible_any"]
        _cur_pbr = str(row.get("donor_prior_beneficiary_rule") or "").strip()
        edited["donor_prior_beneficiary_rule"] = _ps2.selectbox(
            _label("donor_prior_beneficiary_rule"), _pbr_opts,
            index=_pbr_opts.index(_cur_pbr) if _cur_pbr in _pbr_opts else 0,
            format_func=_pretty_choice, key=f"pbr_{ck}",
            help="eligible = prior grantees explicitly welcome (no penalty); "
                 "ineligible_current = current grantees barred; ineligible_previous = "
                 "past grantees barred; ineligible_any = both.")

    # ── Track record — funded projects (JSON: past_projects_json) ────────────
    with t_track:
        st.caption("Funded projects — title, amount, year, country, stage, short "
                   "description, link. Add rows freely.")
        try:
            _proj = json.loads(row.get("donor_past_projects_json") or "[]")
            _proj = _proj if isinstance(_proj, list) else []
        except (ValueError, TypeError):
            _proj = []
        _proj_cols = ["title", "amount", "currency", "year", "country", "stage",
                      "description", "link"]
        _proj_base = (pd.DataFrame(_proj) if _proj else pd.DataFrame()).reindex(columns=_proj_cols)
        # Enforce dtypes so they match the column_config (a NumberColumn over an
        # object/empty column raises StreamlitAPIException at render time).
        _proj_base["amount"] = pd.to_numeric(_proj_base["amount"], errors="coerce")
        for _tc in ("title", "currency", "year", "country", "stage", "description", "link"):
            _proj_base[_tc] = _proj_base[_tc].astype("string")
        _proj_edited = st.data_editor(
            _proj_base, num_rows="dynamic", hide_index=True, width='stretch',
            key=f"proj_ed_{ck}",
            column_config={
                "title": st.column_config.TextColumn("Project title", width="medium"),
                "amount": st.column_config.NumberColumn("Award amount", format="%.0f"),
                "currency": st.column_config.TextColumn("Currency", default="USD"),
                "year": st.column_config.TextColumn("Year"),
                "country": st.column_config.TextColumn("Country"),
                "stage": st.column_config.TextColumn("Stage / band"),
                "description": st.column_config.TextColumn("Description", width="large"),
                "link": st.column_config.LinkColumn("Link"),
            })
        _proj_recs = []
        for _pr in _proj_edited.to_dict("records"):
            if not _cell(_pr.get("title")):
                continue
            _amt = _pr.get("amount")
            try:
                _amt = None if (_amt is None or pd.isna(_amt)) else float(_amt)
            except (TypeError, ValueError):
                _amt = None
            _proj_recs.append({
                "title": _cell(_pr.get("title")),
                "amount": _amt,
                "currency": _cell(_pr.get("currency")),
                "year": _cell(_pr.get("year")),
                "country": _cell(_pr.get("country")),
                "stage": _cell(_pr.get("stage")),
                "description": _cell(_pr.get("description")),
                "link": _cell(_pr.get("link")),
            })
        edited["donor_past_projects_json"] = json.dumps(_proj_recs)

    # ── Strategic guidance — funder-centric, reusable across tenants ─────────
    with t_strat:
        st.caption("Funder-centric guidance any applicant institution can use — "
                   "describe the **donor**, not one organisation. Keep it generic "
                   "(no tenant names) but specific enough to be actionable.")
        edited["donor_strategic_fit_notes"] = st.text_area(
            _label("donor_strategic_fit_notes"), row.get("donor_strategic_fit_notes") or "", height=120,
            key=f"stratfit_{ck}",
            help="The applicant profile this funder rewards and the strengths that win "
                 "(e.g. 'LMIC-led teams with government demand and prior evidence').")
        edited["donor_gaps_risks"] = st.text_area(
            _label("donor_gaps_risks"), row.get("donor_gaps_risks") or "", height=120, key=f"gaps_{ck}",
            help="Common reasons applications fail / are disqualified — generic to any "
                 "applicant (e.g. 'service delivery framed as research', 'no causal design').")
        edited["donor_recommended_approach"] = st.text_area(
            _label("donor_recommended_approach"), row.get("donor_recommended_approach") or "", height=120,
            key=f"recapp_{ck}",
            help="How to position a strong application — recommended tier/band, framing "
                 "and sequencing — usable by any institution.")
        st.divider()
        _vc1, _vc2 = st.columns([1, 2])
        with _vc1:
            _vl_opts = _CHOICE["donor_verification_level"]
            _cur_vl = row.get("donor_verification_level") or ""
            edited["donor_verification_level"] = st.selectbox(
                _label("donor_verification_level"), _vl_opts,
                index=_vl_opts.index(_cur_vl) if _cur_vl in _vl_opts else 0,
                key=f"verification_level_{ck}", format_func=_pretty_choice)
        edited["donor_evidence_summary"] = st.text_area("Evidence summary",
                                                  row.get("donor_evidence_summary") or "", height=80,
                                                  key=f"evidence_summary_{ck}")
        edited["notes"] = st.text_area("Notes", row.get("notes") or "", height=70,
                                       key=f"notes_{ck}")
        _ax1, _ax2 = st.columns(2)
        edited["donor_aliases"] = _ax1.text_area("Aliases", row.get("donor_aliases") or "", height=70,
                                           key=f"aliases_{ck}")
        edited["donor_source_urls"] = _ax2.text_area("Source URLs", row.get("donor_source_urls") or "",
                                               height=70, key=f"source_urls_{ck}")

    # ── Contacts — focal persons & additional (private) ──────────────────────
    with t_contacts:
        st.caption("Add as many as you like (＋ row). Official channels or people the "
                   "team has engaged. Sourced from public pages or first-party — never guessed.")
        _existing = _load_contacts(row["canonical_key"])
        if _existing.empty:
            _base = pd.DataFrame({c: pd.Series(dtype=("bool" if c == "is_official" else "object"))
                                  for c in _CONTACT_COLS})
        else:
            _base = _existing.reindex(columns=_CONTACT_COLS)
            _base["is_official"] = _base["is_official"].fillna(False).astype(bool)
        contacts_edited = st.data_editor(
            _base, num_rows="dynamic", width='stretch', hide_index=True,
            key=f"contacts_ed_{ck}",
            column_config={
                "contact_name": st.column_config.TextColumn("Name"),
                "role_title": st.column_config.TextColumn("Role / title"),
                "email": st.column_config.TextColumn("Email"),
                "phone": st.column_config.TextColumn("Phone"),
                "linkedin_url": st.column_config.TextColumn("LinkedIn URL"),
                "address": st.column_config.TextColumn("Address"),
                "is_official": st.column_config.CheckboxColumn("Official?", default=False),
                "notes": st.column_config.TextColumn("Notes"),
            },
        )

    if st.button("💾 Save changes", type="primary", width='stretch'):
        payload = {k: (v.strip() if isinstance(v, str) else v) or None
                   for k, v in edited.items()}
        key = row["canonical_key"]
        payload["canonical_key"] = key
        # Normalize the category to its canonical form on the way in.
        if payload.get("donor_category"):
            payload["donor_category"] = _normalize_category(payload["donor_category"])
        # Only persist keys that are real columns. Anything the user filled that
        # has no column yet (e.g. the profile fields before migration 025) would
        # be DROPPED silently — so we detect those and warn, instead of letting
        # the save look successful while their input quietly disappears.
        _cols = set(df.columns) | {"canonical_key"}
        _dropped = sorted(_label(k) for k, v in payload.items()
                          if k not in _cols and v not in (None, ""))
        payload = {k: v for k, v in payload.items() if k in _cols}

        # Reconcile contacts: replace this donor's set with the edited rows.
        recs = []
        for _, cr in contacts_edited.iterrows():
            rec: dict = {}
            for c in _CONTACT_COLS:
                if c == "is_official":
                    rec[c] = bool(cr.get(c))
                    continue
                v = cr.get(c)
                try:
                    blank = v is None or pd.isna(v)
                except (TypeError, ValueError):
                    blank = v is None
                rec[c] = (str(v).strip() or None) if not blank else None
            if any(rec[c] for c in _CONTACT_COLS if c != "is_official"):
                rec["canonical_key"] = key
                recs.append(rec)

        # Persist the donor row. We VERIFY the write landed (PostgREST returns
        # the upserted row) rather than trusting a silent 200 — an RLS policy or
        # phantom column can make an upsert a no-op with no exception, which is
        # exactly the "looks saved but nothing changed" symptom. Contacts are
        # reconciled separately so a contacts hiccup can't block the donor save.
        try:
            resp = (sb.table("donor_intel")
                    .upsert(payload, on_conflict="canonical_key")
                    .execute())
        except Exception as e:  # noqa: BLE001 — show the real DB error to the user
            st.error(f"❌ Save failed — donor not updated.\n\n`{e}`")
            return
        if not getattr(resp, "data", None):
            st.error("❌ Save returned no row — the write was blocked (likely an "
                     "RLS policy or a missing column). Nothing was changed.")
            return

        _contact_warn = None
        try:
            sb.table("donor_contacts").delete().eq("canonical_key", key).execute()
            if recs:
                sb.table("donor_contacts").insert(recs).execute()
        except Exception as e:  # noqa: BLE001 — donor saved; contacts didn't
            _contact_warn = f"Donor saved, but contacts couldn't be updated: {e}"

        st.cache_data.clear()
        st.session_state["_donor_flash"] = f"✓ Saved changes to {row.get('donor')}."
        _warns = []
        if _dropped:
            _warns.append(
                "These fields aren't stored yet because the database is missing "
                "their columns (the deployed app and the DB are on different "
                "data-model migrations). Apply the latest `db/migrations/` in Supabase "
                "AND deploy the matching code, then re-save: " + ", ".join(_dropped))
        if _contact_warn:
            _warns.append(_contact_warn)
        if _warns:
            st.session_state["_donor_flash_warn"] = "  \n\n".join(_warns)
        st.toast(f"Saved {row.get('donor')}", icon="✅")
        st.rerun()


@st.dialog("Delete donor")
def _delete_dialog(row: dict) -> None:
    st.warning(f"Delete **{row.get('donor')}** from the matrix? This can't be undone.")
    if st.button("🗑 Yes, delete", type="primary"):
        sb.table("donor_intel").delete().eq("canonical_key", row["canonical_key"]).execute()
        st.cache_data.clear()
        st.session_state["_donor_flash"] = f"Deleted {row.get('donor')}."
        st.rerun()


@st.dialog("Add a new donor")
def _add_donor_dialog() -> None:
    """Create a donor record from a name (+ a few basics), then the user fills
    in the full intelligence via ✏️ Edit. canonical_key is slugged from the name
    and de-duplicated so it never collides with an existing donor."""
    st.caption("Create the record now; open it and click **✏️ Edit** to add the "
               "full profile (scope, awards, contacts, …).")
    name = st.text_input("Donor name *", key="add_donor_name")
    c1, c2 = st.columns(2)
    short = c1.text_input("Short code / acronym", key="add_donor_short")
    cat = c2.selectbox("Category", _CATEGORIES, key="add_donor_cat")
    website = st.text_input("Website", key="add_donor_web",
                            placeholder="https://…")
    b1, b2 = st.columns(2)
    if b1.button("➕ Create donor", type="primary", width="stretch",
                 disabled=not name.strip()):
        _slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "donor"
        _existing = set(df["canonical_key"]) if "canonical_key" in df.columns else set()
        _key, _i = _slug, 2
        while _key in _existing:
            _key, _i = f"{_slug}_{_i}", _i + 1
        payload = {"canonical_key": _key, "donor": name.strip(),
                   "donor_short": (short.strip() or None),
                   "donor_category": _normalize_category(cat),
                   "donor_website": (website.strip() or None)}
        try:
            resp = sb.table("donor_intel").upsert(
                payload, on_conflict="canonical_key").execute()
        except Exception as e:  # noqa: BLE001
            st.error(f"❌ Create failed — {e}")
            return
        if not getattr(resp, "data", None):
            st.error("❌ Create returned no row — the write was blocked "
                     "(RLS / columns). Nothing was added.")
            return
        st.cache_data.clear()
        st.session_state["_donor_flash"] = (
            f"✓ Added {name.strip()} — find it in the table and click ✏️ Edit "
            "to complete the profile.")
        st.rerun()
    if b2.button("Cancel", width="stretch"):
        st.rerun()


def _donor_pdf(lines: list) -> bytes:
    """Render the share summary to a branded PDF: the deploying org's logo +
    name/team at the top, the donor intelligence body, and an RFPIS footer on
    every page. reportlab base fonts are WinAnsi, so emoji/symbols are swapped
    for ASCII and any other non-cp1252 glyph is dropped."""
    import io
    import re as _re
    from datetime import date as _date
    from xml.sax.saxutils import escape as _esc
    from reportlab.lib import colors as _colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (
        HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer,
    )

    def _safe(s: str) -> str:
        for a, b in (("✅", "[official]"), ("📇", ""), ("✉", "Email:"),
                     ("☎", "Phone:")):
            s = s.replace(a, b)
        return s.encode("cp1252", "ignore").decode("cp1252")

    def _inline(s: str) -> str:
        parts = _re.split(r"\*\*(.+?)\*\*", s)
        return "".join(f"<b>{_esc(_safe(p))}</b>" if i % 2 else _esc(_safe(p))
                       for i, p in enumerate(parts))

    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Title"], fontSize=16, spaceBefore=4, spaceAfter=12)
    head = ParagraphStyle("head", parent=base["Heading4"], spaceBefore=8, spaceAfter=2)
    body = ParagraphStyle("body", parent=base["BodyText"], fontSize=9.5, leading=13)
    bullet = ParagraphStyle("bul", parent=body, leftIndent=12)
    org_name_st = ParagraphStyle("orgname", parent=base["Normal"], alignment=TA_CENTER,
                                 fontSize=12, leading=15, spaceAfter=1,
                                 textColor=_colors.HexColor("#1e3a8a"))
    org_team_st = ParagraphStyle("orgteam", parent=base["Normal"], alignment=TA_CENTER,
                                 fontSize=9.5, leading=12, spaceAfter=8,
                                 textColor=_colors.HexColor("#475569"))

    # --- Org branding header (logo + name + team) ---
    flow: list = []
    try:
        from core import settings as _s
        _org = _s.get_org()
        _logo_bytes, _ = _s.get_org_logo()
    except Exception:
        _org, _logo_bytes = {}, None
    if _logo_bytes:
        try:
            _ir = ImageReader(io.BytesIO(_logo_bytes))
            _iw, _ih = _ir.getSize()
            _maxh = 46.0
            _img = Image(io.BytesIO(_logo_bytes), width=_iw * (_maxh / _ih), height=_maxh)
            _img.hAlign = "CENTER"
            flow.append(_img)
            flow.append(Spacer(1, 5))
        except Exception:
            pass  # unrenderable (e.g. SVG/WebP) — skip the logo gracefully
    _oname = _safe(str(_org.get("org_name") or "")).strip()
    if _oname and _oname.lower() != "your organization":
        flow.append(Paragraph(f"<b>{_esc(_oname)}</b>", org_name_st))
    # "Donor Intelligence" eyebrow under the org name (replaces the redundant
    # team line — org_name already carries the team); the donor name then
    # stands alone as the title below.
    flow.append(Paragraph("Donor Intelligence", org_team_st))
    flow.append(HRFlowable(width="100%", thickness=0.6,
                           color=_colors.HexColor("#cbd5e1"), spaceAfter=8))

    # --- Donor intelligence body (from the share markdown lines) ---
    for ln in lines:
        if not ln.strip():
            flow.append(Spacer(1, 4))
        elif ln.startswith("# "):
            flow.append(Paragraph(_inline(ln[2:]), h1))
        elif ln.startswith("- "):
            flow.append(Paragraph("&bull;&nbsp;" + _inline(ln[2:]), bullet))
        else:
            flow.append(Paragraph(_inline(ln), head))

    # --- RFPIS footer on every page ---
    def _footer(canvas, doc):
        canvas.saveState()
        w, _h = A4
        canvas.setStrokeColor(_colors.HexColor("#e2e8f0"))
        canvas.setLineWidth(0.5)
        canvas.line(1.8 * cm, 1.35 * cm, w - 1.8 * cm, 1.35 * cm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(_colors.HexColor("#94a3b8"))
        canvas.drawCentredString(
            w / 2.0, 1.0 * cm,
            "RFP Intelligence System (RFPIS) · Version 1 · All Rights Reserved.")
        canvas.drawString(1.8 * cm, 1.0 * cm, f"Generated {_date.today().isoformat()}")
        canvas.drawRightString(w - 1.8 * cm, 1.0 * cm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        topMargin=1.5 * cm, bottomMargin=1.9 * cm, title="Donor Intelligence",
    ).build(flow, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


@st.dialog("Share donor", width="large")
def _share_dialog(row: dict) -> None:
    st.markdown(f"### {row.get('donor')}")
    lines = _summary_lines(row)
    _contacts = _load_contacts(row["canonical_key"])
    if not _contacts.empty:
        lines.append("Contacts — focal persons")
        for _, cr in _contacts.iterrows():
            lines.append("- " + _contact_line(cr.to_dict()).replace("  \n", " — ").replace(r"\$", "$"))
        lines.append("")
    md = "\n".join(lines)
    st.caption("Copy this summary, or download the full PDF.")
    st.code(md, language="markdown")
    fname = (row.get("donor_short") or row.get("donor") or "donor").strip().replace(" ", "_")
    try:
        st.download_button(
            "⬇ Download PDF", _donor_pdf(lines),
            file_name=f"{fname}.pdf", mime="application/pdf", type="primary",
        )
    except Exception as e:  # reportlab missing / render error — fall back to .md
        st.download_button(
            "⬇ Download (.md)", md, file_name=f"{fname}.md", mime="text/markdown",
        )
        st.caption(f"PDF unavailable ({e}); markdown offered instead.")


@st.dialog("Donor details", width="large")
def _view_dialog(row: dict) -> None:
    """Read-only full intelligence for one donor, shown as a pop-up. Edit /
    Delete are the row-action buttons under the table (Streamlit can't open a
    second dialog from inside this one); Share is offered inline as a download."""
    def _chips(labels: list[str]) -> str:
        return " ".join(
            "<span style='display:inline-block;background:#e6f2eb;color:#00703C;"
            "padding:2px 11px;border-radius:12px;margin:0 5px 7px 0;"
            f"font-size:0.85rem;'>{lab}</span>" for lab in labels)

    import html as _html
    _inject_dialog_css()

    # ── Sticky header — name + subtitle + data-completeness badge ────────────
    _name = _disp(row.get("donor")) or "—"
    _short = _disp(row.get("donor_short"))
    _title_txt = _name + (f" ({_short})" if _short and _short.lower() not in _name.lower() else "")
    _subparts = []
    _cat = _normalize_category(row.get("donor_category"))
    if _cat and _cat != "(uncategorised)":
        _subparts.append(_html.escape(_cat))
    if _disp(row.get("donor_founded")):
        _subparts.append(f"Founded {_html.escape(_disp(row.get('donor_founded')))}")
    _hdr_funders = _to_list(row.get("donor_funders_collaborators"))
    if _hdr_funders:
        _shown = ", ".join(_hdr_funders[:3]) + ("…" if len(_hdr_funders) > 3 else "")
        _subparts.append("Partnered with " + _html.escape(_shown))
    if _disp(row.get("donor_website")):
        _w = _disp(row.get("donor_website"))
        _wh = _w if _w.startswith(("http://", "https://")) else f"https://{_w}"
        _subparts.append(f"<a href='{_html.escape(_wh)}' target='_blank'>{_html.escape(_w)}</a>")
    _pct, _have, _total = _completeness(row)
    _band_lbl, _band_col = _completeness_band(_pct)
    st.markdown(
        f"<div class='di-stick'><div class='di-stick-title'>{_html.escape(_title_txt)}</div>"
        + (f"<div class='di-stick-sub'>{' · '.join(_subparts)}</div>" if _subparts else "")
        + f"<span class='di-badge' style='background:{_band_col}'>"
        + f"Data completeness {_pct}% · {_band_lbl} ({_have}/{_total} fields)</span></div>",
        unsafe_allow_html=True)
    if row.get("donor_verification_level") == "low":
        st.warning("⚠ Low verification — confirm against the live call package.")

    # ── 🏷 Identity — official / institutional contact ───────────────────────
    _inst = [(_label(c), c) for c in
             ("donor_general_email", "donor_main_phone", "donor_hq_country", "donor_hq_address",
              "donor_linkedin_url", "donor_other_profile_urls") if _disp(row.get(c))]
    if _inst:
        with st.container(border=True):
            _sec_header("🏷", "Identity")
            for _lbl, _c in _inst:
                _v = _disp(row.get(_c))
                st.markdown(f"- **{_lbl}:** [{_v}]({_v})" if _v.startswith("http")
                            else f"- **{_lbl}:** {_v}")

    # ── 📖 About & strategy ──────────────────────────────────────────────────
    _about = [(lbl, col) for col, lbl in (
        ("donor_summary_description", "Summary"), ("donor_mission", "Mission"),
        ("donor_vision", "Vision"), ("donor_values", "Values"),
        ("donor_strategic_priorities", _label("donor_strategic_priorities")))
        if _disp(row.get(col))]
    _su = _disp(row.get("donor_strategy_url"))
    if _about or _su:
        with st.container(border=True):
            _sec_header("📖", "About & strategy")
            for _lbl, _col in _about:
                _sub(_lbl)
                st.markdown(_disp(row.get(_col)))
            if _su:
                st.markdown(f"**Strategy:** [{_su}]({_su})")

    # ── 💰 Funding — footprint facts + mechanism / programs / tiers ──────────
    facts: list[tuple[str, str]] = []
    lo, hi = _md(row.get("donor_award_low")), _md(row.get("donor_award_high"))
    if lo or hi:
        facts.append(("Award range", f"{lo or '—'} – {hi or '—'}"))
    for col, lbl in (("donor_total_annual_funding_global", "Annual funding"),
                     ("donor_total_funding_to_date", "Total funding"),
                     ("donor_total_awards", "Total awards"),
                     ("donor_current_awards", "Current awards"),
                     ("donor_past_awards", "Past awards"),
                     ("donor_projected_budget", "Projected budget")):
        v = _md(row.get(col)) or _disp(row.get(col))
        if col == "donor_projected_budget" and v:
            _per = _disp(row.get("donor_projected_budget_period"))
            if _per:
                v = f"{v} ({_per})"
        if v:
            facts.append((lbl, v))
    _mech = _to_list(row.get("donor_funding_mechanism"))
    _programs = _disp(row.get("donor_funding_programs"))
    _funders = _to_list(row.get("donor_funders_collaborators"))
    _tiers = _funding_tiers(row)
    if facts or _mech or _programs or _funders or _tiers:
        with st.container(border=True):
            _sec_header("💰", "Funding")
            if facts:
                _fcols = st.columns(min(len(facts), 4))
                for _i, (_lbl, _v) in enumerate(facts):
                    _fcols[_i % len(_fcols)].markdown(f"**{_lbl}**  \n{_v}")
            if _mech:
                _sub("Funding mechanism")
                st.markdown(_chips(_mech), unsafe_allow_html=True)
            if _funders:
                _sub(_label("donor_funders_collaborators"))
                st.markdown(_chips(_funders), unsafe_allow_html=True)
            if _programs:
                _sub(_label("donor_funding_programs"))
                st.markdown(_programs)
            if _tiers:
                _sub("Funding tiers / bands / stages")
                _tdf = pd.DataFrame(_tiers).reindex(
                    columns=["name", "amount", "duration", "notes"])
                st.dataframe(
                    _tdf.rename(columns={"name": "Tier / band / stage", "amount": "Amount",
                                         "duration": "Duration", "notes": "Notes / criteria"}),
                    hide_index=True, width='stretch')

    # ── 🎯 Scope & fit — strategic priority areas (graded) / geo / in-out ────
    _scope = _to_list(row.get("donor_geographic_scope"))
    _areas = _to_list(row.get("donor_priority_areas"))
    _in_scope = _disp(row.get("donor_in_scope"))
    _out_scope = _disp(row.get("donor_out_of_scope"))
    try:
        _pa_ratings = json.loads(row.get("donor_priority_ratings") or "{}")
        _pa_ratings = _pa_ratings if isinstance(_pa_ratings, dict) else {}
    except (ValueError, TypeError):
        _pa_ratings = {}
    _bars = rating_bars_html(_pa_ratings)
    _ungraded = [a for a in _areas if a not in _pa_ratings]
    if _scope or _areas or _in_scope or _out_scope:
        with st.container(border=True):
            _sec_header("🎯", "Scope & fit")
            if _bars or _ungraded:
                _sub("Strategic priority areas")
                if _bars:
                    st.markdown(_bars, unsafe_allow_html=True)
                if _ungraded:
                    st.markdown(_chips([_pa_sub(a) for a in _ungraded]),
                                unsafe_allow_html=True)
            if _scope:
                _sub("Funding scope — geographies")
                st.markdown(_chips(_scope), unsafe_allow_html=True)
            if _in_scope or _out_scope:
                _sc1, _sc2 = st.columns(2)
                if _in_scope:
                    _sc1.markdown(f"**✓ {_label('donor_in_scope')}**")
                    _sc1.markdown(_in_scope)
                if _out_scope:
                    _sc2.markdown(f"**✗ {_label('donor_out_of_scope')}**")
                    _sc2.markdown(_out_scope)

    # ── ✅ Eligibility & process — routes/requirements flags + logistics ─────
    _elig_kv = []
    for _lbl, _col in (("Route status", "donor_active_route_status"),
                       ("Direct local org eligible", "donor_direct_local_org_eligible"),
                       ("Prefinance", "donor_prefinance_required"),
                       ("Application process", "donor_application_process"),
                       ("Funding cycle", "donor_funding_cycle"),
                       ("Reporting requirements", "donor_reporting_requirements"),
                       (_label("donor_application_deadlines"), "donor_application_deadlines"),
                       (_label("donor_submission_portal_url"), "donor_submission_portal_url"),
                       ("Recent activity", "donor_recent_activity"),
                       (_label("donor_hq_country_required"), "donor_hq_country_required"),
                       (_label("org_stage_required"), "org_stage_required"),
                       (_label("donor_max_annual_budget"), "donor_max_annual_budget"),
                       (_label("donor_min_track_record"), "donor_min_track_record"),
                       (_label("donor_required_partner_type"), "donor_required_partner_type"),
                       (_label("donor_required_partner_country"), "donor_required_partner_country"),
                       (_label("donor_max_request_pct_of_budget"), "donor_max_request_pct_of_budget"),
                       (_label("donor_min_cofinancing_secured_pct"), "donor_min_cofinancing_secured_pct"),
                       (_label("donor_entity_type_required"), "donor_entity_type_required"),
                       (_label("donor_registration_region"), "donor_registration_region"),
                       (_label("donor_requires_pi"), "donor_requires_pi"),
                       (_label("donor_pi_country_scope"), "donor_pi_country_scope"),
                       (_label("donor_max_prior_grant"), "donor_max_prior_grant"),
                       (_label("donor_prior_beneficiary_rule"), "donor_prior_beneficiary_rule")):
        if _col in _LIST_FIELDS:
            _vals = _to_list(row.get(_col))
            v = ", ".join(_vals) if _vals else None
        else:
            v = _md(row.get(_col)) or _disp(row.get(_col))
            if v and _col in ("donor_prefinance_required", "donor_entity_type_required", "donor_requires_pi",
                              "donor_pi_country_scope", "donor_prior_beneficiary_rule"):
                v = _pretty_choice(v)
        if v:
            _elig_kv.append((_lbl, _col, v))
    _elig_flags = {g: [_label(c) for c in _FLAG_GROUPS.get(g, []) if _yes(row.get(c))]
                   for g in ("Eligibility & routes", "Requirements & compliance")}
    _elig_notes = _disp(row.get("donor_eligibility_notes"))
    _sel = _disp(row.get("donor_selection_criteria"))
    if any(_elig_flags.values()) or _elig_kv or _elig_notes or _sel:
        with st.container(border=True):
            _sec_header("✅", "Eligibility & process")
            for _g, _labs in _elig_flags.items():
                if _labs:
                    _sub(_g)
                    st.markdown(_chips(_labs), unsafe_allow_html=True)
            if _elig_kv:
                _sub("Application logistics")
                for _lbl, _col, v in _elig_kv:
                    if _col == "donor_submission_portal_url" and v.startswith("http"):
                        st.markdown(f"- **{_lbl}:** [{v}]({v})")
                    else:
                        st.markdown(f"- **{_lbl}:** {v}")
            if _elig_notes:
                _sub(_label("donor_eligibility_notes"))
                st.markdown(_elig_notes)
            if _sel:
                _sub(_label("donor_selection_criteria"))
                st.markdown(_sel)

    # ── 📚 Track record — funded projects (incl. stage + description) ────────
    _projects = _past_projects(row)
    if _projects:
        with st.container(border=True):
            _sec_header("📚", f"Track record — funded projects ({len(_projects)})")
            _pdf = pd.DataFrame(_projects)
            _show = [c for c in ["title", "amount", "currency", "year", "country",
                                 "stage", "description", "link"]
                     if c in _pdf.columns and _pdf[c].notna().any()]
            st.dataframe(
                _pdf[_show].rename(columns={
                    "title": "Project", "amount": "Award", "currency": "Cur.",
                    "year": "Year", "country": "Country", "stage": "Stage",
                    "description": "Description", "link": "Link"}),
                hide_index=True, width='stretch',
                column_config={"Link": st.column_config.LinkColumn("Link", display_text="Open ↗")})

    # ── 🧭 Strategic guidance — funder-centric, useful to any applicant ──────
    _guide = [(_label(col), _disp(row.get(col))) for col in
              ("donor_strategic_fit_notes", "donor_gaps_risks", "donor_recommended_approach")
              if _disp(row.get(col))]
    if _guide:
        with st.container(border=True):
            _sec_header("🧭", "Strategic guidance")
            st.caption("Funder-centric guidance for any applicant — what this donor "
                       "rewards, what to avoid, and how to position a strong bid.")
            for _lbl, _v in _guide:
                _sub(_lbl)
                st.markdown(_v)

    # ── 📇 Contacts — focal persons (donor_contacts) ─────────────────────────
    _focal = _load_contacts(row["canonical_key"])
    if not _focal.empty:
        with st.container(border=True):
            _sec_header("📇", "Contacts — focal persons")
            for _, cr in _focal.iterrows():
                st.markdown("- " + _contact_line(cr.to_dict()))

    # ── Sources & data quality ───────────────────────────────────────────────
    _ver = _pretty_choice(row.get("donor_verification_level")) if _disp(row.get("donor_verification_level")) else None
    _ev = _disp(row.get("donor_evidence_summary"))
    _notes = _disp(row.get("notes"))
    _alias = _disp(row.get("donor_aliases"))
    _urls = _disp(row.get("donor_source_urls"))
    if _ver or _ev or _notes or _alias or _urls:
        with st.container(border=True):
            _sec_header("🗂", "Sources & data quality")
            st.markdown(f"- **Profile completeness:** {_pct}% ({_have}/{_total} fields)")
            if _ver:
                st.markdown(f"- **Verification (manual):** {_ver}")
            if _ev:
                st.markdown(f"- **Evidence summary:** {_md(row.get('donor_evidence_summary')) or _ev}")
            if _notes:
                st.markdown(f"- **Notes:** {_md(row.get('notes')) or _notes}")
            if _alias:
                st.markdown(f"- **Aliases:** {_alias}")
            if _urls:
                _sub("Source URLs")
                for u in _urls.replace("\\n", "\n").splitlines():
                    if u.strip():
                        st.markdown(f"- {u.strip()}")

    st.divider()
    fname = (row.get("donor_short") or row.get("donor") or "donor").strip().replace(" ", "_")
    lines = _summary_lines(row)
    st.caption("Download the full profile as a print-ready A4 portrait PDF.")
    try:
        st.download_button("⬇ Download / print (PDF)", _donor_pdf(lines),
                           file_name=f"{fname}.pdf", mime="application/pdf",
                           key=f"view_dl_{row['canonical_key']}")
    except Exception:
        st.download_button("⬇ Download (.md)", "\n".join(lines),
                           file_name=f"{fname}.md", mime="text/markdown",
                           key=f"view_dlmd_{row['canonical_key']}")


# ---------------------------------------------------------------------------
# Dashboard cards + category chart
# ---------------------------------------------------------------------------
k1, k2, k3, k4 = st.columns(4)
k1.metric("Donors mapped", len(df))
if "donor_lmic_africa_focus" in df:
    k2.metric("LMIC / Africa focus", int((df["donor_lmic_africa_focus"] == "yes").sum()))
if "donor_global_multi_country_scope" in df:
    k3.metric("Global / multi-country", int((df["donor_global_multi_country_scope"] == "yes").sum()))
if "donor_verification_level" in df:
    k4.metric("High-confidence", int((df["donor_verification_level"] == "high").sum()))

if "category_clean" in df:
    cat = (df["category_clean"].fillna("(uncategorised)")
           .value_counts().reset_index())
    cat.columns = ["category", "count"]
    chart = (
        alt.Chart(cat).mark_bar(color="#00703C").encode(
            x=alt.X("category:N", sort="-y",
                    axis=alt.Axis(labelAngle=-40, labelFontSize=13,
                                  labelLimit=260, title=None)),
            y=alt.Y("count:Q", axis=alt.Axis(title="Donors")),
            tooltip=["category", "count"],
        ).properties(height=340)
    )
    with st.expander("Donors by category", expanded=False):
        st.altair_chart(chart, width='stretch')


# ---------------------------------------------------------------------------
# (Quick-lookup dropdown removed — full intelligence now opens as a pop-up via
# the "👁 View" action on a selected table row below.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# All donors — searchable table. The built-in toolbar gives CSV download,
# search and full-screen (so no separate Export button). Tick row(s) to act;
# full detail opens in a pop-up. Website is a clickable external link (greyed
# when none, until added via Edit).
# ---------------------------------------------------------------------------
st.divider()
_hdr, _addcol = st.columns([4, 1.2])
_hdr.subheader("All donors")
if _addcol.button("➕ Add donor", width="stretch", disabled=not can_edit,
                  key="donor_add_btn",
                  help=None if can_edit else "Admins only."):
    for _k in ("add_donor_name", "add_donor_short", "add_donor_web"):
        st.session_state.pop(_k, None)   # fresh form each open
    _add_donor_dialog()
with st.expander("🔎 Filter & search", expanded=False):
    fc1, fc2 = st.columns([3, 2])
    q = fc1.text_input("Search name / acronym / alias", key="donor_q")
    cats = fc2.multiselect(
        "Category", sorted(df["category_clean"].dropna().unique().tolist()),
        key="donor_cat")
    fc3, fc4 = st.columns(2)
    vers = fc3.multiselect(
        "Verification",
        sorted(df["donor_verification_level"].dropna().unique().tolist()) if "donor_verification_level" in df else [],
        key="donor_ver")
    areas_f = fc4.multiselect(
        "Strategic priority area", _pa.CATEGORIES, key="donor_pa",
        help="Donors whose priority program areas fall in this taxonomy category.")
    fc5, fc6 = st.columns(2)
    appl = fc5.multiselect(
        "Eligible applicant type",
        ["NGO", "For-profit / private", "Sub-recipient / partner"],
        key="donor_appl",
        help="Donors whose eligibility allows this applicant type.")
    funds_in = fc6.multiselect(
        "Funds in (region / country)", _geo.GEO_OPTIONS, key="donor_funds_in",
        help="UN region/tier or country; matches donors that fund there "
             "(region ↔ country expansion).")

fdf = df.copy()
if q:
    ql = q.lower()
    fdf = fdf[fdf.apply(lambda r: ql in f"{r.get('donor','')} {r.get('donor_short','')} {r.get('donor_aliases','')}".lower(), axis=1)]
if cats:
    fdf = fdf[fdf["category_clean"].isin(cats)]
if vers:
    fdf = fdf[fdf["donor_verification_level"].isin(vers)]
# Strategic-priority-area filter — match donor priority_program_areas (expanded
# to taxonomy child keys) against the selected categories.
if areas_f and "donor_priority_areas" in fdf.columns:
    _want_pa = _pa.expand(areas_f)
    fdf = fdf[fdf["donor_priority_areas"].apply(
        lambda v: bool(_pa.expand(_to_list(v)) & _want_pa))]
# Applicant-type filter (OR across selected types) using existing eligibility flags.
_APPL_FLAG = {"NGO": "donor_ngo_eligible", "For-profit / private": "donor_for_profit_eligible",
              "Sub-recipient / partner": "donor_subrecipient_partner_possible"}
_appl_flags = [_APPL_FLAG[a] for a in appl if _APPL_FLAG.get(a) in fdf.columns]
if _appl_flags:
    fdf = fdf[fdf[_appl_flags].apply(
        lambda r: any(str(r[c]).strip().lower() == "yes" for c in _appl_flags), axis=1)]
# Funds-in filter — match donor donor_geographic_scope, expanding region <-> country.
if funds_in and "donor_geographic_scope" in fdf.columns:
    _want_geo = _geo.expand(funds_in)
    fdf = fdf[fdf["donor_geographic_scope"].apply(
        lambda v: bool(_geo.expand(_to_list(v)) & _want_geo))]
fdf = fdf.reset_index(drop=True)

st.caption(f"**{len(fdf)}** of {len(df)} donors · tick row(s) to View / Edit / "
           "Delete / Share · use the table's ⬇ / 🔍 / ⛶ to export, search, expand.")


def _weburl(v):
    v = _disp(v)
    if not v:
        return None
    return v if v.startswith(("http://", "https://")) else f"https://{v}"


@st.dialog("Delete donors")
def _delete_many_dialog(keys: list) -> None:
    st.warning(f"Delete **{len(keys)}** selected donor(s)? This can't be undone.")
    if st.button("🗑 Yes, delete all", type="primary"):
        for _k in keys:
            sb.table("donor_intel").delete().eq("canonical_key", _k).execute()
        st.cache_data.clear()
        st.session_state["_donor_flash"] = f"Deleted {len(keys)} donor(s)."
        st.rerun()


@st.dialog("Share donors", width="large")
def _share_many_dialog(keys: list) -> None:
    st.caption(f"Combined summary of {len(keys)} donors — copy or download.")
    blocks = []
    for _k in keys:
        _m = df[df["canonical_key"] == _k]
        if _m.empty:
            continue
        r = _m.iloc[0].to_dict()
        blocks.append("\n".join(_summary_lines(r)))
    md = "\n\n---\n\n".join(blocks)
    st.code(md, language="markdown")
    st.download_button("⬇ Download (.md)", md, file_name="donors_selected.md",
                       mime="text/markdown", type="primary")


# ── Select-all-across-pages (the per-page st.dataframe selection only sees the
# 10 visible rows, so Share / Delete need a dataset-wide path). The matching
# full-table CSV export sits BELOW the table.
_select_all = st.checkbox(
    f"Select all {len(fdf)} across every page",
    key="donor_select_all", value=False,
    help="When ticked, Share / Delete apply to the ENTIRE filtered table, not "
         "just the rows visible on this page.")

# ── Top control row: page counter (left) + row actions (right) ──────────────
# 10 per page. We RESERVE the actions cell and fill it AFTER the table renders,
# using the dataframe's RETURN value for the selection. (Reading the selection
# from session_state *before* the widget rendered was desyncing it and making
# the table vanish on the next rerun — the return value is the supported path.)
_PER_PAGE = 10
pages = max(1, math.ceil(len(fdf) / _PER_PAGE))
if int(st.session_state.get("donor_page", 1)) > pages:
    st.session_state["donor_page"] = pages

_tc1, _tc2, _tc3 = st.columns([1.2, 2.2, 4.6])
pg = min(int(_tc1.number_input(f"Page (of {pages})", min_value=1, max_value=pages,
                               step=1, key="donor_page")), pages)
page_df = fdf.iloc[(pg - 1) * _PER_PAGE: pg * _PER_PAGE].reset_index(drop=True)
_tc2.caption(f"Showing {len(page_df)} of {len(fdf)} ({_PER_PAGE}/page) · "
             + (f"**all {len(fdf)} selected**" if _select_all
                else "selection applies to this page"))
_actions_slot = _tc3.empty()   # filled after the table renders (stays top-right)

# Clean display grid + clickable Website. LinkColumn renders blank (greyed /
# inactive) when there's no website. STABLE per-page key keeps the selection.
_grid = pd.DataFrame({
    "Donor": page_df.get("donor"),
    "Short": page_df.get("donor_short"),
    "Category": page_df.get("category_clean"),
    "Verification": page_df.get("donor_verification_level"),
    "Website": (page_df["donor_website"].map(_weburl) if "donor_website" in page_df.columns else None),
})
_event = st.dataframe(
    _grid, hide_index=True, width='stretch',
    on_select="rerun", selection_mode="multi-row",
    key=f"donor_table_p{pg}",
    column_config={
        "Donor": st.column_config.TextColumn("Donor", width="large"),
        "Website": st.column_config.LinkColumn("Website", display_text="Open ↗"),
    },
)
if _select_all:
    # Whole filtered table, regardless of which page is showing.
    _sel_keys = fdf["canonical_key"].tolist()
else:
    try:
        _sel_rows = list(_event.selection.rows)
    except Exception:
        _sel_rows = []
    _sel_keys = [page_df.iloc[i]["canonical_key"] for i in _sel_rows if i < len(page_df)]

# Fill the reserved top-right cell with the row actions.
with _actions_slot.container():
    if not _sel_keys:
        st.caption("⬇ Tick row(s) below to View / Edit / Delete / Share")
    elif len(_sel_keys) == 1:
        _row = page_df[page_df["canonical_key"] == _sel_keys[0]].iloc[0].to_dict()
        _ab = st.columns(4)
        if _ab[0].button("👁 View", width='stretch', key="act_view"):
            _view_dialog(_row)
        if _ab[1].button("✏️ Edit", width='stretch', disabled=not can_edit, key="act_edit"):
            _edit_dialog(_row)
        if _ab[2].button("🔗 Share", width='stretch', key="act_share"):
            _share_dialog(_row)
        if _ab[3].button("🗑 Delete", width='stretch', disabled=not can_edit, key="act_del"):
            _delete_dialog(_row)
    else:
        _ab = st.columns([1, 1, 1.4])
        if _ab[0].button(f"🔗 Share {len(_sel_keys)}", width='stretch', key="act_share_many"):
            _share_many_dialog(_sel_keys)
        if _ab[1].button(f"🗑 Delete {len(_sel_keys)}", width='stretch',
                         disabled=not can_edit, key="act_del_many"):
            _delete_many_dialog(_sel_keys)

# ── Full-table CSV export (below the table) — entire filtered set, all columns,
# every page. Neutral (secondary) styling so it doesn't compete visually.
st.divider()
_export_df = fdf.drop(columns=[c for c in ("category_clean",) if c in fdf.columns],
                      errors="ignore")
st.download_button(
    f"⬇ Download all {len(fdf)} donors (CSV)",
    _export_df.to_csv(index=False).encode("utf-8"),
    file_name="donor_intelligence_mapping.csv", mime="text/csv",
    help="Exports every row in the current (filtered) table — all pages, all "
         "columns. Clear filters first to export the whole dataset.")
