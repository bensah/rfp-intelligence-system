"""One opportunity, in full — /opportunity?uid=<uid>.

Two parts, in this order, because that is the order the decision gets made in:

  1. THE OPPORTUNITY — the raw extraction (regex + LLM + deep read, schema
     docs/DATA_SCHEMA_ETL.md §4) restated in the RFPIS standard format. Primary sources all
     publish the same facts differently; one structure is what lets a reviewer read two
     calls the same way. A screened row is joined back to its extraction by call URL, so it
     shows the full call and not only the handful of fields matching kept.
  2. SCORING ANALYSIS — our eligibility criteria and fit strength against this entity, so
     the reviewer can decide whether to put it in their pipeline or drop it.

The live opportunity rail sits alongside, as on Pipelines, so moving between calls doesn't
mean going back first.
"""
from __future__ import annotations

import html as _html
from urllib.parse import quote as _quote

import streamlit as st

from core import opportunity_detail as _od
from core import opportunity_scoring as _osc
# NOT `_links`: this module already uses that name for the external-link list a
# few lines further down, and shadowing it would break every internal link
# rendered after that assignment — the same fault as #189.
from core import ui_links as _uilinks
from core import settings as _settings_mod
from db.supabase_client import get_client

user = st.session_state.get("app_user") or {}
_uid = (st.query_params.get("uid") or "").strip()

# ── page styling: cards, chips, criterion rows ──────────────────────────────
st.markdown("""
<style>
  .opp-chips { display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 2px; }
  .opp-chip { font-size:0.76rem; padding:2px 10px; border-radius:12px;
              background:#eef2f0; color:#3b4a43; white-space:nowrap; }
  .opp-chip.closed { background:#fde2e2; color:#b3261e; }
  .opp-chip.urgent { background:#fde2e2; color:#b3261e; font-weight:600; }
  .opp-chip.soon   { background:#fff4cc; color:#8a6d00; font-weight:600; }
  .opp-chip.open   { background:#dcf5e3; color:#00703C; }
  .opp-card { background:#fff; border:1px solid #e6e6e6; border-radius:10px;
              padding:16px 18px; height:100%; }
  .opp-card h4 { font-weight:700; color:#16734a; margin:0 0 10px;
                 font-size:1.02rem; letter-spacing:.01em; }
  /* TYPE SCALE. The values were 0.83/0.87rem inside a 14px-padded card, so the content
     sat small in a lot of white space and the card outweighed what it held. The card is
     furniture; the extracted value is the point of the page, so the value now leads at
     1rem and the label sits a step below it. */
  .opp-kv { display:flex; justify-content:space-between; gap:16px; padding:7px 0;
            border-bottom:1px dashed #f0f0f0; }
  .opp-kv:last-child { border-bottom:none; }
  .opp-k { color:#5d6b63; font-size:0.94rem; flex:0 0 42%; line-height:1.45; }
  .opp-v { color:#1f2a24; font-size:1rem; text-align:right; font-weight:600;
           line-height:1.45; }
  /* HIERARCHY. These were 0.72rem in mid-grey while the h5 subsections beneath them were
     larger and near-black, so the page read as though the subsections outranked the sections
     they belong to. The section heading is now the largest, darkest thing in the body, and
     the subsections sit a clear step below it in both size and weight. */
  .opp-sec { color:#0f3d2a; font-size:1.35rem; letter-spacing:.005em; margin:34px 0 12px;
             font-weight:800; border-bottom:2px solid #d8e6de; padding-bottom:6px; }
  .opp-open { background:transparent; border:none; padding:0; }
  /* A CARDLESS section is not a card, so its heading should not wear the card's green: it
     sits in the flow of the page exactly like "What is funded", and must match it. */
  .opp-open h4 { font-weight:700; color:#24352c; margin:0 0 8px; font-size:1.06rem; }
  /* Subsection headings (st.markdown "#####") — a step below .opp-sec, not above it. */
  h5 { color:#24352c !important; font-size:1.06rem !important; font-weight:700 !important;
       margin-top:14px !important; }
  /* The summary is PROSE, not a data card — a border round it made a paragraph look like
     one more table. */
  .opp-line { display:flex; gap:18px; padding:5px 0; align-items:baseline;
              border-bottom:1px dashed #eef1ef; max-width:62em; }
  .opp-line:last-child { border-bottom:none; }
  .opp-v2 { color:#1f2a24; font-size:1rem; font-weight:600; line-height:1.5; }
  .opp-lede { color:#2b332e; line-height:1.7; font-size:1.05rem; margin:2px 0 4px;
              max-width:62em; }
  .opp-prose { background:#fff; border:1px solid #e6e6e6; border-radius:10px;
               padding:16px 20px; color:#2b332e; line-height:1.65; font-size:1rem; }
  .opp-crit { display:flex; align-items:center; gap:10px; padding:7px 12px;
              border:1px solid #ececec; border-left-width:4px; border-radius:8px;
              margin-bottom:6px; background:#fff; }
  .opp-crit .nm { flex:1; font-size:0.87rem; color:#2b332e; }
  .opp-crit .lb { font-size:0.85rem; font-weight:700; }
  .opp-crit .ct { font-size:0.78rem; color:#8a8f8b; min-width:96px; text-align:right; }
  .opp-crit .pt { font-size:0.8rem; color:#5d6b63; min-width:62px; text-align:right; }
</style>
""", unsafe_allow_html=True)


