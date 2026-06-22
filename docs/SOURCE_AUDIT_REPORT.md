# RFPIS Source Verification Report

**Date:** 2026-06-21
**Scope:** All 75 rows in `donor_sources` (55 active, 20 inactive).
**Method:** Per-source liveness → real crawl (`scripts/audit_sources.py`, Playwright)
→ targeted investigation (`probe_apis.py` XHR capture, `inspect_source.py`,
direct `requests`/REST probes) → per-site handlers built + re-tested where an API
or embedded-data path existed.

> **Health definition used:** a source is *healthy* when its crawl yields distinct
> **individual-solicitation detail links** with titles (deadlines when available).
> Eligibility (`auto_scorer.is_eligible`) is a **downstream policy gate**, reported
> but never used as the health verdict — most donor listing pages don't expose a
> deadline until deep-read, so 0-eligible ≠ broken.
>
> **Important nuance discovered this audit:** the "real detail links" metric is
> *necessary but not sufficient*. Several proactive funders publish an **awarded-grants
> / grantee database** (recipient names as "titles") that passes the link metric while
> containing **zero open solicitations**. These are flagged FALSE-OK below.

---

## 1. Summary table

Legend — Action: **KEEP** (healthy, no change) · **KEEP-EMPTY** (valid page, no current
calls — keep scanning) · **FIXED** (handler built + tested this session) ·
**NEEDS-PARSER** (real content, no API, generic crawler can't read — bespoke parser
required) · **DEACTIVATE** (dead, blocked, or no machine-readable solicitations) ·
**MANUAL-OK** (correctly inactive/manual — application or description page).

### Active sources (55)

