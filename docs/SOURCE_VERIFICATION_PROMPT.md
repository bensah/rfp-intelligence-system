# Fresh-session prompt — full source verification (all 75 catalogue sources)

> Paste everything below the line into a new session as the opening prompt.
> It is self-contained: objective, method, tools, rules, known cases, output.

---

## Objective

Independently verify **every source** in the RFPIS scan catalogue (`donor_sources`,
currently ~75 rows). For each one, determine — **by testing, never by assumption** —
whether it is a usable funding-opportunity source and exactly how to crawl it. Produce
a **per-source report document** I can review.

This is the core of the app: the scan is only as good as the health of its sources.
Each website is built differently — **do not apply one logic to all**. Treat each
source as its own investigation.

## Scope & ground rules (read carefully)

1. **Liveness is the FIRST check.** Is the URL alive? (Follow redirects; try a
   browser User-Agent; a plain-GET 403 may still work via Playwright.)
2. **Listings vs. detail.** Is the URL an actual **listings page** (multiple current
   solicitations) or a **single solicitation detail** page with the data we need —
   or neither (just a description/“how to apply” page with no calls)? A page that
   only *describes* procurement/grant-making with no solicitations is **invalid**
   (e.g. UNFPA `/procurement`). Find the real listings URL or deactivate.
3. **“No current calls” is VALID, not an error.** If a *valid* listings page says
   “no open calls / no proposals at the moment,” that is correct behavior — **keep it
   active and keep scanning** for future solicitations. Do **not** deactivate it.
   Example: `https://www.scidev.net/notice-type/grants/`.
4. **Only deactivate** sources that are genuinely dead (404/gone), persistently
   blocked with no workaround, or pure description pages with no machine-readable
   path to solicitations.
5. **Pick the best method by testing it.** Preference order, but verify empirically:
   **API (JSON)** → **RSS/Atom feed** → **JS render (Playwright)** → **page crawl**.
   Some sites need **bespoke handling** (see Known cases): button-click flows, news
   pages, Oracle/SaaS negotiation portals.
6. **Test extraction of the target fields**, don’t just confirm the page loads.
7. **No assumptions. Everything must be verified** end-to-end, source by source.

## Target data (the candidate shape)

The crawler returns dicts with these keys (match them exactly):

```
opportunity_title      # the solicitation title
opportunity_link       # the individual solicitation detail URL (NOT the listing)
funding_agency         # donor name
brief_description       # short summary (optional but wanted)
date_posted            # publication date (optional)
submission_deadline    # closing date (optional but high-value)
_source_origin         # provenance label, set by the handler
```

A source is **healthy** when its crawl yields **distinct individual-solicitation
links** with titles (deadlines when available) — NOT navigation, pagination,
language toggles, homepages, or campaign pages.

> Note: eligibility (`core.auto_scorer.is_eligible`) is a **downstream policy gate**
> (country/theme/deadline/RFP-wording). Do **NOT** use it as the source-health
> metric — it rejects many real calls because donor listing pages don’t expose a
> deadline until deep-read. Health = “extracts real solicitation links.”

## Tools already built (use and extend these)

- `scripts/audit_sources.py` — for each active source: liveness → real crawl (with
  Playwright) → counts candidates, **detail-links**, and eligible. Verdict
  OK / NOISE / EMPTY / DEAD. Run: `python scripts/audit_sources.py` (add `--all`
  for inactive too).
- `scripts/inspect_source.py "<url-substring>" …` — drills into a source: prints
  each extracted candidate (title, link, deadline) + why the gate accepts/rejects.
  Use this to see *what* a source actually extracts.
- `scripts/triage_empty.py "<url-substring>" …` — for an EMPTY/DEAD source, tries
  BOTH the requests path and Playwright, reports HTTP status + counts → tells you if
  it’s a method mismatch, genuinely empty, or blocked.
- `scripts/probe_apis.py` — loads a page in Playwright and captures JSON XHR/fetch
  responses → finds the **underlying data API** to parse instead of the DOM. Edit
  its `TARGETS` dict for the source(s) you’re investigating.

## Codebase internals you need

- **`core/scraper.py`**
  - `scan_source({"name","method","url"})` — entry point. `method` ∈
    `html | html_js | rss | rest_json | manual`. It **intercepts** specific hosts
    and routes them to per-site handlers (see the `scan_source` body):
    grants.gov, EU F&T (`api.tech.ec.europa.eu`), TED, World Bank, UNGM
    (`ungm.org`), UNOPS (`grantplus.unops.org`), CHINNOVA (`grants.chinnova.aau.org`).
  - `_extract_candidates_from_html(name, url, html)` — the generic anchor crawler.
    Filters: `_STRONG_OPP_PATH` (lets opportunity-shaped paths bypass the 25-char
    title floor), `_is_junk_anchor` (drops language toggles, pagination `?paged=`,
    nav verbs, self-links), `_BLOG_URL_RE`, `_SEARCH_PAGE_URL_RE`, `_GRANTY_RE`.
  - **Per-site handler pattern** (copy these): `_scan_chinnova`, `_scan_ungm`,
    `_scan_unops`, `_scan_worldbank_procurement`, `_scan_eu_funding_tenders`. Each
    parses that site’s API/DOM and returns candidate dicts. Add new ones and wire a
    host check into `scan_source`.
  - `expand_listing(url)` — walk a listing/aggregator index → child detail links.