def _esc(v) -> str:
    # display_value untangles the jsonb columns (real list / JSON string / double-encoded)
    # so a Python repr never reaches the page. "$" is neutralised so Streamlit's markdown
    # doesn't render a money value as a LaTeX block.
    return _html.escape(_od.display_value(v)).replace("$", "&#36;")


def _txt(s) -> str:
    return _html.escape("" if s is None else str(s)).replace("$", "&#36;")


# ── resolve ──────────────────────────────────────────────────────────────────
def _pipeline_reader(uid: str):
    # Tenant-scoped: a reviewer must only ever see their own entity's screened rows.
    rows = (get_client().table("rfp_submissions").select(_od.PIPELINE_FIELDS)
            .eq("uid", uid).limit(1).execute().data or [])
    return rows[0] if rows else None


def _catalog_reader(uid: str):
    # NOT tenant-scoped, by design: the catalogue is the shared pool the Featured card
    # ranks, which is what makes a screening miss recoverable. It holds no tenant data.
    from db.supabase_client import service_client
    rows = (service_client().table("extracted_solicitations").select(_od.CATALOG_FIELDS)
            .eq("uid", uid).limit(1).execute().data or [])
    return rows[0] if rows else None


@st.cache_data(ttl=120, show_spinner=False)
def _catalog_by_link(link: str):
    """The raw extraction behind a screened row, matched on call URL — so a pipeline
    opportunity shows the whole call, not just the fields matching kept.

    CASE-INSENSITIVE on purpose: `link` arrives lowercased by `normalise_link` while the
    column stores the URL as published, so an `=` comparison could never match the 344 rows
    whose URL contains uppercase. See `opportunity_detail.link_query_patterns`. The returned
    row is still verified against the normalised link, because LIKE can over-match.
    """
    from db.supabase_client import service_client
    sb = service_client()
    for pattern in _od.link_query_patterns(link):
        rows = (sb.table("extracted_solicitations").select(_od.CATALOG_FIELDS)
                .ilike("opportunity_url", pattern).limit(5).execute().data or [])
        for r in rows:
            if _od.normalise_link(r.get("opportunity_url")) == link:
                return r
    return None


if not _uid:
    st.title("Opportunity")
    st.info("No opportunity selected. Open one from the **Live Opportunity Feed** on "
            "Home or Pipelines.")
    st.markdown(_uilinks.internal_link("📚 Go to Pipelines", "pipelines"),
                unsafe_allow_html=True)
    st.stop()

