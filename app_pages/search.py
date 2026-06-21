"""Site-wide search results page.

Reached from the header 🔍 box (which stows the query in
st.session_state['site_search_query'] and switches here). Works like a
mini search engine scoped to the app: a query box at the top, then top
matching links grouped by kind — Pages & tabs, Opportunities, Donors —
each a clickable link that navigates to the relevant page.

Hidden from the sidebar nav (registered in App.py but CSS-hidden in
core/app_header.py, same as Profile / Help / Settings).
"""
from __future__ import annotations

import html
import urllib.parse as _urlparse

import streamlit as st

from core import permissions, site_search, web_search

user = st.session_state.get("app_user") or {}
is_admin = permissions.is_admin(user)

# Borderless, desaturated (colorless) feedback emoji buttons — they sit beside the
# result title like a superscript rather than in boxes. The ➕ Track button keeps
# its box and its own aligned column. Disabled (chosen) state stays greyed.
st.markdown(
    """<style>
    [class*="st-key-fbgood"] button, [class*="st-key-fbneut"] button,
    [class*="st-key-fbbad"] button {
        border:none!important; background:transparent!important;
        box-shadow:none!important; padding:0 .12rem!important; min-height:0!important;
        line-height:1!important; filter:grayscale(1); font-size:1rem;
    }
    [class*="st-key-fbgood"] button:disabled, [class*="st-key-fbneut"] button:disabled,
    [class*="st-key-fbbad"] button:disabled { opacity:.4; filter:grayscale(1); }
    </style>""",
    unsafe_allow_html=True)


def _render_result_actions(wr: dict, *, key: str, user: dict, cols) -> None:
    """Render the 👍/😐/👎 feedback + ➕ Track controls into the supplied column
    objects (`cols` = [g, n, b, track]) so they sit on the SAME ROW as the result
    text. Best-effort — a failure here never breaks the results list."""
    from core import decision_log, found_loader

    cand = found_loader.candidate_from_web_result(wr)
    email = (user or {}).get("email")
    # Per-result state keyed by the link so a rating / track survives paging and
    # greys the chosen button (no accidental re-clicks).
    link = (wr.get("link") or "").strip()
    fb_done = st.session_state.setdefault("_srch_fb", {})
    tracked = st.session_state.setdefault("_srch_tracked", set())
    cur = fb_done.get(link)
    is_tracked = link in tracked

    c1, c2, c3, c4 = cols
    # Feedback emojis render borderless + grayscale (CSS targets the st-key-fb*
    # wrappers) so they read like a superscript beside the title, not boxed.
    g = c1.button("✓👍" if cur == "good" else "👍", key=f"fbgood_{key}",
                  disabled=cur == "good", help="Good — like a Proceed.")
    n = c2.button("✓😐" if cur == "neutral" else "😐", key=f"fbneut_{key}",
                  disabled=cur == "neutral", help="Neutral — like a Park.")
    b = c3.button("✓👎" if cur == "bad" else "👎", key=f"fbbad_{key}",
                  disabled=cur == "bad", help="Bad — like a Decline.")
    track = c4.button("✓ Tracked" if is_tracked else "➕ Track", key=f"track_{key}",
                      disabled=is_tracked,
                      help="Load into Found Records as a scored candidate "
                           "awaiting review.")
    if g or n or b:
        verdict = "good" if g else "neutral" if n else "bad"
        try:
            decision_log.log_feedback(cand, verdict, by=email,
                                      reason="search-result")
            fb_done[link] = verdict
            st.toast("Thanks — that trains the scorer.", icon="🧠")
        except Exception as exc:
            st.toast(f"Couldn't record: {exc}", icon="⚠️")
        st.rerun()
    if track:
        res = found_loader.load_candidate(cand, user, provenance="search")
        if res["ok"]:
            tracked.add(link)
            st.toast(f"Tracked as {res['uid']} (auto-rec: {res['reason']}).",
                     icon="➕")
        elif res["skipped"]:
            tracked.add(link)
            st.toast("Already tracked — not re-added.", icon="ℹ️")
        else:
            st.toast(f"Couldn't track: {res['reason']}", icon="⚠️")
        st.rerun()