| Source | Cur. method | Verdict | Recommended | Real calls (test) | Action |
|---|---|---|---|---|---|
| Agence Française de Développement | html_js | OK | html_js | 3 | KEEP |
| Alliance for Health Policy (WHO) | html | OK | html | 1 | KEEP |
| Biswas Family Foundation | html | OK | html | 1 | KEEP |
| CHINNOVA / Assoc. of African Universities | html→`_scan_chinnova` | OK | (as-is) | 2 | KEEP |
| DevelopmentAid Aggregator | html_js | OK | html_js | 40 | KEEP |
| EU Funding & Tenders Portal | rest_json | OK | rest_json | 100 | KEEP |
| Fondation Pierre Fabre | html | OK | html | 11 | KEEP |
| FundsForNGOs | rss | OK | rss | 1 | KEEP |
| Gavi, the Vaccine Alliance | html | OK | html | 5 | KEEP |
| Global Affairs Canada | html | OK | html | 31 | KEEP |
| Global Alliance for Chronic Diseases | html | OK | html | 8 | KEEP |
| Global Health EDCTP3 | rss | OK | rss | 30 | KEEP |
| Government of the United Kingdom | rss (atom) | OK | rss | 50 | KEEP |
| Grand Challenges Canada | html | OK | html | 2 | KEEP |
| Health Research, Inc. | rss | OK | rss | 10 | KEEP |
| International Cancer Foundation | html | OK | html | 2 | KEEP |
| International Development Research Centre | html | OK | html | 21 | KEEP |
| JICA Africa Hiroba | html_js | OK | html_js | 2 | KEEP |
| Medicines for Malaria Venture | html | OK | html | 24 | KEEP |
| Norwegian Agency for Dev. Coop. (Norad) | html_js | OK | html_js | 2 | KEEP |
| Novo Nordisk Foundation | rss | OK | rss | 10 | KEEP |
| Pfizer | html | OK | html | 1 | KEEP |
| ReliefWeb | rss | OK | rss | 20 | KEEP |
| Research.Swiss | html | OK | html | 40 | KEEP |
| Robert Wood Johnson Foundation | html | OK | html | 2 | KEEP |
| SciDevNet | html | OK | html | 4 | KEEP |
| Stanford Center for Innovation in GH | html | OK | html | 9 | KEEP |
| Swedish Int'l Dev. Coop. (sida.se main) | html | OK | html | 1 | KEEP |
| The Pandemic Fund | html_js | OK | html_js | 3 | KEEP (prompt note outdated) |
| Unitaid | html | OK | html | 1 | KEEP |
| United Nations Global Marketplace (UNGM) | rest_json→`_scan_ungm` | OK | (as-is) | 30 | KEEP |
| United States Federal Gov (grants.gov) | rest_json | OK | rest_json | 71 | KEEP |
| UNOPS (GrantPlus) | rest_json→`_scan_unops` | OK | (as-is) | 5 | KEEP |
| Wellcome | html_js | OK | html_js | 16 | KEEP |
| worldbank.org | rest_json | OK | rest_json | 24 | KEEP |
| **Gates Foundation** (grandchallenges.org) | html | was NOISE | host→`_scan_grandchallenges` | **2 (w/ deadlines)** | **FIXED** |
| **Grand Challenges Global Health** | html | was EMPTY | host→`_scan_grandchallenges` | **2 (w/ deadlines)** | **FIXED** |
| **Netherlands MFA (RVO)** | html | was EMPTY | host→`_scan_rvo` (rest_json) | **43** | **FIXED** |
| **Packard Foundation** | html | was EMPTY | host→`_scan_packard` (WP REST) | **1** | **FIXED** |
| Rockefeller Foundation | html | FALSE-OK | manual | 0 (7 awarded grants) | DEACTIVATE |
| Comic Relief | html | FALSE-OK | manual | 0 (7 fundraising/theme) | DEACTIVATE |
| Chan Zuckerberg Initiative | html | EMPTY | manual | 0 (awarded grants only) | DEACTIVATE |
| Hewlett Foundation | html | EMPTY | manual | 0 (grantee DB only) | DEACTIVATE |
| African Development Bank | html_js | EMPTY | manual | 0 (policy page) | DEACTIVATE |
| Deutsche Ges. f. Int. Zusammenarbeit (GIZ) | html | EMPTY | manual | 0 (login-walled) | DEACTIVATE |
| Swedish Int'l Dev. (calls.sida.se Moodle) | html | EMPTY | manual | 0 (redundant) | DEACTIVATE |
| The Global Fund | html | EMPTY | manual | 0 (Oracle portal) | DEACTIVATE-URL |
| Coalition for Epidemic Prep. (CEPI) | html_js | EMPTY | html_js | 0 (no current calls) | KEEP-EMPTY |
| Google.org | html_js | EMPTY | html_js | 0 (no current challenge) | KEEP-EMPTY |
| Google Alert — Gov/Org RFPs | rss | EMPTY | rss | 0 (feed valid, empty now) | KEEP-EMPTY |
| Google Alert — RFPs / CFPs / EOIs | rss | EMPTY | rss | 0 (feed valid, empty now) | KEEP-EMPTY |
| Open Society Foundations | html_js | EMPTY | html_js | 0 (mostly invitation) | KEEP-EMPTY |
| MIT Solve | html_js | EMPTY | (bespoke) | 0 (JS lazy cards) | NEEDS-PARSER |
| National Inst. for Health & Care Research (NIHR) | html_js | DEAD/EMPTY | (bespoke) | 0 (JS finder) | NEEDS-PARSER |
| Sidaction | **manual (active)** | EMPTY (no-op) | html_js | 0 (JS WordPress) | FIX-METHOD |

### Inactive sources (20)