try:
    _res = _od.load(_uid, pipeline_reader=_pipeline_reader,
                    catalog_reader=_catalog_reader,
                    catalog_by_link_reader=_catalog_by_link)
except Exception as exc:
    st.error(f"Couldn't load `{_uid}` right now: {exc}")
    st.stop()

_kind, _row, _ext = _res["kind"], _res["row"], _res["extraction"]
if not _kind:
    st.title("Opportunity")
    st.warning(f"Couldn't find an opportunity with uid `{_uid}`. It may have been "
               "deleted, or it belongs to another entity.")
    st.markdown(_uilinks.internal_link("📚 Back to Pipelines", "pipelines"),
                unsafe_allow_html=True)
    st.stop()

_view = _od.standard_view(_kind, _row, _ext)

_main, _rail = st.columns([3.4, 1], gap="medium")

with _rail:
    try:
        from views.opportunity_rail import render_opportunity_rail
        render_opportunity_rail()
    except Exception as _rexc:                     # never let the rail take the page down
        st.caption(f"_Opportunity feed unavailable: {_rexc}_")

try:
    from core import permissions as _perm
    _is_super = _perm.is_super_user(user)
except Exception:
    _is_super = False

with _main:
    # ── header ──────────────────────────────────────────────────────────────
    # The KIND of solicitation is settled before any of the detail: a reviewer reads a
    # tender differently from a concept-note round, and the column stores only the trade
    # abbreviation (and is blank on a third of rows), so it is spelled out here.
    # The kind of solicitation is a CHIP, not a title suffix: appending it produced
    # "DIV Fund – Request for Proposals: Request for Proposals" whenever the funder had
    # already named the kind, which they usually have. title_line returns "" in that case.
    _title, _sol = _od.title_line(_view)
    st.title(_title)
    _dl_txt, _dl_tone = _od.deadline_status(_view.get("deadline"),
                                           _view.get("funding_status"))
    _chips = [(_dl_txt, _dl_tone)]
    if _sol:
        _chips.append((_sol, ""))          # ahead of the instrument, per the owner
    for _f in ("instrument_type", "funding_window"):
        if _view.get(_f):
            _chips.append((_od.display_value(_view[_f]), ""))
    # A row lands in rfp_submissions the moment the scan touches it, so "in your pipeline"
    # was true of everything the scan had ever seen — including the 160 rows marked not
    # eligible. Only a recorded disposition means it is actually in a pipeline.
    _decision = _od.pipeline_decision(_kind, _row)
    if _decision:
        _chips.append((f"In your pipeline · {_decision}",
                       {"Proceed": "open", "Park": "soon", "Decline": "closed"}[_decision]))
    elif _kind == _od.KIND_PIPELINE:
        _chips.append(("Screened — not in a pipeline yet", ""))
    else:
        _chips.append(("Shared catalogue — not screened for you", ""))
    # The reference beside the funder is the FUNDER'S id for the call, which is what gets
    # quoted in an enquiry or searched on their portal. The RFPIS uid is internal and shows
    # under Identity; it used to sit here, where it looked like the call's own number.
    _ref = _od.header_reference(_view)
    st.markdown(
        f"<div style='color:#5d6b63;font-size:0.97rem;margin:-6px 0 2px'>"
        f"{_txt(_view.get('funder_name') or '—')}"
        + (f" <span style='color:#9aa39d'>· administered by "
           f"{_txt(_view.get('grantmaking_entity'))}</span>"
           if _view.get("grantmaking_entity") else "")
        + (f" <span style='color:#b9c0bb'>· {_txt(_ref)}</span>" if _ref else "")
        + "</div>"
        + "<div class='opp-chips'>"
        + "".join(f"<span class='opp-chip {t}'>{_txt(c)}</span>" for c, t in _chips)
        + "</div>", unsafe_allow_html=True)

    # apply_url falls back to the call page (it is extracted on no row — see
    # opportunity_detail.apply_url), so a separate "Apply" link only appears when the
    # extraction genuinely found a different target.
    _call, _apply = _od.call_url(_view), _od.apply_url(_view)
    _links = []
    if _call:
        _links.append(f"[Open the call ↗]({_call})")
    if _apply and _apply != _call:
        _links.append(f"[Apply ↗]({_apply})")
    if _view.get("aggregator_url"):
        _links.append(f"[Where we found it ↗]({_view['aggregator_url']})")
    if _links:
        st.markdown(" &nbsp;·&nbsp; ".join(_links))

    # ── PART 1 — the opportunity, in our standard format ────────────────────
    st.markdown("<div class='opp-sec'>1 · The opportunity</div>",
                unsafe_allow_html=True)
    _brief = _od.summary_of(_view)
    if _brief:
        st.markdown(f"<div class='opp-lede'>{_txt(_brief)}</div>", unsafe_allow_html=True)

    # Headline money + deadline, the two facts a reviewer looks for first.
    _money = _od.format_money(_view.get("grant_amount"), _view.get("currency"))
    _usd = _od.usd_equivalent(_view.get("grant_amount"), _view.get("currency"))
    _range = _od.format_money_range(_view.get("call_award_floor"),
                                    _view.get("call_award_ceiling"),
                                    _view.get("currency"))
    # Breathing room: the summary card sat flush against the metrics, so the three read as
    # part of the same block instead of as the headline facts drawn out of it.
    st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
    _h1, _h2, _h3 = st.columns(3)
    # ALWAYS a second line under the award value, including when the call is already in USD.
    # Without it the first card was a line shorter than its neighbours and the row of three
    # sat unevenly; and a reader comparing calls wants the USD figure in the same place every
    # time rather than only on the foreign-currency ones.
    _h1.metric("Award value", _money or "—",
               _od.usd_reference(_view.get("grant_amount"), _view.get("currency"))
               or (_range if _range and _range != _money else None))
    _h2.metric("Submission deadline", str(_view.get("deadline") or "—")[:10], _dl_txt)
    _h3.metric("Project duration",
               _od.format_duration(_view.get("project_duration")) or "—",
               _od.display_value(_view.get("funding_window")) or "—")

    # THE PUBLISHER'S OWN SUMMARY, below the three numbers a reviewer reads first. Borderless:
    # a box round a paragraph made prose look like one more data card.
    st.markdown("##### Project overview")
    _ov = _od.overview_text(_view)
    if _ov:
        st.markdown(f"<div class='opp-lede'>{_txt(_ov)}</div>", unsafe_allow_html=True)
        if _od.overview_is_truncated(_view) and _call:
            st.markdown(f"[Learn more ↗]({_call})")
    else:
        # NOT a fallback to brief_description: that already leads the section a few lines
        # above, and printing it twice under two different headings is exactly the
        # in-card/out-of-card repetition being removed.
        st.markdown(f"<div class='opp-lede'>{_od.MISSING}</div>", unsafe_allow_html=True)

    # THE BODY as explicit ROWS — see opportunity_detail.page_rows. Streaming sections into
    # a two-column run and resetting it whenever prose appeared left holes all down the page:
    # a card on the left with nothing beside it, then a heading, then another lone card.
    _rows = _od.page_rows(_view)
    _secs = [b for r in _rows for c in r for b in c if b["kind"] != "prose"]

    def _fact_html(_b):
        _cls = "opp-card" if _b["kind"] == "cards" else "opp-open"
        return (f"<div class='{_cls}'><h4>{_txt(_b['title'])}</h4>"
                + "".join(f"<div class='opp-kv'><span class='opp-k'>{_txt(lb)}</span>"
                          f"<span class='opp-v'>{_txt(v)}</span></div>"
                          for lb, v in _b["rows"]) + "</div>")

    def _render(_b):
        if _b["kind"] == "prose":
            st.markdown(f"##### {_txt(_b['title'])}")
            if _b["missing"] or len(_b["lines"]) == 1:
                st.markdown(f"<div class='opp-lede'>{_txt(_b['lines'][0])}</div>",
                            unsafe_allow_html=True)
            else:
                st.markdown(chr(10).join(f"- {l}" for l in _b["lines"]))
            return
        st.markdown(_fact_html(_b), unsafe_allow_html=True)
        # The subsection's own detail, under its rows and with NO second heading.
        if _b["prose"]:
            st.markdown(
                f"<div class='opp-lede'>{_txt(' '.join(_b['prose']))}</div>"
                if len(_b["prose"]) == 1
                else chr(10).join(f"- {l}" for l in _b["prose"]),
                unsafe_allow_html=True)

    # Each row is N side-by-side COLUMNS, and each column STACKS its blocks — so a short card
    # is followed straight away by the next one in the same column instead of leaving dead
    # space until the tallest block in the row ends.
    # NOT `_row`: that name holds the OPPORTUNITY row for the whole page, and shadowing it
    # here left section 2 scoring a list instead of the record — which raised, and a raised
    # exception with error details suppressed renders as BLANK SPACE under the heading. That
    # is the empty "Decision aid" section, and it broke both the catalogue and pipeline paths.
    for _layout_row in _rows:
        _cols = (st.columns(len(_layout_row), gap="medium")
                 if len(_layout_row) > 1 else [st.container()])
        for _stack, _c in zip(_layout_row, _cols):
            with _c:
                for _b in _stack:
                    _render(_b)
                    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

    # Kept as a section rather than appearing only when it has content, for the same reason
    # as the rest of the skeleton.
    st.markdown("##### Documents & links")
    _docs = _od.documents(_view)
    if _docs:
        for _label, _url, _dkind in _docs:
            st.markdown(f"- [{_label}]({_url}) &nbsp;<span style='color:#9aa39d;"
                        f"font-size:0.78rem'>{_txt(_dkind)}</span>",
                        unsafe_allow_html=True)
    else:
        st.caption("_No documents or templates were found on the call page. "
                   "**Open the call ↗** for anything published behind a login._")

    # The two award axes are shown as one reconciled line, so the only thing left to say is
    # when the combination genuinely does not add up. That is 7 of 686 live rows — warning on
    # the 30 legitimate grant-awarded-as-a-contract rows would have taught a reviewer to
    # ignore the warning that matters here.
    _pair = _od.award_pairing(_view)
    if _pair.get("note"):
        st.caption(f":orange[⚠ {_pair['note']}]")

    # There is no "thin page" caption any more: every section now renders whether or not this
    # call filled it, so the dashes say what the caption used to.
    #
    # And the primary source's own raw text is NOT shown here — see
    # opportunity_detail.as_published. Putting another publisher's structure on screen beside
    # ours undid the one thing this page is for, which is that every call reads the same way.
    # It moves to the super_user block below, where it belongs as audit material.

    # ── internal bookkeeping: super_user only ───────────────────────────────
    # A reviewer does not act on a crawl timestamp, a content hash or an extraction
    # confidence band, and the completeness line is a statement about OUR pipeline, not
    # about the call. All of it competed for attention with the funder's actual terms.
    if _is_super:
        _tech = _od.technical_sections(_view)
        _filled, _total, _missing = _od.coverage(_view)
        with st.expander(f"🔧 Record, provenance & extraction coverage "
                         f"({_filled}/{_total} schema fields) — super_user"):
            for _title, _fields in _tech:
                st.markdown(
                    f"<div class='opp-card'><h4>{_txt(_title)}</h4>"
                    + "".join(f"<div class='opp-kv'><span class='opp-k'>{_txt(lb)}</span>"
                              f"<span class='opp-v'>{_txt(v)}</span></div>"
                              for lb, v in _fields)
                    + "</div>", unsafe_allow_html=True)
            _raw = _od.as_published(_view)
            if _raw:
                st.markdown("**As published by the primary source** — what the extraction "
                            "had to work with. Audit only; a reviewer reads our schema.")
                st.text(_raw)
            if _missing:
                st.caption("Not extracted: " + ", ".join(_missing))
                st.caption("Most of these have no writer at all — the LLM-synthesis stage "
                           "of the schema was specified but never built. `full_description` "
                           "is named in exactly two places: the column allow-list and the "
                           "read on this page.")

    # ── PART 2 — scoring analysis ───────────────────────────────────────────
    st.markdown("<div class='opp-sec'>2 · Decision aid — is this worth bidding?"
                "</div>", unsafe_allow_html=True)
    # "Scoring analysis" and "This entity against this opportunity" were two headings saying
    # the same thing, and "entity" is our internal word for a tenant — a reader who has not
    # met it does not know whether it means them, the funder, or something else.
    # The tenant's own name, so the heading names WHO is being assessed. "your organisation"
    # is correct but generic, and this page is read beside others.
    try:
        _entity = str((_settings_mod.get_org() or {}).get("org_name") or "").strip()
    except Exception:
        _entity = ""
    _who = _entity or "your organisation"
    st.markdown(f"##### How this opportunity fares against {_txt(_who)}")

    from core import criteria_derive as _cd
    from core import org_profile as _orgp
    from core import settings as _settings

    @st.cache_data(ttl=120, show_spinner=False)
    def _context():
        return _orgp.get_profile(), _settings.get_org()

    try:
        _org_prof, _org_set = _context()
    except Exception:
        _org_prof, _org_set = {}, {}
    # A catalogue call has no screened row, so score the CANDIDATE built from its
    # extraction — same derivation, same criteria, so the two paths can't disagree.
    _scored_row = _row if _kind == _od.KIND_PIPELINE else _od.to_candidate(_row)
    _donor = None
    try:
        from core.donor_intel import match_donor
        _fa = str(_view.get("funder_name") or "").strip()
        if _fa:
            _donor = match_donor(_fa, fuzzy=False)
    except Exception:
        _donor = None
    try:
        import json as _json
        _flags = _json.loads(_scored_row.get("call_compliance_flags") or "{}")
        _flags = _flags if isinstance(_flags, dict) else {}
    except Exception:
        _flags = {}
    _ov = _scored_row.get("criteria_component_overrides")
    if isinstance(_ov, str):
        try:
            import json as _json2
            _ov = _json2.loads(_ov or "{}")
        except Exception:
            _ov = {}
    try:
        _an = _osc.analyse(_scored_row, _org_prof, _donor, _org_set,
                           rfp_compliance=_flags,
                           overrides=_ov if isinstance(_ov, dict) else {})
    except Exception as _sexc:
        _an = None
        st.warning(f"Couldn't score this opportunity right now: {_sexc}")
    if not _an:
        # Never leave the section as a bare heading over white space: if scoring produced
        # nothing, say so, because a blank section reads as a broken page.
        st.info("No scoring is available for this opportunity yet. It needs an entity "
                "profile and a screened row before the criteria can be evaluated.")

    if _an:
        _tone = {"Proceed": ("#dcf5e3", "#00703C"), "Park": ("#fff4cc", "#8a6d00"),
                 "Decline": ("#fde2e2", "#b3261e")}.get(
                     _an["suggested_decision"], ("#eee", "#333"))
        _conf = _an["confidence"]
        _dtxt = (f"{_conf['donor_pct']}%" if _conf["donor_matched"]
                 else "no funder profile")
        st.markdown(
            f"<div style='background:{_tone[0]};border-radius:10px;padding:12px 16px;"
            f"display:flex;gap:28px;align-items:center;flex-wrap:wrap'>"
            f"<div><span style='color:{_tone[1]};font-size:1.05rem;font-weight:400'>"
            f"Bid Strength: </span>"
            f"<span style='color:{_tone[1]};font-weight:700;font-size:1.25rem'>"
            f"{_an['bid_strength']}/100 — {_txt(_an['fit'])}</span></div>"
            f"<div style='color:#31403a;font-size:1.05rem;font-weight:400'>Suggestion: "
            f"<span style='font-size:1.25rem;font-weight:700'>"
            f"{_txt(_an['suggested_decision'])}</span>"
            + (f" <span style='color:#8a6d00'>(was {_txt(_an['system_decision'])})</span>"
               if _an["suggested_decision"] != _an["system_decision"] else "")
            + "</div>"
            f"<div style='color:#31403a;font-size:1.05rem;font-weight:400'>Confidence: "
            f"<span style='font-size:1.25rem;font-weight:700'>{_txt(_conf['band'])}</span>"
            # the data share carries the same weight as the band: it is what says whether the
            # band can be trusted, and at 0.86rem it read as a footnote to its own headline
            f"<span style='font-size:1.05rem'> · data {_conf['pct']}% "
            f"(donor {_txt(_dtxt)} · call {_conf['call_pct']}%)</span></div>"
            "</div>", unsafe_allow_html=True)
        if _an["fatal"]:
            st.error(f"🔒 **Fatal gate — {_od.display_value(_an['fatal_trigger'])}.** "
                     "This is a structural ineligibility we cannot fix before the "
                     "deadline, so the system declines it.")
        if _an["confidence_note"]:
            st.warning(f"⚠ {_an['confidence_note']}")
        if _an["below_award_floor"]:
            st.info("This award is below your minimum funding target, which caps a "
                    "would-be Proceed at Park.")

        _COL = {2: "#1a7f37", 1: "#b8860b", 0: "#c0392b"}
        st.markdown("##### Eligibility screening criteria")

    if _an:
        for _c in _an["criteria"]:
            _col = _COL.get(_c["band"], "#9aa39d")
            st.markdown(
                f"<div class='opp-crit' style='border-left-color:{_col}'>"
                f"<span class='nm'>{_txt(_c['title'])}</span>"
                f"<span class='lb' style='color:{_col}'>{_txt(_c['label'])}</span>"
                f"<span class='ct'>{_txt(_c['count_text'])}</span>"
                f"<span class='pt'>{_c['points']:.1f} / "
                f"{_c['weight'] * 100:.0f}</span></div>", unsafe_allow_html=True)
            # The per-row note explaining WHY a label can differ from its component ratio
            # (PREFER-6/8 are weighted models, not means) is gone from here. It is true and it
            # matters — to somebody editing components in Update Decision, where it still
            # shows. On this page it put a paragraph of scoring internals between one
            # criterion and the next for a reader who only wants the verdict.
        # One line and a pointer, rather than the rulebook. How the nine criteria roll up,
        # what the bands mean and why a label can disagree with its ratio all live on Help
        # now — a reader who wants them will go and read them once, not on every call.
        st.caption("Each row: the criterion, its verdict, the components behind it, and the "
                   "points it contributes out of its weight. "
                   + _uilinks.internal_link("How this is scored", "help")
                   + " explains the bands and the roll-up.",
                   unsafe_allow_html=True)


    # ── decision aid: the facts about US, not about the call ────────────────
    # Both of these used to sit in Part 1 — "Our role" inside the "Who can apply" card,
    # where it read as an eligibility rule the funder had published, and "Key risks" as a
    # call narrative even though it describes this entity's exposure. Part 1 is the call;
    # this is us against it.
    _aid_rows, _aid_prose = _od.decision_aid(_view, _who)
    if _aid_rows or _aid_prose or _an:
        if _aid_rows:
            st.markdown(
                "<div class='opp-card'>"
                + "".join(f"<div class='opp-kv'><span class='opp-k'>{_txt(lb)}</span>"
                          f"<span class='opp-v'>{_txt(v)}</span></div>"
                          for lb, v in _aid_rows)
                + "</div>", unsafe_allow_html=True)
        for _heading, _lines in _aid_prose:
            st.markdown(f"##### {_heading}")
            if len(_lines) == 1:
                st.markdown(f"<div class='opp-prose'>{_txt(_lines[0])}</div>",
                            unsafe_allow_html=True)
            else:
                st.markdown("\n".join(f"- {l}" for l in _lines))
        if _an and _an.get("blockers"):
            # NAME THE COMPONENTS, not just the criteria. "Compliance requirements, Donor
            # relationship" told a reviewer which criteria failed but not what failed inside
            # them; "Authorized signatory (this donor)" is the thing you can go and get.
            # NOT `_lines`: that name belongs to the narrative loop just above. Reusing a
            # name already live in this scope is what produced the blank section 2 (#189).
            _blk = []
            for _b in _an["blockers"]:
                _crit = _b["title"].split(" · ", 1)[-1]
                _parts = _osc.failing_components(_b)
                _blk.append(f"- **{_txt(_crit)}** — "
                            + (", ".join(_txt(p) for p in _parts) if _parts
                               else "no component met"))
            st.markdown("**What's against it**")
            st.markdown(chr(10).join(_blk))
        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    # ── decision ────────────────────────────────────────────────────────────
    st.divider()
    if _kind == _od.KIND_PIPELINE:
        st.markdown(_uilinks.internal_link("✏️ Open in Review", "pipelines", uid=_uid,
                                         style="font-size:1.15rem;font-weight:700"),
                    unsafe_allow_html=True)
        st.caption("Already in your pipeline — score the criteria and record the team "
                   "decision there.")
    else:
        _tracked = st.session_state.get(f"_opp_tracked_{_uid}")
        if not _tracked:
            try:
                _mine = (get_client().table("rfp_submissions")
                         .select("uid,opportunity_link").limit(2000).execute().data or [])
                _tracked = _od.tracked_uid(_row, _mine)
            except Exception:
                _tracked = None
        _rejected = st.session_state.get(f"_opp_rejected_{_uid}")
        if _tracked:
            st.success(f"✓ In your pipeline as `{_tracked}`.")
            st.markdown(_uilinks.internal_link("✏️ Open in Review", "pipelines",
                                             uid=_tracked,
                                             style="font-size:1.15rem;font-weight:700"),
                        unsafe_allow_html=True)
        elif _rejected:
            st.info("Marked not relevant — noted for the learning engine.")
            st.markdown(_uilinks.internal_link("📚 Back to Pipelines", "pipelines"),
                        unsafe_allow_html=True)
        else:
            st.markdown("**Add this to your pipeline?** Adding scores it against your "
                        "eligibility criteria and queues it for review — nothing is "
                        "decided for you.")
            _d1, _d2, _d3 = st.columns([1.5, 1.3, 3])
            if _d1.button("➕ Add to my pipeline", type="primary", width='stretch'):
                from core import found_loader
                # provenance is NOT "search": that path re-runs the eligibility gate, and
                # this call already came from the crawl + extraction. A Featured call may
                # have MISSED this tenant's soft gate — the whole reason it is offered
                # here — so re-gating it would refuse exactly the recovery intended.
                _r2 = found_loader.load_candidate(_od.to_candidate(_row), user,
                                                  provenance="opportunity-page")
                if _r2.get("ok"):
                    st.session_state[f"_opp_tracked_{_uid}"] = _r2["uid"]
                    st.cache_data.clear()      # the rail + pipeline lists are cached
                    st.rerun()
                elif _r2.get("skipped"):
                    st.info("Already in your pipeline — not added twice.")
                else:
                    st.warning(f"Couldn't add it: {_r2.get('reason') or 'unknown error'}")
            if _d2.button("✕ Not relevant", width='stretch',
                          help="Records a negative signal for the learning engine."):
                try:
                    from core import decision_log
                    decision_log.log_feedback(_od.to_candidate(_row), "bad",
                                              by=user.get("email"),
                                              reason="opportunity-page")
                    st.session_state[f"_opp_rejected_{_uid}"] = True
                    st.rerun()
                except Exception as _fexc:
                    st.warning(f"Couldn't record that: {_fexc}")
            _d3.caption("")
