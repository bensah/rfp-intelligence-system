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

import altair as alt
import pandas as pd
import streamlit as st

from core import geographies as _geo
from core import permissions, settings
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
    return pd.DataFrame(res.data or [])


@st.cache_data(ttl=60)
def _load_contacts(canonical_key: str) -> pd.DataFrame:
    """Focal-person / additional contacts for one donor (official channels
    first, then by name)."""
    res = (get_client().table("donor_contacts").select("*")
           .eq("canonical_key", canonical_key)
           .order("is_official", desc=True).order("contact_name").execute())
    return pd.DataFrame(res.data or [])


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
    "local_board_required": "Local board members required",
    "summary_description": "Summary",
    "donor_values": "Values",
    "strategy_url": "Strategy (URL)",
    "projected_budget_period": "Budget period",
}


def _pretty_choice(v: str) -> str:
    """Display a stored enum value nicely without changing what's stored:
    'reimbursement_only' → 'Reimbursement Only', 'high' → 'High', '' → '—'."""
    s = str(v or "").strip()
    return s.replace("_", " ").title() if s else "—"


def _label(col: str) -> str:
    if col in _LABEL_OVERRIDES:
        return _LABEL_OVERRIDES[col]
    words = col.replace("_", " ").split()
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
_SHORT_TEXT = ["donor", "donor_short", "donor_category", "website",
               "award_low_usd", "award_high_usd",
               "total_annual_funding_global", "funding_mechanism"]
_LONG_TEXT = ["aliases", "funding_scope_geographic", "active_route_status",
              "direct_local_org_eligible", "priority_program_areas",
              "verification_caveats", "evidence_summary", "notes",
              "source_urls"]
# Institutional / official donor contact (one set per donor — the donor_intel
# row). The many focal-person contacts live in the donor_contacts table.
_CONTACT = ["hq_address", "hq_country", "main_phone", "general_email",
            "donor_linkedin_url", "other_profile_urls",
            "contact_persons", "contact_emails", "contact_phones",
            "contact_linkedin_urls"]
_CHOICE = {
    "prefinance_required": ["", "none", "partial", "reimbursement_only"],
    "verification_level": ["", "high", "medium", "low"],
}
# Qualitative donor-intelligence profile (added to the edit form, migration 025).
# Listed here so these columns are treated as free text — NOT flags — once they
# exist in the table, and so the View / Share / PDF summaries include them.
_PROFILE = ["summary_description", "mission", "vision", "donor_values",
            "strategy_url", "total_awards", "total_funding_to_date",
            "current_awards", "past_awards", "projected_budget",
            "projected_budget_period", "funding_cycle", "recent_activity",
            "application_process", "reporting_requirements",
            "past_projects_json"]
# Columns kept for backward-compat but no longer surfaced anywhere (not edited,
# not shown in View, not in share/PDF). verification_level already captures data
# confidence, so the free-text "verification caveats" was redundant + confusing.
# funded_geographies is an unused column (the geo field is funding_scope_geographic).
_HIDDEN_FIELDS = {"verification_caveats", "funded_geographies"}

_NON_FLAG = (set(_META) | set(_SHORT_TEXT) | set(_LONG_TEXT)
             | set(_CHOICE) | set(_CONTACT) | set(_PROFILE) | _HIDDEN_FIELDS)