| Source | Cur. method | Live? | Assessment | Action |
|---|---|---|---|---|
| Abdul Latif Jameel (J-PAL) | manual | 200 | Homepage; EVAH is invitation/initiative | MANUAL-OK |
| Coefficient Giving (ex-Open Phil) | manual | 200 | `/funds/` description page | MANUAL-OK |
| Development Innovation Ventures | manual | 200 | `/apply` rolling application page | MANUAL-OK |
| ELMA Philanthropies | manual | 200 | Homepage, no public calls | MANUAL-OK |
| Fund for Loss & Damage (FRLD) | manual | 403 | Bot-blocked all paths | MANUAL-OK (dead) |
| fundinnovation.dev | manual | 200 | Application page | MANUAL-OK |
| GiveWell | manual | 200 | Top-charities fund, not a call | MANUAL-OK |
| globalinnovation.fund | manual | 200 | Rolling application | MANUAL-OK |
| LEAP-RE (EU-AU energy) | manual | 200 | Single dated call page | MANUAL-OK (recheck) |
| National Institute of Health (NIH RSS) | manual | 403 | RSS 403 + US-domestic (grants.gov covers) | MANUAL-OK (dead) |
| Nestlé Foundation | manual | 403 | Bot-blocked homepage | MANUAL-OK |
| ocrahope.org | manual | 200 | Grant-programs description | MANUAL-OK |
| Robert Carr Fund | manual | 200 | `/funding/` — recheck for listings | MANUAL-OK (recheck) |
| thecatalystfund.com | manual | 200 | Pitch-to-us application | MANUAL-OK |
| U.S. Embassy Cameroon | manual | 404 | Feed gone | MANUAL-OK (dead) |
| UNFPA Procurement | html | 200 | Description page only; UNGM covers tenders | DEACTIVATE-OK ✓ |
| WHO ETDR Solicitations | manual | 200 | Salesforce SPA, login | MANUAL-OK |
| WHO TDR Grants (deprecated) | manual | 200 | Deprecated portal | MANUAL-OK |
| World Bank Projects | manual | 200 | Redundant with active worldbank.org procnotices | DEACTIVATE-OK ✓ |
| Zayed Sustainability Prize | manual | 500 | Application page, 500 | MANUAL-OK (dead) |

---

## 2. Per-source detail (grouped by action)

### 2a. FIXED this session — new handlers, tested working

#### Gates Foundation + Grand Challenges Global Health → `_scan_grandchallenges`
- **URLs:** `grandchallenges.org/grant-opportunities`, `gcgh.grandchallenges.org/grant-opportunities`
- **Alive?** Yes (200). **Type:** listings page (Next.js SPA).
- **Why it was broken:** the challenge cards render client-side; the generic anchor
  crawler saw only nav (Gates → NOISE) or nothing (GCGH → EMPTY).
- **Fix:** both pages server-side-embed the current calls in
  `<script id="__NEXT_DATA__">` at `props.pageProps.initialData.listing.data`. The
  handler parses that JSON directly (no Playwright), giving title, detail URL,
  description, funder, launch + **closing date** (UNIX), and `coming_soon`.
- **Proof (2026-06-21):**
  - *Innovations in Cost-Disruptive Tools for Diagnosis and Screening* — deadline **2026-06-23** — `…/challenge/innovations-cost-disruptive-tools-…`
  - *Breakthrough Solutions and Cost-Disruptive Innovations for Screening* — deadline **2026-07-01**
- One handler covers both rows (host intercept on `grandchallenges.org` + `grant-opportunit`).

#### Netherlands MFA → `_scan_rvo` (rest_json)
- **URL:** `english.rvo.nl/subsidies-programmes` (human page) → routed to keyless API
  `english.rvo.nl/api/rvo/v1/search-subsidies`.
- **Why it was broken:** Next.js SPA; generic crawler got 0.
- **Fix:** the search API returns `searchResults[]` with `title/url/summary/subsidyStatusName`
  and a `pager`. Handler keeps only **"Open for application"** and paginates (`?page=`).
- **Proof:** 43 open subsidies, e.g. *Accelerating Resilient Food Systems in Africa (ARFSA)*,
  *Connecting Europe Facility (CEF Transport)* → `english.rvo.nl/en/subsidies-financing/<slug>`.
- Note: RVO subsidies are broad (development + domestic); the downstream health/theme
  gate narrows them — that's the policy layer's job, not the source's.

#### Packard Foundation → `_scan_packard` (WordPress REST)
- **URL:** `packard.org/grantees/funding-opportunties/` (JS cards) → WP REST custom
  post type `wp-json/wp/v2/funding-opportunity`.
