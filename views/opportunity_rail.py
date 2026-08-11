"""Live opportunity right-rail — three cards shown beside the Entity view and on the
Pipeline page:

  💰 Top Funding      — biggest / most-urgent calls, fit-agnostic
  ✅ Top Matches      — strong fit (Proceed / Park / high alignment)
  ✨ Also Interesting — fresh non-matches worth a glance

Reads the CURRENT entity's pipeline (rfp_submissions — tenant-scoped by get_client, so a
super_user's 'view-as' shows the viewed entity, and public/individual tenants' rows are
included per the scoping wrapper). Cached ~45s so it refreshes as new calls land without
hammering the DB — a live feed, not a static card. Classification lives in
core.opportunity_feed.
"""
from __future__ import annotations

from urllib.parse import quote as _quote

import streamlit as st

from core import opportunity_feed as _feed
from db.supabase_client import get_client, _tenant_scope_tid

_FIELDS = ("uid,opportunity_title,funding_agency,call_award_value,currency,"
           "call_submission_deadline,call_geographic_scope,date_posted,search_date,"
           "created_at,alignment_score,auto_recommendation,decision,opportunity_link,"
           # brief_description + focus_theme feed the geo hard-gate's text detection
           # (auto_scorer._geo_text) so Top Funding excludes off-geography calls robustly.
           "brief_description,focus_theme")


@st.cache_data(ttl=45, show_spinner=False)
def _load_pipeline(scope_key: str) -> list[dict]:
    """Recent pipeline rows for the effective tenant. `scope_key` (the tenant id) is part
    of the cache key so tenants / view-as never share a cached feed."""
    try:
        return (get_client().table("rfp_submissions").select(_FIELDS)
                .order("created_at", desc=True).limit(800).execute().data or [])
    except Exception:
        return []


_CATALOG_FIELDS = ("uid,opportunity_name,opportunity_url,funder_name,deadline,"
                   "grant_amount,currency,call_geographic_scope,call_domain_areas,"
                   "focus_themes,brief_description,solicitation_type,instrument_type,"
                   "opportunity_type,date_posted")


@st.cache_data(ttl=120, show_spinner=False)
def _load_catalog() -> list[dict]:
    """The SHARED extracted catalog — every call the crawl found, whether or not it passed
    THIS tenant's gate. Deliberately org-agnostic and not tenant-scoped: it is the pool the
    Featured card ranks, which is what makes a screening miss recoverable."""
    try:
        from db.supabase_client import service_client
        return (service_client().table("extracted_solicitations").select(_CATALOG_FIELDS)
                .order("scraped_at", desc=True).limit(1200).execute().data or [])
    except Exception:
        return []


@st.cache_data(ttl=120, show_spinner=False)
def _tenant_prefs(scope_key: str) -> dict:
    """What this tenant SAYS it wants (configured policy) plus what it actually DOES
    (funders engaged, programme areas pursued). Behaviour is the honest signal — a tenant's
    themes list goes stale, their submission history doesn't."""
    prefs: dict = {}
    try:
        from core.policies import get_policies
        pol = get_policies() or {}
        countries = (pol.get("countries") or {})
        prefs["countries"] = countries.get("eligible") or []
        prefs["broad_terms"] = countries.get("broad_terms") or []
        _th = pol.get("themes")
        prefs["themes"] = (_th.get("required_any") if isinstance(_th, dict) else _th) or []
    except Exception:
        pass
    try:
        from core import org_profile as _orgp
        prof = _orgp.get_profile() or {}
        prefs["known_funders"] = list(prof.get("org_engaged_donors") or []) +                                  list(prof.get("org_active_donors") or [])
    except Exception:
        prefs.setdefault("known_funders", [])
    # Behaviour: areas they actually pursued + funders they actually applied to.
    try:
        rows = (get_client().table("rfp_submissions")
                .select("funding_agency,call_domain_areas,decision")
                .limit(800).execute().data or [])
        areas, funders = [], []
        for r in rows:
            if str(r.get("decision") or "").lower().startswith("proceed"):
                funders.append(r.get("funding_agency"))
                areas.extend(r.get("call_domain_areas") or [])
        prefs["pursued_areas"] = [a for a in areas if a]
        prefs["known_funders"] = (prefs.get("known_funders") or []) + [f for f in funders if f]
    except Exception:
        pass
    return prefs


@st.cache_data(ttl=45, show_spinner=False)
def _pipeline_links(scope_key: str) -> list[str]:
    """Normalised links already in this tenant's pipeline — so Featured never repeats
    something they can already see in Review."""
    try:
        rows = (get_client().table("rfp_submissions").select("opportunity_link")
                .limit(2000).execute().data or [])
        return [str(r.get("opportunity_link") or "").strip().lower().rstrip("/")
                for r in rows if r.get("opportunity_link")]
    except Exception:
        return []


def _fmt_amount(amount: float, currency: str) -> str:
    if not amount or amount <= 0:
        return ""
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get((currency or "").upper(), "")
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if amount >= div:
            return f"{sym}{amount / div:.1f}{unit}".replace(".0", "")
    return f"{sym}{amount:,.0f}"


