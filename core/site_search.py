"""Site-wide search powering the header 🔍 icon.

Two result kinds:
  * NAV   — pages + tabs + features, from a static registry. Lets users jump
            anywhere by typing a page/tab/feature name (e.g. "blacklist",
            "manage users", "screen", "engagements").
  * DATA  — live matches in the org's content: opportunity titles + funders
            (→ Pipelines) and donor names (→ Donors).

Streamlit can't deep-link an individual tab, so a "Pipelines · Review" hit
navigates to the Pipelines page; the label tells the user which tab to open.
"""
from __future__ import annotations

import streamlit as st

from db.supabase_client import get_client, safe_execute

# (label, keyword string, page_path, admin_only)
_NAV: list[tuple[str, str, str, bool]] = [
    ("Home", "dashboard start overview quick", "app_pages/home.py", False),
    ("Pipelines", "screen review tracking summary screening triage lifecycle",
     "app_pages/pipelines.py", False),
    ("Pipelines · Screen", "screening new opportunities triage auto",
     "app_pages/pipelines.py", False),
    ("Pipelines · Review", "review eligibility criteria badges decision",
     "app_pages/pipelines.py", False),
    ("Pipelines · Tracking", "tracking pipeline stage progress proposal",
     "app_pages/pipelines.py", False),
    ("Pipelines · Summary", "summary stats overview", "app_pages/pipelines.py", False),
    ("Grants", "applied funding awarded won reporting deadlines",
     "app_pages/grants.py", False),
    ("Actions", "meetings engagements pending follow ups todo tasks check-ins",
     "app_pages/actions.py", False),
    ("Report", "analytics kpi dashboard charts export pdf metrics",
     "app_pages/report.py", False),
    ("Donors", "donor intelligence contacts catalogue funders multilateral",
     "app_pages/donors.py", False),
    ("Profile", "my profile change password account email phone",
     "app_pages/profile.py", False),
    ("Help", "guide how to faq orientation support", "app_pages/help.py", False),
    ("Settings · Setup", "settings setup org organization year currency excel sync rates",
     "app_pages/admin.py", True),
    ("Settings · Manage Users", "users accounts roles add user reset password deactivate invite",
     "app_pages/admin.py", True),
    ("Settings · User Access", "access matrix permissions overrides roles",
     "app_pages/admin.py", True),
    ("Settings · Records", "records data rfp backend edit delete export share columns",
     "app_pages/admin.py", True),
    ("Settings · Sources", "donor sources scan urls catalog feeds",
     "app_pages/admin.py", True),
    ("Settings · Manual Scan", "manual scan trigger run now history logs",
     "app_pages/admin.py", True),
    ("Settings · Blacklist", "blacklist blocked domains exclude suppress",
     "app_pages/admin.py", True),
]


def search_nav(query: str, is_admin: bool) -> list[tuple[str, str]]:
    """Return [(label, page_path)] of nav targets matching `query`, best first."""
    q = (query or "").strip().lower()
    if not q:
        return []
    out: list[tuple[int, str, str]] = []
    for label, keywords, path, admin_only in _NAV:
        if admin_only and not is_admin:
            continue
        ll = label.lower()
        score = 0
        if q in ll:
            # Prefix match on the (sub)tab name ranks highest.
            score = 100 - ll.index(q)
        elif any(q in tok for tok in keywords.split()):
            score = 40
        elif all(w in (ll + " " + keywords) for w in q.split()):
            score = 20
        if score:
            out.append((score, label, path))
    out.sort(key=lambda x: x[0], reverse=True)
    return [(label, path) for _, label, path in out]


def _sanitize(query: str) -> str:
    """Strip PostgREST `or_` delimiters (comma / parens) from the user value
    so a typed comma can't break the filter — they act as ilike wildcards
    once removed anyway."""
    return (query or "").replace(",", " ").replace("(", " ").replace(")", " ").strip()


@st.cache_data(ttl=30, show_spinner=False)
def search_opportunities(query: str, limit: int = 30) -> list[dict]:
    """Opportunity matches across title / funder / brief / focus theme.

    Returns dicts: {title, funder, deadline, decision, source, page}.
    """
    qf = _sanitize(query)
    if len(qf) < 2:
        return []
    sb = get_client()
    try:
        rows = (safe_execute(
            sb.table("rfp_submissions")
            .select("opportunity_title,funding_agency,call_submission_deadline,"
                    "decision,source")
            .or_(f"opportunity_title.ilike.%{qf}%,"
                 f"funding_agency.ilike.%{qf}%,"
                 f"brief_description.ilike.%{qf}%,"
                 f"focus_theme.ilike.%{qf}%")
            .limit(limit)).data or [])
    except Exception:
        rows = []
    out, seen = [], set()
    for r in rows:
        title = (r.get("opportunity_title") or "").strip()
        funder = (r.get("funding_agency") or "").strip()
        key = (title.lower(), funder.lower())
        if not title or key in seen:
            continue
        seen.add(key)
        dl = r.get("call_submission_deadline")
        out.append({
            "title": title,
            "funder": funder,
            "deadline": (str(dl)[:10] if dl else ""),
            "decision": (r.get("decision") or "").strip(),
            "source": (r.get("source") or "").strip(),
            "page": "app_pages/pipelines.py",
        })
    return out


@st.cache_data(ttl=30, show_spinner=False)
def search_donors(query: str, limit: int = 30) -> list[dict]:
    """Donor matches by name. Returns dicts: {name, page}."""
    qf = _sanitize(query)
    if len(qf) < 2:
        return []
    sb = get_client()
    try:
        rows = (safe_execute(
            sb.table("donor_sources").select("donor_name")
            .ilike("donor_name", f"%{qf}%").limit(limit)).data or [])
    except Exception:
        rows = []
    out, seen = [], set()
    for d in rows:
        name = (d.get("donor_name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "page": "app_pages/donors.py"})
    return out