- **Proof:** *Community-Led Practices for Strengthening Maternal and Child …* (Open Call
  for Statements of Interest) → `packard.org/funding-opportunity/community-led-practic…`.
- Currently 1 item; the content type is correct and will grow.

### 2b. FALSE-OK — passes link metric, but zero real solicitations → DEACTIVATE

#### Rockefeller Foundation
- `…/grants/search/?post_type=grant…` is an **awarded-grants database**, not a call
  listing. All 7 "candidates" are recipient records — *Fiscal Policy Institute 2026*,
  *Bioversity International 2026*, *Institute of International Education, Inc. 2026* —
  and every one is rejected by the gate as `not-an-rfp`. Rockefeller is a proactive
  funder with **no public open-RFP listing**. **Recommend DEACTIVATE** (or `manual`).
- *(Corrects the prompt's known-case note: the grants/search URL is the awarded-grants
  DB, not a usable calls page.)*

#### Comic Relief
- `/funding/funding-opportunities/` yields 7 links, all fundraising challenges
  (Red Nose Day / Sport Relief — already blacklisted) or theme **description** pages
  (*Alleviating the Effects of Poverty*, *Pop Culture for Social Change*). No open RFPs
  with deadlines; UK-domestic fundraising focus. **Recommend DEACTIVATE.**

### 2c. EMPTY — no machine-readable solicitations → DEACTIVATE

| Source | Finding |
|---|---|
| **Chan Zuckerberg Initiative** | Only data API is `wp-json/czi/v1/grants/` — an **awarded-grants** list (recipient, amount, commitment year). No open-calls endpoint or page. *(Corrects prompt note.)* |
| **Hewlett Foundation** | WP REST exposes only a `grantee` post type (awarded recipients). No funding-opportunity/RFP content type. Proactive funder. |
| **African Development Bank** | `/projects-and-operations/procurement` is a **policy/guidance** page (links: *new-procurement-policy*, etc.); 0 call-mentions when rendered. Real AfDB tenders live on a separate eProcurement system. |
| **GIZ** (ausschreibungen.giz.de) | **Login-walled** tender portal (ISO-8859-1 landing, 4× "login"). Uncrawlable without registration. |
| **Sida #2** (`calls.sida.se`) | A **Moodle** instance (calls modelled as "courses"); generic crawler reads nothing useful. **Redundant** — the main `sida.se/.../calls-and-announcements` source is healthy (OK). |
| **The Global Fund** | Catalogue URL is the **Oracle Fusion** negotiation portal (`…oraclecloud.com/…/NegotiationAbstracts`), an ADF SPA that returns only a JS shell to crawlers and exposes no JSON API (`probe_apis` captured nothing on `/open-tenders/` either). **Recommend:** change URL to the readable `theglobalfund.org/en/business-opportunities/` and set `manual` (human review of "View Open Tenders"). |

### 2d. KEEP-EMPTY — valid page/feed, no current calls (keep scanning)

- **CEPI** (`/calls-for-proposals`): JS page, no API; rendered text shows no live call
  cards right now. Rule #3 — keep active and keep scanning.
- **Google.org** (`/impact-challenges/`): periodic challenge program; no active challenge
  currently. Keep; revisit if a challenge launches (it exposes an `index.rss`).
- **Google Alert ×2**: both Atom feeds return HTTP 200, valid XML, **0 entries** right now
  — alerts are live but unmatched. Keep.
- **Open Society Foundations** (`/grants`): JS page; OSF funding is largely
  invitation-only and currently shows no open calls to crawl. Keep; low expected yield.

### 2e. NEEDS-PARSER — real content exists, no API, generic crawler can't read

- **MIT Solve** (`/challenges`): challenges are present (33 "challenge" mentions in the
  rendered text) but rendered as **lazy-loaded / non-anchor cards** — 0 `/challenges/<slug>`
  anchors in the DOM and no JSON XHR. A bespoke handler would need to trigger the
  open-challenges section and parse card elements. *Effort: medium; yield: a handful of
  challenges. Deferred — flagged for a dedicated build.*