_FLAGS = [c for c in df.columns if c not in _NON_FLAG]
_ELIG = [
    "ngo_eligible", "for_profit_eligible", "govt_or_ccm_route_required",
    "grant_route", "procurement_tender_route", "loan_dev_finance_route",
    "subrecipient_partner_possible", "open_call_unsolicited",
    "invitation_solicited", "two_stage_application", "online_portal_submission",
    "lmic_africa_focus", "global_multi_country_scope",
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
# PROGRAM_AREAS reuses the program-area-fit flags so it matches Scan Preferences.
PROGRAM_AREAS = [re.sub(r"\s*fit$", "", _label(c), flags=re.I) for c in _FIT]
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


# Fields stored as JSON lists — rendered as comma lists in share/export.
_LIST_FIELDS = {"funding_scope_geographic", "priority_program_areas",
                "funding_mechanism"}


def _disp_field(row: dict, col: str):
    """Display string for share/export: list-fields joined with commas, else _disp."""
    if col in _LIST_FIELDS:
        vals = _to_list(row.get(col))
        return ", ".join(vals) if vals else None
    return _disp(row.get(col))


def _past_projects(row: dict) -> list[dict]:
    """Parsed past-projects list (only entries with a title), or []."""
    try:
        pp = json.loads(row.get("past_projects_json") or "[]")
    except (ValueError, TypeError):
        return []
    return [p for p in pp if isinstance(p, dict) and p.get("title")] if isinstance(pp, list) else []


def _summary_lines(row: dict) -> list[str]:
    """Markdown summary for share / PDF / download — the core matrix fields PLUS
    the full donor-intelligence profile (About, footprint, intelligence, past
    projects). Blank fields are skipped, so the summary only shows what's known."""
    lines = [f"# {_title_name(row)}", ""]
    # Core matrix fields (identity, awards, scope, routes, institutional contact).
    for col in _SHORT_TEXT + list(_CHOICE) + _LONG_TEXT + _CONTACT:
        v = _disp_field(row, col)
        if col in ("donor", "donor_short") or col in _HIDDEN_FIELDS or not v:
            continue
        lines.append(f"- **{_label(col)}:** {v}")
    # Profile fields (handled below: projected_budget pairs with its period,
    # past_projects_json renders as its own block).
    for col in _PROFILE:
        if col in ("projected_budget", "projected_budget_period", "past_projects_json"):
            continue
        v = _disp(row.get(col))
        if v:
            lines.append(f"- **{_label(col)}:** {v}")
    _pb = _disp(row.get("projected_budget"))
    if _pb:
        _per = _disp(row.get("projected_budget_period"))
        lines.append(f"- **{_label('projected_budget')}:** {_pb}"
                     + (f" ({_per})" if _per else ""))
    projects = _past_projects(row)
    if projects:
        lines.append("")
        lines.append(f"**Past projects ({len(projects)}):**")
        for p in projects:
            bits = []
            amt = p.get("amount")
            if amt not in (None, ""):
                cur = (p.get("currency") or "").strip()
                try:
                    bits.append((f"{cur} " if cur else "") + f"{float(amt):,.0f}")
                except (TypeError, ValueError):
                    bits.append(((f"{cur} " if cur else "") + str(amt)).strip())
            if p.get("year"):
                bits.append(str(p["year"]).strip())
            if p.get("country"):
                bits.append(str(p["country"]).strip())
            tail = f" — {', '.join(bits)}" if bits else ""
            lines.append(f"- {str(p['title']).strip()}{tail}")
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


def _checkbox_matrix(row: dict, *, editable: bool, key_prefix: str) -> dict:
    """Render the flag groups as a multi-column checkbox grid. Returns the
    edited {col: 'yes'|'no'|<original-if-untouched-blank>} when editable."""
    edited: dict = {}
    for group, cols in _FLAG_GROUPS.items():
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
    st.markdown(f"### {row.get('donor')}")
    edited: dict = {}
    with st.container():
        c1, c2 = st.columns(2)
        edited["donor"] = c1.text_input("Donor", row.get("donor") or "",
                                        key=f"donor_{row['canonical_key']}")
        edited["donor_short"] = c2.text_input("Donor short", row.get("donor_short") or "",
                                              key=f"donor_short_{row['canonical_key']}")
        c3, c4 = st.columns(2)
        _cur_cat = _normalize_category(row.get("donor_category"))
        _cat_opts = list(_CATEGORIES)
        if _cur_cat not in _cat_opts and _cur_cat != "(uncategorised)":
            _cat_opts.append(_cur_cat)
        edited["donor_category"] = c3.selectbox(
            "Donor category", _cat_opts,
            index=(_cat_opts.index(_cur_cat) if _cur_cat in _cat_opts else 0),
            key=f"cat_{row['canonical_key']}")
        edited["website"] = c4.text_input("Website", row.get("website") or "",
                                          key=f"website_{row['canonical_key']}")

        # About this donor — summary / mission / vision / values / strategy.
        # Placeholders to fill in during donor research; persisted once the
        # matching columns exist (see the migration note in the page summary).
        st.markdown("**About this donor**")
        edited["summary_description"] = st.text_area(
            "Summary / description", row.get("summary_description") or "", height=80,
            key=f"summary_description_{row['canonical_key']}")
        _mv1, _mv2 = st.columns(2)
        edited["mission"] = _mv1.text_area("Mission", row.get("mission") or "", height=70,
                                           key=f"mission_{row['canonical_key']}")
        edited["vision"] = _mv2.text_area("Vision", row.get("vision") or "", height=70,
                                          key=f"vision_{row['canonical_key']}")
        edited["donor_values"] = st.text_area(
            "Values", row.get("donor_values") or "", height=70,
            key=f"donor_values_{row['canonical_key']}")
        edited["strategy_url"] = st.text_input(
            "Donor strategy (URL)", row.get("strategy_url") or "",
            key=f"strategy_url_{row['canonical_key']}",
            help="Link to the donor's published strategy document.")

        # Funding footprint — counts + money (free text so "~120/year" is fine).
        st.markdown("**Funding footprint**")
        _ff1, _ff2 = st.columns(2)
        edited["total_awards"] = _ff1.text_input(
            "Total awards", row.get("total_awards") or "",
            key=f"total_awards_{row['canonical_key']}")
        edited["total_funding_to_date"] = _ff2.text_input(
            "Total funding to date (amount)", row.get("total_funding_to_date") or "",
            key=f"total_funding_to_date_{row['canonical_key']}")
        _ff3, _ff4 = st.columns(2)
        edited["current_awards"] = _ff3.text_input(
            "Current / active awards", row.get("current_awards") or "",
            key=f"current_awards_{row['canonical_key']}")
        edited["past_awards"] = _ff4.text_input(
            "Past awards", row.get("past_awards") or "",
            key=f"past_awards_{row['canonical_key']}")
        _pb1, _pb2 = st.columns([3, 2])
        edited["projected_budget"] = _pb1.text_input(
            "Projected budget / published allocations",
            row.get("projected_budget") or "",
            key=f"projbud_{row['canonical_key']}",
            help="The amount, e.g. '200 billion XAF' or '$5M'.")
        edited["projected_budget_period"] = _pb2.text_input(
            "Period / end year",
            row.get("projected_budget_period") or "",
            key=f"projbudper_{row['canonical_key']}",
            help="When it applies, e.g. '2024–2030' or 'by 2046'.")

        # Intelligence — structured where it helps consistency across donors;
        # each dropdown has an "Other (specify)" escape hatch for edge cases.
        st.markdown("**Intelligence**")
        _in1, _in2 = st.columns(2)
        with _in1:
            edited["funding_cycle"] = _single_with_other(
                "Funding cycle / timing", FUNDING_CYCLES,
                row.get("funding_cycle"), key=f"fcyc_{row['canonical_key']}")
        edited["recent_activity"] = _in2.text_input(
            "Recent activity / last funded", row.get("recent_activity") or "",
            key=f"recent_activity_{row['canonical_key']}",
            help="Free text — e.g. a year or last-funded note.")
        _ip1, _ip2 = st.columns(2)
        with _ip1:
            edited["application_process"] = _single_with_other(
                "Application process", APPLICATION_PROCESSES,
                row.get("application_process"), key=f"appproc_{row['canonical_key']}")
        with _ip2:
            edited["reporting_requirements"] = _single_with_other(
                "Reporting requirements", REPORTING_REQUIREMENTS,
                row.get("reporting_requirements"), key=f"reprq_{row['canonical_key']}")

        # Past projects — title + award amount (+ year/country). Stored as JSON
        # in past_projects_json; add rows freely.
        st.markdown("**Past projects**")
        try:
            _proj = json.loads(row.get("past_projects_json") or "[]")
            _proj = _proj if isinstance(_proj, list) else []
        except (ValueError, TypeError):
            _proj = []
        _proj_base = (pd.DataFrame(_proj) if _proj else pd.DataFrame()).reindex(
            columns=["title", "amount", "currency", "year", "country"])
        # Enforce dtypes so they match the column_config (a NumberColumn over an
        # object/empty column raises StreamlitAPIException at render time).
        _proj_base["amount"] = pd.to_numeric(_proj_base["amount"], errors="coerce")
        for _tc in ("title", "currency", "year", "country"):
            _proj_base[_tc] = _proj_base[_tc].astype("string")
        _proj_edited = st.data_editor(
            _proj_base, num_rows="dynamic", hide_index=True, use_container_width=True,
            key=f"proj_ed_{row['canonical_key']}",
            column_config={
                "title": st.column_config.TextColumn("Project title", width="large"),
                "amount": st.column_config.NumberColumn("Award amount", format="%.0f"),
                "currency": st.column_config.TextColumn("Currency", default="USD"),
                "year": st.column_config.TextColumn("Year"),
                "country": st.column_config.TextColumn("Country"),
            })
        _proj_recs = []
        for _pr in _proj_edited.to_dict("records"):
            if not str(_pr.get("title") or "").strip():
                continue
            _amt = _pr.get("amount")
            try:
                _amt = None if (_amt is None or pd.isna(_amt)) else float(_amt)
            except (TypeError, ValueError):
                _amt = None
            _proj_recs.append({
                "title": str(_pr.get("title")).strip(),
                "amount": _amt,
                "currency": (str(_pr.get("currency")).strip() or None) if _pr.get("currency") else None,
                "year": (str(_pr.get("year")).strip() or None) if _pr.get("year") else None,
                "country": (str(_pr.get("country")).strip() or None) if _pr.get("country") else None,
            })
        edited["past_projects_json"] = json.dumps(_proj_recs)

        # Choice dropdowns. Values stay lowercase (the scorer keys off them, e.g.
        # prefinance_required == "reimbursement_only"); only the DISPLAY is
        # prettified → "None", "Partial", "Reimbursement Only", "High", …
        for col, opts in _CHOICE.items():
            cur = row.get(col) or ""
            edited[col] = st.selectbox(
                _label(col), opts, index=opts.index(cur) if cur in opts else 0,
                key=f"{col}_{row['canonical_key']}", format_func=_pretty_choice,
            )
        # Funding short text
        fcols = st.columns(2)
        for j, col in enumerate(["award_low_usd", "award_high_usd",
                                 "total_annual_funding_global"]):
            edited[col] = fcols[j % 2].text_input(
                _label(col), row.get(col) or "", key=f"{col}_{row['canonical_key']}")
        edited["funding_mechanism"] = json.dumps(_multi_with_options(
            "Funding mechanism", FUNDING_MECHANISMS, row.get("funding_mechanism"),
            key=f"fm_{row['canonical_key']}"))

    st.divider()
    edited.update(_checkbox_matrix(row, editable=True, key_prefix=f"ed_{row['canonical_key']}"))

    st.divider()
    st.markdown("**Eligibility, scope & routes**")
    edited["funding_scope_geographic"] = json.dumps(_multi_with_options(
        "Funding scope — geographies (UN regions / tiers / countries)",
        _geo.GEO_OPTIONS, row.get("funding_scope_geographic"),
        key=f"fsg_{row['canonical_key']}",
        help="Where the donor funds. Drives the 'Funds in' filter + coverage view."))
    edited["priority_program_areas"] = json.dumps(_multi_with_options(
        "Priority program areas", PROGRAM_AREAS, row.get("priority_program_areas"),
        key=f"ppa_{row['canonical_key']}",
        help="Reuses the program-area vocabulary from Scan Preferences."))
    _r1, _r2 = st.columns(2)
    with _r1:
        edited["direct_local_org_eligible"] = _single_with_other(
            "Direct local org eligible", LOCAL_ORG_ELIGIBLE,
            row.get("direct_local_org_eligible"), key=f"dle_{row['canonical_key']}")
    with _r2:
        edited["active_route_status"] = _single_with_other(
            "Route status", ROUTE_STATUSES, row.get("active_route_status"),
            key=f"rstat_{row['canonical_key']}")

    st.markdown("**Other details**")
    for col in ["evidence_summary", "notes", "aliases", "source_urls"]:
        edited[col] = st.text_area(_label(col), row.get(col) or "", height=70,
                                   key=f"{col}_{row['canonical_key']}")

    st.divider()
    st.markdown("**Official / institutional contact**")
    ic1, ic2 = st.columns(2)
    edited["general_email"] = ic1.text_input("General email", row.get("general_email") or "",
                                             key=f"general_email_{row['canonical_key']}")
    edited["main_phone"] = ic2.text_input("Main phone", row.get("main_phone") or "",
                                          key=f"main_phone_{row['canonical_key']}")
    ic3, ic4 = st.columns(2)
    edited["hq_country"] = ic3.text_input("HQ country", row.get("hq_country") or "",
                                          key=f"hq_country_{row['canonical_key']}")
    edited["donor_linkedin_url"] = ic4.text_input("Donor LinkedIn", row.get("donor_linkedin_url") or "",
                                                  key=f"donor_linkedin_url_{row['canonical_key']}")
    edited["hq_address"] = st.text_area("HQ address", row.get("hq_address") or "", height=60,
                                        key=f"hq_address_{row['canonical_key']}")
    edited["other_profile_urls"] = st.text_area("Other profile URLs", row.get("other_profile_urls") or "", height=60,
                                                key=f"other_profile_urls_{row['canonical_key']}")

    st.markdown("**Contacts — focal persons & additional (private)**")
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
        _base, num_rows="dynamic", use_container_width=True, hide_index=True,
        key=f"contacts_ed_{row['canonical_key']}",
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

    if st.button("💾 Save changes", type="primary", use_container_width=True):
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
                "their columns. Apply **migration 025** in Supabase, then re-save: "
                + ", ".join(_dropped))
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
        lines.append("")
        lines.append("**Contacts:**")
        for _, cr in _contacts.iterrows():
            lines.append("- " + _contact_line(cr.to_dict()).replace("  \n", " — ").replace(r"\$", "$"))
    yes_flags = [_label(c) for c in _FLAGS if _yes(row.get(c))]
    if yes_flags:
        lines.append("")
        lines.append("**Flags = yes:** " + ", ".join(yes_flags))
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

    if row.get("verification_level") == "low":
        st.warning("⚠ Low verification — confirm against the live call package.")
    st.subheader(_disp(row.get("donor")) or "—")
    sub = []
    if _disp(row.get("donor_category")):
        sub.append(_normalize_category(row.get("donor_category")))
    if _disp(row.get("website")):
        w = _disp(row.get("website"))
        sub.append(f"[{w}]({w})")
    if sub:
        st.caption(" · ".join(sub))

    # ── Key facts — only the populated ones (no "— — —" filler) ──────────────
    facts: list[tuple[str, str]] = []
    lo, hi = _md(row.get("award_low_usd")), _md(row.get("award_high_usd"))
    if lo or hi:
        facts.append(("Award range", f"{lo or '—'} – {hi or '—'}"))
    for col, lbl in (("total_annual_funding_global", "Annual funding"),
                     ("total_funding_to_date", "Total funding"),
                     ("total_awards", "Total awards"),
                     ("current_awards", "Current awards"),
                     ("past_awards", "Past awards"),
                     ("projected_budget", "Projected budget"),
                     ("verification_level", "Verification"),
                     ("prefinance_required", "Prefinance")):
        v = _md(row.get(col)) or _disp(row.get(col))
        if col == "projected_budget" and v:
            _per = _disp(row.get("projected_budget_period"))
            if _per:
                v = f"{v} ({_per})"
        elif col in ("verification_level", "prefinance_required") and v:
            v = _pretty_choice(v)
        if v:
            facts.append((lbl, v))
    if facts:
        _fcols = st.columns(min(len(facts), 4))
        for _i, (_lbl, _v) in enumerate(facts):
            _fcols[_i % len(_fcols)].markdown(f"**{_lbl}**  \n{_v}")

    # ── About — only populated ───────────────────────────────────────────────
    _about = [(lbl, _disp(row.get(col))) for col, lbl in (
        ("summary_description", "Summary"), ("mission", "Mission"),
        ("vision", "Vision"), ("donor_values", "Values")) if _disp(row.get(col))]
    _su = _disp(row.get("strategy_url"))
    if _about or _su:
        st.markdown("**About**")
        for _lbl, _v in _about:
            st.markdown(f"- **{_lbl}:** {_v}")
        if _su:
            st.markdown(f"- **Strategy:** [{_su}]({_su})")

    # ── Funding scope / program areas / mechanism (chips) + routes ───────────
    _scope = _to_list(row.get("funding_scope_geographic"))
    if _scope:
        st.markdown("**Funding scope — geographies**")
        st.markdown(_chips(_scope), unsafe_allow_html=True)
    _areas = _to_list(row.get("priority_program_areas"))
    if _areas:
        st.markdown("**Priority program areas**")
        st.markdown(_chips(_areas), unsafe_allow_html=True)
    _mech = _to_list(row.get("funding_mechanism"))
    if _mech:
        st.markdown("**Funding mechanism**")
        st.markdown(_chips(_mech), unsafe_allow_html=True)
    for _lbl, _col in (("Route status", "active_route_status"),
                       ("Direct local org eligible", "direct_local_org_eligible")):
        if _disp(row.get(_col)):
            st.markdown(f"- **{_lbl}:** {_disp(row.get(_col))}")

    # ── Intelligence — qualitative profile (populated only) ──────────────────
    _intel = [(lbl, _disp(row.get(col))) for col, lbl in (
        ("funding_cycle", "Funding cycle"),
        ("recent_activity", "Recent activity"),
        ("application_process", "Application process"),
        ("reporting_requirements", "Reporting requirements")) if _disp(row.get(col))]
    if _intel:
        st.markdown("**Intelligence**")
        for _lbl, _v in _intel:
            st.markdown(f"- **{_lbl}:** {_v}")

    # ── Past projects — title + award amount (populated only) ────────────────
    try:
        _projects = json.loads(row.get("past_projects_json") or "[]")
        _projects = _projects if isinstance(_projects, list) else []
    except (ValueError, TypeError):
        _projects = []
    _projects = [p for p in _projects if isinstance(p, dict) and p.get("title")]
    if _projects:
        st.markdown(f"**Past projects** ({len(_projects)})")
        _pdf = pd.DataFrame(_projects)
        _show = [c for c in ["title", "amount", "currency", "year", "country"]
                 if c in _pdf.columns and _pdf[c].notna().any()]
        st.dataframe(
            _pdf[_show].rename(columns={
                "title": "Project", "amount": "Award", "currency": "Cur.",
                "year": "Year", "country": "Country"}),
            hide_index=True, use_container_width=True)

    # ── Contacts — only when populated ───────────────────────────────────────
    _render_contacts(row)

    # ── Flags as clean chips (only "yes"); Program-area drops the "…fit" tail ─

    for _group, _gcols in _FLAG_GROUPS.items():
        _yeses = [c for c in _gcols if _yes(row.get(c))]
        if not _yeses:
            continue
        if _group == "Program-area fit":
            _labels = [re.sub(r"\s*fit$", "", _label(c), flags=re.I) for c in _yeses]
        else:
            _labels = [_label(c) for c in _yeses]
        st.markdown(f"**{_group}**")
        st.markdown(_chips(_labels), unsafe_allow_html=True)

    # ── Other details — only non-empty (structured fields shown above) ───────
    _structured = {"funding_scope_geographic", "priority_program_areas",
                   "active_route_status", "direct_local_org_eligible"}
    _other = [(c, _disp(row.get(c))) for c in _LONG_TEXT
              if c not in _structured and c not in _HIDDEN_FIELDS
              and _disp(row.get(c))]
    if _other:
        st.markdown("**Other details**")
        for col, val in _other:
            if col == "source_urls":
                st.markdown(f"**{_label(col)}:**")
                for u in val.replace("\\n", "\n").splitlines():
                    if u.strip():
                        st.markdown(f"- {u.strip()}")
            else:
                st.markdown(f"**{_label(col)}:** {_md(row.get(col)) or val}")

    st.divider()
    fname = (row.get("donor_short") or row.get("donor") or "donor").strip().replace(" ", "_")
    lines = _summary_lines(row)
    try:
        st.download_button("⬇ Download (share)", _donor_pdf(lines),
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
if "lmic_africa_focus" in df:
    k2.metric("LMIC / Africa focus", int((df["lmic_africa_focus"] == "yes").sum()))
if "global_multi_country_scope" in df:
    k3.metric("Global / multi-country", int((df["global_multi_country_scope"] == "yes").sum()))
if "verification_level" in df:
    k4.metric("High-confidence", int((df["verification_level"] == "high").sum()))

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
        st.altair_chart(chart, use_container_width=True)


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
st.subheader("All donors")
with st.expander("🔎 Filter & search", expanded=False):
    fc1, fc2 = st.columns([3, 2])
    q = fc1.text_input("Search name / acronym / alias", key="donor_q")
    cats = fc2.multiselect(
        "Category", sorted(df["category_clean"].dropna().unique().tolist()),
        key="donor_cat")
    fc3, fc4 = st.columns(2)
    vers = fc3.multiselect(
        "Verification",
        sorted(df["verification_level"].dropna().unique().tolist()) if "verification_level" in df else [],
        key="donor_ver")
    fit = fc4.multiselect("Program-area fit = yes", _FIT, format_func=_label, key="donor_fit")
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
    fdf = fdf[fdf.apply(lambda r: ql in f"{r.get('donor','')} {r.get('donor_short','')} {r.get('aliases','')}".lower(), axis=1)]
if cats:
    fdf = fdf[fdf["category_clean"].isin(cats)]
if vers:
    fdf = fdf[fdf["verification_level"].isin(vers)]
for fc in fit:
    fdf = fdf[fdf[fc] == "yes"]
# Applicant-type filter (OR across selected types) using existing eligibility flags.
_APPL_FLAG = {"NGO": "ngo_eligible", "For-profit / private": "for_profit_eligible",
              "Sub-recipient / partner": "subrecipient_partner_possible"}
_appl_flags = [_APPL_FLAG[a] for a in appl if _APPL_FLAG.get(a) in fdf.columns]
if _appl_flags:
    fdf = fdf[fdf[_appl_flags].apply(
        lambda r: any(str(r[c]).strip().lower() == "yes" for c in _appl_flags), axis=1)]
# Funds-in filter — match donor funding_scope_geographic, expanding region <-> country.
if funds_in and "funding_scope_geographic" in fdf.columns:
    _want_geo = _geo.expand(funds_in)
    fdf = fdf[fdf["funding_scope_geographic"].apply(
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
             "selection applies to this page")
_actions_slot = _tc3.empty()   # filled after the table renders (stays top-right)

# Clean display grid + clickable Website. LinkColumn renders blank (greyed /
# inactive) when there's no website. STABLE per-page key keeps the selection.
_grid = pd.DataFrame({
    "Donor": page_df.get("donor"),
    "Short": page_df.get("donor_short"),
    "Category": page_df.get("category_clean"),
    "Verification": page_df.get("verification_level"),
    "Website": (page_df["website"].map(_weburl) if "website" in page_df.columns else None),
})
_event = st.dataframe(
    _grid, hide_index=True, use_container_width=True,
    on_select="rerun", selection_mode="multi-row",
    key=f"donor_table_p{pg}",
    column_config={
        "Donor": st.column_config.TextColumn("Donor", width="large"),
        "Website": st.column_config.LinkColumn("Website", display_text="Open ↗"),
    },
)
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
        if _ab[0].button("👁 View", use_container_width=True, key="act_view"):
            _view_dialog(_row)
        if _ab[1].button("✏️ Edit", use_container_width=True, disabled=not can_edit, key="act_edit"):
            _edit_dialog(_row)
        if _ab[2].button("🔗 Share", use_container_width=True, key="act_share"):
            _share_dialog(_row)
        if _ab[3].button("🗑 Delete", use_container_width=True, disabled=not can_edit, key="act_del"):
            _delete_dialog(_row)
    else:
        _ab = st.columns([1, 1, 1.4])
        if _ab[0].button(f"🔗 Share {len(_sel_keys)}", use_container_width=True, key="act_share_many"):
            _share_many_dialog(_sel_keys)
        if _ab[1].button(f"🗑 Delete {len(_sel_keys)}", use_container_width=True,
                         disabled=not can_edit, key="act_del_many"):
            _delete_many_dialog(_sel_keys)