- **`core/deep_read.py`** — Playwright render + enrichment (`render_text`, `enrich`).
  Set `RFPIS_DEEP_READ=1` to enable JS rendering in scripts.
- **`core/source_registry.py`** — the staging/curation registry (host-keyed).
  `push_primaries` upserts confirmed primaries → catalogue; `reconcile_in_catalogue`
  flags registry rows already in the catalogue; `list_rows`, `add_row`.

## Registry ↔ catalogue model (keep clean)

- **`donor_sources`** = the scan catalogue (URL-keyed) — what the scan runs on.
- **`source_registry`** = staging/curation (host-keyed) — single point of entry for
  new sources; confirmed primaries are **pushed** to the catalogue.
- **No duplicates across the two.** Migration **040** adds
  `source_registry.in_catalogue`; run it, then
  `python scripts/reconcile_registry.py` to sync catalogue→registry and set the flag.
  Future pushes must **exclude** `in_catalogue = true` rows.

## Known cases / corrections (verified this session — start from these)

| Source | Finding / required handling |
|---|---|
| **Rockefeller Foundation** | `…/our-grants/` is NOT a listings page. Real listing is the grants **search** URL (`rockefellerfoundation.org/grants/search/?post_type=grant…`). Check for a public API/RSS first; else crawl the search results. |
| **The Global Fund** | `…/iel/upcoming-requests-for-proposals/` is invalid. Real tenders live on an **Oracle SaaS** negotiation portal (`fa-enmo-saasfaprod1.fa.ocs.oraclecloud.com/…/NegotiationAbstracts?…`). If not directly fetchable, use the base page `theglobalfund.org/en/business-opportunities/` and **click the “View Open Tenders” button** (bespoke Playwright flow) then crawl. |
| **The Pandemic Fund** | `…/call-for-proposals` is not a real listings page — calls are published **among news**. Crawl `thepandemicfund.org/news` and **exceptionally allow news links for this source** to find genuine calls. (The `…/news/announcement/…` deep link they publish can 404 — handle gracefully.) |
| **UNFPA** | `unfpa.org/procurement` only describes procurement/supply-chain guides — **no solicitations**. Find the real tender portal (likely **UNGM**) or deactivate. |
| **SciDev.Net** | `scidev.net/notice-type/grants/` is valid; may currently show no grants — that’s fine, **keep active**. |
| **RVO (Netherlands)** | Real JSON API: `english.rvo.nl/api/rvo/v1/search-subsidies` (`searchResults/facets/pager`). Build a `rest_json` handler. |
| **Chan Zuckerberg** | WordPress REST API: `chanzuckerberg.com/wp-json/czi/v1/grants/` (`{grants}`). Build a `rest_json` handler. |
| **CEPI, AfDB, GCGH, Gates/Grand Challenges, GIZ, OSF, MIT Solve, Packard, Google.org** | No JSON API captured — data is SSR HTML or needs interaction. Investigate each: read page source for embedded JSON / GraphQL, or write a DOM/per-site parser. |
| **Already fixed/deactivated this session** | EC → EU F&T API (`rest_json`, working). Deactivated: `frld.org` (403 all paths), US Embassy Cameroon (feed 404), Zayed (500 + application page), NIH RSS (403 + US-domestic, covered by grants.gov). DevelopmentAid → `html_js` (40). Research.Swiss → `html` (40). |

## Generic crawler improvement already landed

`_is_junk_anchor` now drops language toggles (“English”), pagination (`2/3/4`,
`?paged=`), nav verbs, and self-links — this cut false “noise” from 27 → 1 in the
audit. Build per-site handlers for anything the generic crawler still can’t read.

## Deliverable — the report document

Create **`docs/SOURCE_AUDIT_REPORT.md`** with:

1. A **summary table**: every source → verdict (KEEP / FIX-METHOD / NEEDS-PARSER /
   DEACTIVATE), current method, recommended method, # real calls extracted in test.
2. A **per-source section**: URL, alive?, listings-vs-detail-vs-description, the
   method(s) tested and results, a few **sample extracted candidates** (title +
   link + deadline) as proof, the recommended action, and notes.
3. A **changes-applied** appendix (DB updates, new handlers, deactivations) — but
   **do not commit or push** unless I explicitly ask.

Work through all ~75 sources methodically. Verify everything. No assumptions. When a
source needs a per-site parser, build it (pattern above), wire it into `scan_source`,
and re-test until it extracts real solicitations — then record the proof in the report.