- **NIHR** (`/funding-opportunities`): JS "funding finder"; GET returns a shell (HEAD/GET
  intermittently 405), no JSON endpoint captured. Needs reverse-engineering of the search
  (likely a POST/search service). *Deferred — flagged.*
- **Sidaction** (`/appels-a-projet/`): **active but `method=manual`** → currently a
  **no-op in every scan**. Page is a JS WordPress listing (`html` yields 0). Relevant
  (French HIV/AIDS calls). *Needs an `html_js` parser or its WP/admin-ajax endpoint;
  flagged.*

### 2f. KEEP — healthy, no change (35 sources)

These extract real solicitation links in test (counts in the table above). Spot-check
notes:
- High-yield structured APIs are solid: **grants.gov** (71), **EU F&T** (100), **UNGM**
  (30), **worldbank** (24), **UNOPS** (5) — all via dedicated `rest_json` handlers.
- Strong donor listings: **MMV** (24, 5 eligible), **Stanford** (9, 4 eligible),
  **IDRC** (21, 2 eligible), **Global Affairs Canada** (31), **Pierre Fabre** (11),
  **DevelopmentAid** (40), **Research.Swiss** (40).
- Feeds healthy: **gov.uk atom** (50), **EDCTP3** (30), **ReliefWeb** (20), **Novo
  Nordisk** (10), **Health Research** (10).
- **The Pandemic Fund** is OK (3 calls via `html_js` on `/call-for-proposals`) — the
  prompt's "use `/news` instead" note is **outdated**; no change needed.

> Caveat: KEEP sources with very low candidate counts and recipient-style titles
> (e.g. single-item foundation pages) deserve periodic human spot-check for the same
> awarded-grants-vs-calls trap documented above. The audit health metric alone can't
> distinguish them when the gate also can't see a deadline.

### 2g. Inactive — assessments

Most inactive rows are correctly `manual`/deactivated: application pages, description
pages, login-walled portals, or genuinely dead URLs (FRLD 403, U.S. Embassy Cameroon
404, Zayed 500, NIH RSS 403). Two are correctly deactivated as redundant/description-only:
**UNFPA Procurement** (UNGM covers UN tenders) and **World Bank Projects** (active
`worldbank.org` procnotices covers it). Worth a future recheck: **Robert Carr Fund**
`/funding/` (200) and **LEAP-RE** (200) in case they expose a listing.

---

## 3. Changes appendix

### 3a. Applied — code (in the worktree, NOT committed)

`core/scraper.py`:
- New helper `_ts_to_date()` (UNIX epoch → date).
- New `_scan_grandchallenges()` — Next.js `__NEXT_DATA__` parser (Gates + GCGH).
- New `_scan_rvo()` — RVO subsidy search JSON (rest_json), "Open for application" only.
- New `_scan_packard()` — WordPress REST `funding-opportunity` post type.
- `scan_source()` host intercepts added for `grandchallenges.org` (+`grant-opportunit`),
  `english.rvo.nl`, and `packard.org`.

`scripts/probe_apis.py`:
- Added NIHR + Global Fund open-tenders to `TARGETS` (investigation aid).

All four handlers re-tested 2026-06-21 and return real solicitation links (proof above).
No DB rows were modified. Nothing committed or pushed.

### 3b. DB updates — APPLIED 2026-06-21

Applied to the live `donor_sources` catalogue (each row's `notes` was stamped
`[audit 2026-06-21]` with the reason; previous notes preserved). Active count: 55 → 48.

**Deactivated (no machine-readable solicitations), `is_active = false`:**
`Rockefeller Foundation`, `Comic Relief`, `Chan Zuckerberg Initiative`,
`Hewlett Foundation`, `African Development Bank`, `Deutsche Gesellschaft … (GIZ)`,
`Swedish Int'l Dev. (calls.sida.se)`.

**Re-pointed + set manual:**
`The Global Fund` → `rfp_listing_url = https://www.theglobalfund.org/en/business-opportunities/`,
`scrape_method = manual` (stays active for human review of "View Open Tenders").