st.title("🔍 Search")

# ── Query box (pre-filled, editable to refine) ──────────────────────────────
# The query lives in the URL (?q=…), so a refresh or a shared link reproduces
# the same search and keeps you on this page. The URL value wins over the
# session value the header search stashes.
_url_q = (st.query_params.get("q") or "").strip()
_sess_q = (st.session_state.get("site_search_query") or "").strip()
current_q = _url_q or _sess_q

with st.form("site_search_form", clear_on_submit=False):
    c1, c2 = st.columns([6, 1])
    q_in = c1.text_input(
        "Search the site", value=current_q,
        placeholder="Search pages, tabs, opportunities, donors…",
        label_visibility="collapsed")
    submitted = c2.form_submit_button("Search", type="primary",
                                      width='stretch')
if submitted:
    current_q = (q_in or "").strip()

# Sync session + URL to the active query → unique /search?q=… that survives a
# refresh. Guard the assignment so it doesn't loop.
st.session_state["site_search_query"] = current_q
if current_q:
    if st.query_params.get("q") != current_q:
        st.query_params["q"] = current_q
elif "q" in st.query_params:
    del st.query_params["q"]

q = current_q
if len(q) < 2:
    st.info("Type a keyword above (2+ characters) to search across pages, "
            "tabs, opportunities and donors.")
    st.stop()

# Once a web search has been run for THIS query, collapse the in-app result
# groups so the user can focus on the web results (they can re-open them).
web_active = st.session_state.get("_web_search_for") == q

# ── Run the search ──────────────────────────────────────────────────────────
nav = site_search.search_nav(q, is_admin)
opps = site_search.search_opportunities(q)
donors = site_search.search_donors(q)
total = len(nav) + len(opps) + len(donors)

st.caption(f"{total} result{'s' if total != 1 else ''} in this app for “{q}”")
if total == 0:
    st.warning("No in-app matches. Try fewer or different keywords — e.g. a "
               "donor name, a page like *blacklist* or *manage users*, or part "
               "of an opportunity title. You can also search the web below.")

# In-app result groups are collapsible expanders (expanded by default) so you
# can roll them up to get straight to the web results below.
# ── Pages & tabs ────────────────────────────────────────────────────────────
if nav:
    with st.expander(f"📑 Pages & tabs · {len(nav)}", expanded=not web_active):
        for label, path in nav:
            st.page_link(path, label=label, icon="➡️")

# ── Opportunities ───────────────────────────────────────────────────────────
if opps:
    with st.expander(f"📄 Opportunities · {len(opps)}", expanded=not web_active):
        for o in opps:
            st.page_link(o["page"], label=o["title"], icon="📄")
            meta = " · ".join(p for p in (
                o.get("funder"),
                (f"Deadline {o['deadline']}" if o.get("deadline") else ""),
                (o.get("decision") or "").title(),
                (o.get("source") or "").title(),
            ) if p)
            if meta:
                st.caption(meta)

# ── Donors ──────────────────────────────────────────────────────────────────
if donors:
    with st.expander(f"🗺️ Donors · {len(donors)}", expanded=not web_active):
        for d in donors:
            st.page_link(d["page"], label=d["name"], icon="🗺️")

# ── Web discovery (Tavily) ──────────────────────────────────────────────────
st.divider()
st.subheader("🌐 Opportunities on the web")
if not web_search.available():
    st.info(
        "**Web search isn't configured yet.** An admin can enable it by adding "
        "`SERPER_API_KEY` (real Google results, 2,500 free queries from "
        "[serper.dev](https://serper.dev)) and/or `TAVILY_API_KEY` "
        "([app.tavily.com](https://app.tavily.com/)) to the app secrets. Once "
        "set, one keyword fans out into several RFP-framed searches and keeps "
        "only results that pass your health-theme + RFP-signal rules and "
        "blacklist — filtering out the noise.")
