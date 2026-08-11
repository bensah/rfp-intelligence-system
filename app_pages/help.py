"""Help page — reached from the top-right user menu (❓).

A navigation-first orientation: where things live, what each page/tab does,
and how the weekly opportunity workflow flows. Role-aware — admins also see a
Settings section. Content is kept in step with the live app (nav in App.py,
tabs in the page files, Settings IA in app_pages/admin.py).
"""
from __future__ import annotations

import streamlit as st

from core import permissions, settings
from views.account_sections import ADMIN_CONTACT_EMAIL

user = st.session_state.get("app_user") or {}
is_admin = permissions.is_admin(user)
_is_super = permissions.is_super_user(user)
_name = user.get("name") or user.get("email") or "there"
try:
    _role = permissions.role_label(user)
except Exception:
    _role = (user.get("role") or "collaborator").title()
try:
    _org = settings.get_org_name() or "your organization"
except Exception:
    _org = "your organization"


# ── Styling ───────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
      .rfpis-hero { border-left:4px solid #00703C; background:#f1f7f4;
        padding:14px 18px; border-radius:0 12px 12px 0; margin:.1rem 0 .6rem;
        color:#14532d; line-height:1.45; }
      .rfpis-chips { display:flex; flex-wrap:wrap; gap:8px; margin:.2rem 0 1rem; }
      .rfpis-chip { background:#e6f2eb; color:#00703C; border:1px solid #cfe6da;
        padding:4px 12px; border-radius:999px; font-size:.82rem; font-weight:600; }
      .rfpis-chip.alt { background:#eef2f6; color:#0f3d6e; border-color:#dbe4ee; }
      div[data-testid="stVerticalBlockBorderWrapper"] { border-radius:12px; }
      div[data-testid="stVerticalBlockBorderWrapper"] h4 { margin:.1rem 0 .55rem 0;
        color:#00703C; font-size:1.05rem; }
      .rfpis-flow { display:flex; flex-wrap:wrap; align-items:stretch; gap:10px;
        margin:.5rem 0 .2rem; }
      .rfpis-flow .step { flex:1 1 150px; display:flex; gap:11px; background:#f8fafc;
        border:1px solid #e6eaef; border-radius:10px; padding:11px 13px; }
      .rfpis-flow .step-n { flex:0 0 26px; height:26px; width:26px; border-radius:50%;
        background:#00703C; color:#fff; font-weight:700; display:flex;
        align-items:center; justify-content:center; font-size:.85rem; }
      .rfpis-flow .step-b { display:flex; flex-direction:column; gap:1px; }
      .rfpis-flow .step-b b { color:#0f172a; font-size:.94rem; }
      .rfpis-flow .step-b span { color:#64748b; font-size:.81rem; line-height:1.28; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Hero ──────────────────────────────────────────────────────────────────────
st.title("❓ Help & navigation guide")
st.markdown(
    "<div class='rfpis-hero'><b>RFPIS</b> discovers funding opportunities "
    "(RFPs, RFIs, EOIs, calls for proposals, grand challenges) from donor sites "
    "and the web, screens each against your eligibility rules, and helps the team "
    "decide <b>Proceed / Park / Decline</b> — all in one place.</div>",
    unsafe_allow_html=True,
)
st.markdown(
    f"<div class='rfpis-chips'>"
    f"<span class='rfpis-chip'>👋 {_name}</span>"
    f"<span class='rfpis-chip alt'>Role · {_role}</span>"
    f"<span class='rfpis-chip alt'>Org · {_org}</span>"
    f"</div>",
    unsafe_allow_html=True,
)
st.caption(f"Anything this guide doesn't answer? **Ask an administrator** — "
           f"{ADMIN_CONTACT_EMAIL}.")

# ── Getting around ──────────────────────────────────────────────────────────
with st.container(border=True):
    st.markdown("#### 🧭 Getting around")
    st.markdown(
        "There are two navigation zones:\n"
        "- **Left sidebar** — your six everyday work pages. Use the **«/»** "
        "control to expand it to labels or collapse it to an icon rail; your "
        "**role** shows at the bottom.\n"
        "- **Top-right icons** — 🔍 **Search**, 🔔 **Notifications**, and the "
        "👤 **person menu**.\n\n"
        "Most pages carry **tabs** along the top — that's where the detail lives.")

# ── Pages + top-right tools (side by side on wide screens) ────────────────────
_cL, _cR = st.columns(2)
with _cL:
    with st.container(border=True):
        st.markdown("#### 📋 The pages (left sidebar)")
        st.markdown(
            "- **🏠 Home** — dashboard: pending actions, recent activity, and "
            "quick links to submit or review an opportunity.\n"
            "- **📚 Pipelines** — the core workflow in four tabs: **Screen** → "
            "**Review** → **Track** → **Summary**.\n"
            "- **💼 Grants** — opportunities won or under active management, "
            "with reporting deadlines.\n"
            "- **🗒️ Actions** — four tabs: **Meetings** (team-call notes), "
            "**Engagements** (donor touchpoints), **Pending** (open follow-ups), "
            "**Schedule** (the weekly team-call rota).\n"
            "- **📊 Report** — analytics across the whole pipeline (volume by "
            "donor, decision mix, team activity), downloadable.\n"
            "- **🗺️ Donors** — the donor catalogue + intelligence: focus areas, "
            "award ranges, and contacts.")
with _cR:
    with st.container(border=True):
        st.markdown("#### 🔝 Top-right tools")
        _menu = "**Organization**, **Profile**, **Help**"
        if is_admin:
            _menu += ", **Settings**"
        _menu += ", and **Sign Out**"
        st.markdown(
            "- **🔍 Search** — type a keyword and **Search** to jump to matching "
            "**pages & tabs**, **opportunities**, and **donors**. It can also "
            "search the **web** for live, validated, recent funding calls.\n"
            "- **🔔 Notifications** — your organization's activity feed: when "
            "scans run, the next scheduled scan, and newly added opportunities. "
            "The badge counts items since you last hit **Mark all as read**.\n"
            f"- **👤 Person menu** — {_menu}.\n"
            "- **🏢 Organization** — your org's profile: identity, eligibility, "
            "program areas, and funders (admins can edit).")

# ── Workflow ────────────────────────────────────────────────────────────────
with st.container(border=True):
    st.markdown("#### 🔄 How an opportunity flows")
    st.markdown(
        "<div class='rfpis-flow'>"
        "<div class='step'><div class='step-n'>1</div><div class='step-b'>"
        "<b>Discovered</b><span>The auto-scan or a manual submission lands it "
        "in <b>Screen</b>.</span></div></div>"
        "<div class='step'><div class='step-n'>2</div><div class='step-b'>"
        "<b>Screened</b><span>Auto-scoring tags eligibility (MUST 1–5 / PREFER "
        "6–9) and proposes a decision.</span></div></div>"
        "<div class='step'><div class='step-n'>3</div><div class='step-b'>"
        "<b>Reviewed</b><span>On <b>Review</b>, the team confirms or overrides "
        "— Proceed / Park / Decline.</span></div></div>"
        "<div class='step'><div class='step-n'>4</div><div class='step-b'>"
        "<b>Tracked</b><span>A <b>Proceed</b> moves to <b>Track</b> with an "
        "owner + deadlines; wins graduate to <b>Grants</b>.</span></div></div>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Funding calls are a **shared, platform-wide pool** — every organization's "
        "eligibility scan screens the whole pool against its own preferences, so a "
        "call surfaces wherever it fits. An opportunity **submitted by any user** joins "
        "that pool too, becoming available to everyone on their next scan.")

# ── How scoring works ────────────────────────────────────────────────────────
# MOVED HERE from the opportunity page (owner, 2026-08-11). The per-criterion notes explained
# the weighting model inline, between one criterion and the next — true, and useful to somebody
# editing components, but a paragraph of scoring internals for a reader who only wants the
# verdict. Explained once here instead of on every call.
_MUSTS = [
    "**The five MUSTs** (65% of the score)", "",
    "1. Legal status & qualification",
    "2. Strategic fit",
    "3. Implementation capacity",
    "4. Geographic fit",
    "5. Compliance requirements", "",
    "**The four PREFERs** (35%)", "",
    "6. Funding quality",
    "7. Donor relationship",
    "8. Competitiveness",
    "9. Bid effort",
]
_VERDICTS = [
    "**What the verdicts mean**", "",
    "- **Proceed** — 90 or above",
    "- **Park** — 70 to 89, worth a human look",
    "- **Decline** — below 70",
    "- a fatal gate declines outright: a structural ineligibility that cannot be fixed "
    "before the deadline",
    "- **not scored** means the call stated nothing to score — it takes the Park midpoint "
    "rather than counting against you",
]
_DIVERGE = [
    "The count is the EVIDENCE — how many of the criterion's components were met. The "
    "verdict is the SCORE. They are not the same measure, and two things make them diverge:",
    "",
    "- **Gate criteria.** MUST-5 spans compliance requirements, and any unmet requirement "
    "makes it *Not met* — so it shows what is unmet rather than a percentage, because a "
    "percentage would imply partial credit the score does not give.",
    "- **Weighted criteria.** Funding quality and Competitiveness are models, not averages: "
    "a track record counts one and a half times, and unmet donor requirements subtract. A "
    "flat ratio over the components cannot express that, so the model names the verdict and "
    "the ratio stays beside it as evidence.",
]
with st.container(border=True):
    st.subheader("How an opportunity is scored")
    st.markdown("Every opportunity is read against **nine criteria** — five MUSTs and four "
                "PREFERs — and each contributes points out of its own weight.")
    _sL, _sR = st.columns(2)
    _sL.markdown(chr(10).join(_MUSTS))
    _sR.markdown(chr(10).join(_VERDICTS))
    st.markdown("**Why a verdict can differ from the count beside it**")
    st.markdown(chr(10).join(_DIVERGE))
    st.caption("Reviewers can change any component in **Update Decision**, and the verdict "
               "follows. A human who has read the call outranks the derivation.")


# ── Account + contact (side by side) ──────────────────────────────────────────
_aL, _aR = st.columns(2)
with _aL:
    with st.container(border=True):
        st.markdown("#### 👤 Your account & role")
        st.markdown(
            "- **👤 → Profile** — edit your name and contact details or "
            "**change your password**.\n"
            "- Your **role** shows at the bottom of the sidebar — most "
            "teammates are **Contributors**.\n"
            "- Need more access? Ask an administrator to adjust your role under "
            "**Settings → Accounts → Users**.\n"
            "- **Delete your account** — **Profile → My Profile → ⚠️ Danger "
            "zone**. This removes your login only; your contributed records "
            "stay in the system for institutional memory.")
with _aR:
    with st.container(border=True):
        st.markdown("#### ✉️ Contacting an administrator")
        st.markdown(
            "Wherever the app says **“ask an administrator”** — a role change, "
            "a locked action, a suspended organization, or a data request — "
            f"reach the app owner (Super User) at **{ADMIN_CONTACT_EMAIL}**.")
        st.info(
            "**Closing an organization.** An org admin can **suspend** their "
            "organization under **Settings → Setup → Danger zone**: auto-scans "
            "stop and the account is retired, **but all records are kept** for "
            "later retrieval — nothing is deleted from the app. **Reactivating "
            "a suspended org, or permanently deleting its data, is done by the "
            f"Super User only** — email **{ADMIN_CONTACT_EMAIL}**.")

# ── Admins only ─────────────────────────────────────────────────────────────
if is_admin:
    with st.container(border=True):
        st.markdown("#### ⚙️ Settings (administrators)")
        st.markdown(
            "Open from **👤 → Settings**:\n"
            "- **Setup** — org profile, working year, currencies, and scan "
            "eligibility (health themes, geographic scope, criteria). Includes "
            "the **Danger zone** to suspend this organization's account.\n"
            "- **Accounts** — **Users** (add/edit teammates, set roles, reset "
            "passwords; pick a user to see their **User Access Privileges** "
            "inline)"
            + (", **Tenants** (every organization on the platform, with "
               "view-as), and **Blacklisted** (hard-blocked users / tenants, "
               "with undo)" if _is_super else "") + ".\n"
            "- **Records** — the full opportunity backend in **Data · Verify · "
            "Reset** (view / edit / delete, export, share). The **Reset** tab holds "
            "**backup + maintenance** tools and is **Super-User-only**.\n"
            "- **Sources** — **Validated** (the donor-site catalogue the scanner "
            "crawls), **Verify Registry** (classify hosts aggregator-vs-primary + push "
            "new sources to the catalogue), and **Blocked** (URL tokens excluded from "
            "scans + web search).\n"
            "- **Manual Scan** — run a full **Extraction** (crawl every source), "
            "**Eligibility Scan** (fast re-screen against this org), or **Sync Excel** "
            "(import a master workbook into this tenant, when one is configured), and "
            "view scan history.\n"
            "- **Learning data** — captured scan / decision / feedback signals "
            "that train the scoring model."
            + ("\n- **Analytics** — cross-tenant platform stats and the "
               "system-wide discovery counter (Super User)." if _is_super
               else ""))

st.divider()
st.caption(f"{_org} · powered by RFPIS")