**Not changed — `Sidaction`** (active no-op): leave `manual` until the `html_js`/WP
parser is built (see §3c). Flipping to `html` now would still scan nothing.

**No DB change needed for the 4 FIXED sources** — they work via host intercept
regardless of the stored `scrape_method`.

### 3c. Deferred parser builds (flagged)

`MIT Solve`, `NIHR`, `Sidaction` — real content, no API; each needs a bespoke
DOM/endpoint parser. Recommend one focused session per site (pattern: `probe_apis` →
if no API, render + parse card elements → wire host intercept → re-test with
`inspect_source`).

---

## 4. Source identity & donor linkage (migration 041)

Every source row in **both** tables now carries a stable unique identifier and a
link to the Donor Intelligence Mapping table, so sources can be identified
uniquely and triangulated to donors.

**Columns added** (`db/migrations/041_source_uid_and_donor_link.sql`):

| Column | `donor_sources` | `source_registry` | Meaning |
|---|:--:|:--:|---|
| `source_uid` | ✓ | ✓ | Stable, unique, human-readable key. Host-based; a 6-char URL hash is appended only when a host carries >1 catalogue row. In the registry it equals the host. |
| `host` | ✓ | (PK) | Normalised netloc (strip `www.`). **Join key** between the URL-keyed catalogue and the host-keyed registry. |
| `donor_intel_id` | ✓ | ✓ | FK → `donor_intel(id)` (`ON DELETE SET NULL`). The donor this source belongs to. |
| `donor_key` | ✓ | ✓ | Snapshot of `donor_intel.canonical_key` — the human-readable triangulation key. |

**The three-way triangulation:**
```
source_registry.host  ──►  donor_sources.host            (curation ↔ scan catalogue)
donor_sources.donor_intel_id ──► donor_intel.id          (source ↔ donor)
donor_sources.donor_key      ──► donor_intel.canonical_key
```

**Backfill** (`scripts/backfill_source_uids.py`, idempotent): all **75 + 75** rows
have a unique `source_uid`; donors resolved via `core.donor_intel.match_donor`
(canonical_key / donor / donor_short / aliases). Linked: **44/75** catalogue +
**36/75** registry. The 3 colliding hosts (`google.com`, `who.my.site.com`,
`projects.worldbank.org`) correctly received `host-<hash>` uids.

**Column display order:** `source_uid` is added last by `041`, so it appears at the
far right in the Supabase Table Editor. Migration `042_reorder_source_uid_first.sql`
rebuilds both tables (temp-table swap, atomic, preserves PK/CHECK/FK/indexes/RLS/
grants/trigger) to move `source_uid` to the **first** column. Validated by
execute-then-rollback against the live schema; run it in Supabase when ready.

**Unmatched (31 catalogue sources) — donor absent from `donor_intel`.** These are
left `NULL` (honest — no fabricated links) and are the follow-up backlog for
extending the mapping table (add the donor row or an alias, then re-run the
backfill). Notable: CHINNOVA, GACD, MMV, Stanford, Pfizer, UNOPS, UNGM, grants.gov,
Pierre Fabre, IDRC, ReliefWeb, gov.uk, SciDevNet, GCGH, Nestlé, plus near-misses
that just need an alias (`worldbank.org`→"World Bank Group", `Netherlands MFA`→
"Government of the Netherlands"). Aggregator hosts in the registry (grantbite,
opportunitysquare, etc.) are correctly unlinked — they aren't donors.

## 5. Follow-up hygiene

- Per `docs/EXTENDING_SOURCES.md`, add `config/donor_field_map.yaml` sections for the
  new donors (Grand Challenges, RVO, Packard) so dedup signals and field mappings stay
  in sync, and drop smoke tests asserting the promised fields.
- Extend `donor_intel` for the 31 unlinked catalogue donors (§4), then re-run
  `python scripts/backfill_source_uids.py` to pick up the new links (idempotent).