else:
    st.caption(
        "Type one keyword (e.g. *health* or *malaria*) — it auto-expands into "
        "RFP-framed searches across every configured engine (Google/Serper, "
        "Tavily, Exa), then keeps only results that pass your health-theme + "
        "RFP-signal rules and blacklist. Broad terms sweep the whole portfolio; "
        "specific terms stay focused.")
    wb1, wb2, _wsp = st.columns([1.7, 1.2, 4])
    if wb1.button(f"🌐 Search the web for “{q}”", key="web_search_btn",
                  type="primary", width='stretch'):
        st.session_state["_web_search_for"] = q
        st.query_params.pop("p", None)  # fresh search → back to page 1
        st.rerun()  # restart so the in-app groups collapse before web results
    if wb2.button("🔄 Refresh results", key="web_search_refresh",
                  width='stretch',
                  help="Re-run the web search, bypassing the 15-min cache."):
        web_search.search.clear()       # drop cached results → re-query Tavily
        st.session_state["_web_search_for"] = q
        st.query_params.pop("p", None)
        st.rerun()
    if st.session_state.get("_web_search_for") == q:
        with st.spinner("Searching the web…"):
            res = web_search.search(q, num=30)
        if not res.get("ok"):
            st.warning("Web search unavailable: "
                       f"{res.get('error') or 'unknown error'}")
        elif not res.get("results"):
            if res.get("mode") == "lookup":
                st.caption(
                    f"No primary-source match for that exact title "
                    f"(checked {res.get('raw_count', 0)} hits"
                    + (f", skipped {res['dropped_aggregator']} aggregator/blog"
                       if res.get("dropped_aggregator") else "")
                    + "). The opportunity may only live on an aggregator, or the "
                    "title wording differs on the donor's site — try trimming to "
                    "the distinctive part of the title.")
            else:
                st.caption(
                    f"No web results passed the RFP filter "
                    f"(checked {res.get('raw_count', 0)} hits). Try different "
                    f"keywords.")
        else:
            if res.get("mode") == "lookup":
                st.caption("🎯 **Title lookup** — searched the exact phrase and "
                           "filtered to primary sources (aggregators hidden).")
            _msg = []
            if res.get("dropped_aggregator"):
                _msg.append(f"{res['dropped_aggregator']} aggregator/blog")
            if res.get("dropped_expired"):
                _msg.append(f"{res['dropped_expired']} expired")
            if res.get("dropped_old"):
                _msg.append(f"{res['dropped_old']} stale")
            if res.get("dropped_notrfp"):
                _msg.append(f"{res['dropped_notrfp']} not a call")
            if res.get("dropped_geo"):
                _msg.append(f"{res['dropped_geo']} out-of-geo")
            if res.get("dropped_scholarship"):
                _msg.append(f"{res['dropped_scholarship']} scholarship")
            if res.get("dropped_offtopic"):
                _msg.append(f"{res['dropped_offtopic']} job/off-topic")
            if res.get("dropped_lang"):
                _msg.append(f"{res['dropped_lang']} non-EN/FR")
            # ── Pagination (15/page, ranked best→worst) ──────────────────
            _all = res["results"]            # already sorted best-match first
            _PER = 15
            _pages = max(1, (len(_all) + _PER - 1) // _PER)
            try:
                _pg = int(st.query_params.get("p", "1"))
            except (TypeError, ValueError):
                _pg = 1
            _pg = max(1, min(_pg, _pages))
            _start = (_pg - 1) * _PER
            _page_results = _all[_start:_start + _PER]
            st.caption(
                f"{len(_all)} of {res['raw_count']} web results matched your "
                "RFP configuration"
                + (f" · dropped {', '.join(_msg)}" if _msg else "")
                + f" · showing {_start + 1}–{_start + len(_page_results)} "
                f"(page {_pg} of {_pages}), best matches first.")

            def _page_nav() -> None:
                """Google-style hyperlinked page numbers (?q=…&p=N)."""
                if _pages <= 1:
                    return
                _qq = _urlparse.quote(q)
                bits: list[str] = []
                if _pg > 1:
                    bits.append(f"<a href='?q={_qq}&p={_pg-1}' "
                                "style='text-decoration:none;'>‹ Prev</a>")
                for n in range(1, _pages + 1):
                    if n == _pg:
                        bits.append(f"<b style='color:#00703C;'>{n}</b>")
                    else:
                        bits.append(f"<a href='?q={_qq}&p={n}' "
                                    "style='text-decoration:none;color:#1e3a8a;'>"
                                    f"{n}</a>")
                if _pg < _pages:
                    bits.append(f"<a href='?q={_qq}&p={_pg+1}' "
                                "style='text-decoration:none;'>Next ›</a>")
                st.markdown(
                    "<div style='margin:0.4rem 0 0.2rem;font-size:0.95rem;"
                    "letter-spacing:0.04em;'>" + "&nbsp;&nbsp;".join(bits)
                    + "</div>", unsafe_allow_html=True)

            # Uniform cards: title clamped to 1 line, summary to exactly 2
            # lines (CSS line-clamp + a hard char cap) so every result is the
            # same height — no long paragraphs.
            st.caption(
                "Rate a result (👍/😐/👎) to feed the learning engine, or "
                "**➕ Track** it into Found Records as a scored candidate for "
                "review.")
            for _ri, wr in enumerate(_page_results):
                _title = html.escape((wr["title"] or "")[:140])
                _dom = html.escape(wr["domain"] or "")
                _snip = (wr["snippet"] or "")
                if len(_snip) > 200:
                    _snip = _snip[:200].rstrip() + "…"
                _snip = html.escape(_snip)
                _href = html.escape(wr["link"] or "", quote=True)
                _dl = wr.get("deadline") or ""
                _pdate = wr.get("page_date") or ""
                if _dl:
                    _lead = (f"<span style='color:#00703C;font-weight:600;'>"
                             f"Deadline {html.escape(_dl)}</span> · ")
                elif _pdate:
                    _lead = (f"<span style='color:#94a3b8;'>Posted "
                             f"{html.escape(_pdate)}</span> · ")
                else:
                    _lead = ""
                # Row method: result text in a wide column, the 3 borderless
                # feedback emojis right after it (superscript-like), and Track in
                # its own aligned column. Center-aligned across the row.
                _row = st.columns([7, 0.4, 0.4, 0.4, 1.1],
                                  vertical_alignment="center")
                _row[0].markdown(
                    "<div style='margin:0.1rem 0 0.2rem;'>"
                    f"<a href='{_href}' target='_blank' rel='noopener' "
                    "style='font-weight:600;color:#1e3a8a;text-decoration:none;"
                    "display:block;white-space:nowrap;overflow:hidden;"
                    f"text-overflow:ellipsis;'>🔗 {_title}</a>"
                    "<div style='color:#475569;font-size:0.83rem;"
                    "line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;"
                    "-webkit-box-orient:vertical;overflow:hidden;"
                    "max-height:2.6em;'>"
                    f"{_lead}<span style='color:#94a3b8;'>{_dom}</span> — "
                    f"{_snip}</div></div>",
                    unsafe_allow_html=True)
                _render_result_actions(wr, key=f"wr_{_pg}_{_ri}", user=user,
                                       cols=_row[1:])
            _page_nav()   # hyperlinked page numbers at the tail (Google-style)

# ── Related searches ────────────────────────────────────────────────────────
# Alternative queries (clickable) to widen discovery. Each links to ?q=… so it
# re-runs the search on this page and stays refresh-stable.
_alts = web_search.suggest_terms(q)
if _alts:
    st.divider()
    st.caption("Related searches")
    _chips = "".join(
        f"<a href='?q={_urlparse.quote(t)}' style='text-decoration:none;"
        "display:inline-block;background:#e6f2eb;color:#00703C;"
        "padding:3px 11px;border-radius:13px;font-size:0.85rem;"
        f"margin:0 0.4rem 0.4rem 0;'>{html.escape(t)}</a>"
        for t in _alts)
    st.markdown(f"<div style='line-height:2.0;'>{_chips}</div>",
                unsafe_allow_html=True)