def _deadline_chip(item: dict) -> str:
    d = item.get("days_until")
    if d is None:
        return ""
    if d < 0:
        return "⏳ closed"
    if d == 0:
        return "⏳ due today"
    if d <= 14:
        return f"🔴 {d}d left"
    if d <= 45:
        return f"🟠 {d}d left"
    return f"🗓 {d}d"


def _render_item(item: dict) -> None:
    # Title links to THAT opportunity's own page, carrying its uid. Every title used to
    # link to the bare `/pipelines` — the same destination for all of them, so the click
    # told you nothing and you still had to find the row by hand. Worse, a FEATURED item
    # comes from the shared catalogue and is not in rfp_submissions at all, so no
    # pipeline page could show it; /opportunity resolves both stores and offers
    # "Track this opportunity" for the catalogue ones.
    title = (item["title"][:70] + "…") if len(item["title"]) > 70 else item["title"]
    uid = str(item.get("uid") or "").strip()
    if uid:
        st.markdown(f"**[{title}](/opportunity?uid={_quote(uid)})**")
    else:
        # No uid to link to (shouldn't happen — both stores carry one). Don't emit a link
        # that goes nowhere useful; the external ↗ below is still offered.
        st.markdown(f"**{title}**")
    bits = [b for b in (item.get("funder", "")[:34],
                        _fmt_amount(item["amount"], item["currency"]),
                        _deadline_chip(item)) if b]
    line = "  ·  ".join(bits)
    if item.get("link"):
        line += f"  ·  [↗]({item['link']})"
    if line:
        st.caption(line)
    # A FEATURED item must justify itself — it wasn't screened into this pipeline, so
    # without a reason it reads as noise rather than a recovered miss.
    if item.get("_why"):
        st.caption(f":green[**Why:** {item['_why']}]")


def _card(title: str, help_txt: str, items: list[dict], empty: str) -> None:
    with st.container(border=True):
        st.markdown(f"#### {title}")
        st.caption(help_txt)
        if not items:
            st.caption(f"_{empty}_")
            return
        for i, it in enumerate(items):
            _render_item(it)
            if i < len(items) - 1:
                st.divider()


def render_opportunity_rail() -> None:
    """Render the three live cards. Safe to call in any column/container."""
    scope = _tenant_scope_tid() or "all"
    # Bind the org's HARD geographic gate (same predicate the screener uses) so the
    # fit-agnostic cards never feature calls that geographically exclude this tenant —
    # e.g. a Samoa-only call to a Congo-DRC entity. Global / non-geo-tagged calls pass
    # the gate untouched, so they remain as the natural fallback. Unconfigured geo (or a
    # super's 'all' scope) → empty org set → the gate defers (no filtering).
    _geo_reject = None
    try:
        from core.policies import get_policies as _get_policies
        from core.auto_scorer import geographic_exclusion_reject as _geo_rej
        _pol = _get_policies()
        _geo_reject = lambda r: _geo_rej(r, _pol)[0]        # noqa: E731
    except Exception:
        _geo_reject = None
    groups = _feed.classify(_load_pipeline(f"t:{scope}"), geo_reject=_geo_reject)

    st.markdown("<div class='app-rail-marker' style='font-size:.8rem;color:#00703C;"
                "font-weight:700;letter-spacing:.03em;'>🔴 LIVE OPPORTUNITY FEED</div>",
                unsafe_allow_html=True)
    # The old caption sent the reader to Pipelines → Review to open an item, which stopped
    # being true once every entry here linked straight to its own opportunity page.
    st.caption("Every title opens that opportunity in full.")
    # ORDER: discovery first, then fit, then size, then the rest (owner, 2026-08-11).
    # Featured leads because it is ranked from the WHOLE catalogue rather than this tenant's
    # pipeline, so it is the only card that can surface a call screening never reached — the
    # thing a reader is least likely to find any other way. Top Matches then answers "is it
    # for us", and Top Funding "how big is it", which only matters once the first two do.
    try:
        _featured = _feed.featured(
            _load_catalog(), _tenant_prefs(f"t:{scope}"),
            seen_keys=set(_pipeline_links(f"t:{scope}")))
    except Exception:
        _featured = []
    _card("🎯 Featured for you",
          "Ranked from the whole catalog against your geography, programme areas and the "
          "funders you work with — including calls your screening didn't pick up.",
          _featured, "Nothing to feature yet — run an extraction.")
    _card("✅ Top Matches", "Strong fit for this entity (Proceed / Park / high alignment).",
          groups["top_matches"], "No strong matches yet.")
    _card("💰 Top Funding", "Biggest / most-urgent calls your entity is geographically "
          "eligible for.",
          groups["top_funding"], "No live opportunities yet — run a scan.")
    _card("✨ Also Interesting", "Fresh calls that aren't a match but are worth a look.",
          groups["other"], "Nothing else new right now.")
