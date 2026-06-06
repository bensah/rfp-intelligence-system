"""Donor Intelligence Mapping — donor metadata dashboard + CRUD.

Reads the `donor_intel` table (seeded via scripts/import_donor_intel.py).
Layout: summary cards → category chart → quick donor lookup (dropdown +
full detail with a greyed checkbox matrix) → filter/search → paginated table
with per-row edit / delete / share + CSV export.

Edit/delete is admin + super_user only; everyone else views + exports.
Auth + global header already ran in App.py; this file is content-only.
"""
from __future__ import annotations

import math
import re

import altair as alt
import pandas as pd
import streamlit as st

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
}


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
    return _deshout(s.replace("{country}", _COUNTRY))


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
_META = {"id", "updated_at", "created_at", "canonical_key",
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
_NON_FLAG = (set(_META) | set(_SHORT_TEXT) | set(_LONG_TEXT)
             | set(_CHOICE) | set(_CONTACT))

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
        edited["donor"] = c1.text_input("Donor", row.get("donor") or "")
        edited["donor_short"] = c2.text_input("Donor short", row.get("donor_short") or "")
        c3, c4 = st.columns(2)
        edited["donor_category"] = c3.text_input("Donor category", row.get("donor_category") or "")
        edited["website"] = c4.text_input("Website", row.get("website") or "")
        # Choice dropdowns
        for col, opts in _CHOICE.items():
            cur = row.get(col) or ""
            edited[col] = st.selectbox(
                _label(col), opts, index=opts.index(cur) if cur in opts else 0,
            )
        # Funding short text
        fcols = st.columns(2)
        for j, col in enumerate(["award_low_usd", "award_high_usd",
                                 "total_annual_funding_global", "funding_mechanism"]):
            edited[col] = fcols[j % 2].text_input(_label(col), row.get(col) or "")

    st.divider()
    edited.update(_checkbox_matrix(row, editable=True, key_prefix=f"ed_{row['canonical_key']}"))

    st.divider()
    st.markdown("**Other details**")
    for col in ["direct_local_org_eligible", "funding_scope_geographic",
                "active_route_status", "priority_program_areas",
                "verification_caveats", "evidence_summary", "notes",
                "aliases", "source_urls"]:
        edited[col] = st.text_area(_label(col), row.get(col) or "", height=70)

    st.divider()
    st.markdown("**Official / institutional contact**")
    ic1, ic2 = st.columns(2)
    edited["general_email"] = ic1.text_input("General email", row.get("general_email") or "")
    edited["main_phone"] = ic2.text_input("Main phone", row.get("main_phone") or "")
    ic3, ic4 = st.columns(2)
    edited["hq_country"] = ic3.text_input("HQ country", row.get("hq_country") or "")
    edited["donor_linkedin_url"] = ic4.text_input("Donor LinkedIn", row.get("donor_linkedin_url") or "")
    edited["hq_address"] = st.text_area("HQ address", row.get("hq_address") or "", height=60)
    edited["other_profile_urls"] = st.text_area("Other profile URLs", row.get("other_profile_urls") or "", height=60)

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
        sb.table("donor_intel").upsert(payload, on_conflict="canonical_key").execute()

        # Reconcile contacts: replace this donor's set with the edited rows.
        sb.table("donor_contacts").delete().eq("canonical_key", key).execute()
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
        if recs:
            sb.table("donor_contacts").insert(recs).execute()

        st.cache_data.clear()
        st.session_state["_donor_flash"] = f"✓ Saved changes to {row.get('donor')}."
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
    lines = [f"# {_title_name(row)}", ""]
    for col in _SHORT_TEXT + list(_CHOICE) + _LONG_TEXT + _CONTACT:
        v = _disp(row.get(col))
        if col in ("donor", "donor_short") or not v:
            continue
        lines.append(f"- **{_label(col)}:** {v}")
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

if "donor_category" in df:
    cat = (df["donor_category"].fillna("(uncategorised)")
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
# Quick lookup — dropdown + full detail (moved up, right under the cards)
# ---------------------------------------------------------------------------
st.divider()
pick = st.selectbox(
    "Select a donor to view full intelligence",
    ["—"] + df["donor"].dropna().tolist(), key="donor_pick",
)
if pick != "—":
    row = df[df["donor"] == pick].iloc[0].to_dict()
    if row.get("verification_level") == "low":
        st.warning("⚠ Low verification — confirm against the live call package.")

    # Donor name heading, then category + website caption, then a tidy
    # funding grid ($ escaped, blank/nan skipped).
    st.subheader(_disp(row.get("donor")) or "—")
    sub = []
    if _disp(row.get("donor_category")):
        sub.append(_disp(row.get("donor_category")))
    if _disp(row.get("website")):
        w = _disp(row.get("website"))
        sub.append(f"[{w}]({w})")
    if sub:
        st.caption(" · ".join(sub))

    # Contacts (institutional + focal persons) — shown before the award grid.
    _render_contacts(row)

    g = st.columns(4)
    lo, hi = _md(row.get("award_low_usd")), _md(row.get("award_high_usd"))
    g[0].markdown(f"**Award range**  \n{lo or '—'} – {hi or '—'}")
    g[1].markdown(f"**Annual funding**  \n{_md(row.get('total_annual_funding_global')) or '—'}")
    g[2].markdown(f"**Mechanism**  \n{_disp(row.get('funding_mechanism')) or '—'}")
    g[3].markdown(f"**Verification**  \n{_disp(row.get('verification_level')) or '—'}")
    if _disp(row.get("prefinance_required")):
        st.caption(f"Prefinance: **{_disp(row.get('prefinance_required'))}**")

    _checkbox_matrix(row, editable=False, key_prefix=f"view_{row['canonical_key']}")

    st.markdown("**Other details**")
    for col in _LONG_TEXT:
        val = _disp(row.get(col))
        if not val:
            continue
        if col == "source_urls":
            st.markdown(f"**{_label(col)}:**")
            for u in val.replace("\\n", "\n").splitlines():
                if u.strip():
                    st.markdown(f"- {u.strip()}")
        else:
            st.markdown(f"**{_label(col)}:** {_md(row.get(col)) or val}")

    # Edit / Share actions — at the end of the donor detail.
    st.divider()
    a1, a2, _a3 = st.columns([1, 1, 6])
    if a1.button("✏️ Edit", key="detail_edit", disabled=not can_edit):
        _edit_dialog(row)
    if a2.button("🔗 Share", key="detail_share"):
        _share_dialog(row)


# ---------------------------------------------------------------------------
# Filter / search + paginated table with row actions
# ---------------------------------------------------------------------------
st.divider()
st.subheader("All donors")
with st.expander("🔎 Filter & search", expanded=False):
    fc1, fc2 = st.columns([3, 2])
    q = fc1.text_input("Search name / acronym / alias", key="donor_q")
    cats = fc2.multiselect(
        "Category",
        sorted(df["donor_category"].dropna().unique().tolist()) if "donor_category" in df else [],
        key="donor_cat",
    )
    fc3, fc4 = st.columns(2)
    vers = fc3.multiselect(
        "Verification",
        sorted(df["verification_level"].dropna().unique().tolist()) if "verification_level" in df else [],
        key="donor_ver",
    )
    fit = fc4.multiselect("Program-area fit = yes", _FIT, format_func=_label, key="donor_fit")

fdf = df.copy()
if q:
    ql = q.lower()
    fdf = fdf[fdf.apply(lambda r: ql in f"{r.get('donor','')} {r.get('donor_short','')} {r.get('aliases','')}".lower(), axis=1)]
if cats:
    fdf = fdf[fdf["donor_category"].isin(cats)]
if vers:
    fdf = fdf[fdf["verification_level"].isin(vers)]
for fc in fit:
    fdf = fdf[fdf[fc] == "yes"]

top = st.columns([3, 1.4, 1.4])
top[0].caption(f"**{len(fdf)}** of {len(df)} donors")
per_page = top[1].selectbox("Rows", [10, 25, 50, "All"], index=0, key="donor_pp")
_export = fdf.apply(
    lambda col: col.map(lambda x: x.replace("{country}", _COUNTRY) if isinstance(x, str) else x)
)
top[2].download_button(
    "⬇ Export CSV", _export.to_csv(index=False).encode("utf-8"),
    file_name="donors_filtered.csv", mime="text/csv", use_container_width=True,
)

# Pagination
if per_page == "All" or len(fdf) <= int(per_page):
    page_df = fdf
else:
    pp = int(per_page)
    pages = max(1, math.ceil(len(fdf) / pp))
    pg = min(st.session_state.get("donor_page", 1), pages)
    nav = st.columns([1, 2, 1])
    if nav[0].button("← Prev", disabled=pg <= 1, use_container_width=True):
        st.session_state["donor_page"] = pg - 1
        st.rerun()
    nav[1].markdown(f"<div style='text-align:center'>Page {pg} of {pages}</div>", unsafe_allow_html=True)
    if nav[2].button("Next →", disabled=pg >= pages, use_container_width=True):
        st.session_state["donor_page"] = pg + 1
        st.rerun()
    page_df = fdf.iloc[(pg - 1) * pp: (pg - 1) * pp + pp]

# Row table with per-row actions
h = st.columns([3, 1.3, 2.4, 1, 1.4])
for col, lbl in zip(h, ["Donor", "Short", "Category", "Verif.", "Actions"]):
    col.markdown(f"**{lbl}**")
st.markdown("<hr style='margin:2px 0 6px'/>", unsafe_allow_html=True)

for _, r in page_df.iterrows():
    rd = r.to_dict()
    c = st.columns([3, 1.3, 2.4, 1, 1.4])
    c[0].write(rd.get("donor") or "—")
    c[1].write(rd.get("donor_short") or "—")
    c[2].write(rd.get("donor_category") or "—")
    c[3].write(rd.get("verification_level") or "—")
    ac = c[4].columns(3)
    if ac[0].button("✏️", key=f"e_{rd['canonical_key']}", disabled=not can_edit, help="Edit"):
        _edit_dialog(rd)
    if ac[1].button("🗑", key=f"d_{rd['canonical_key']}", disabled=not can_edit, help="Delete"):
        _delete_dialog(rd)
    if ac[2].button("🔗", key=f"s_{rd['canonical_key']}", help="Share"):
        _share_dialog(rd)
