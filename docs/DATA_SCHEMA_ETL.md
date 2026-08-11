# Data Schema & ETL Architecture (Extraction-first)

Status: **DRAFT for review** · Owner: Bernard · Last updated: 2026-06-24

This is the living spec for separating **extraction** (raw, org-agnostic capture)
from **scoring** (org-specific screening). It supersedes the implicit "scan =
extract + score in one pass" model.

---

## 1. Core principle

We have been **conflating extraction with scoring**. We now split them:

```
Crawl ─► EXTRACT (regex + LLM) ─► [hard gates] ─► ┌──────────────────────────┐
                                                   │  extracted_solicitations  │  GLOBAL / shared
   gates = deadline-past · off-theme ·             │  (ALL geographies)        │  org-AGNOSTIC
           not-an-RFP · opportunity-type ·         │  PUBLIC-FACING source     │  (our servers)
           language                                └─────────────┬────────────┘
   (GEOGRAPHY is NOT a gate here)                                │
                                                   per-tenant auto-scorer
                                                   (org × donor × solicitation;
                                                    geography = 2nd-tier filter)
                                                                 │
                                                   ┌─────────────▼────────────┐
                                                   │  rfp_submissions          │  PER-TENANT
                                                   │  UI: "Screened Solicitations"  (was "Found RFPs")
                                                   └───────────────────────────┘
```

**Capture-as-stated rule (applies to every field in `extracted_solicitations`):**
record what the **solicitation actually says**, not the org match.
- `geographic_scope` = exact countries/regions listed (`[LMICs]`, `[Nigeria, India]`)
  even when they don't match any org.
- `eligibility_*` = the call's own wording (LLM-paraphrased), **not** our
  MUST/PREFER structure.
The org lens is applied **only** downstream, in the per-tenant scorer.

---

## 2. Multi-tenant model (confirmed)

- **`extracted_solicitations` = GLOBAL.** We do the broad extraction once, store it
  on our servers. No org scoping. This is the eventual **public-facing** dataset.
- **`rfp_submissions` (Screened) = PER-TENANT.** Each subscribed org (the organisation is the
  first) runs *their own* screening against the shared extracted store and gets a
  curated, tailored view. Orgs never crawl the web — we do the heavy lifting; they
  consume the curation.
- Therefore scanning splits into two jobs:
  1. **Central extraction** (crawl → extract → `extracted_solicitations`) — runs once for everyone.
  2. **Per-org screening** (`extracted_solicitations` → score vs org policy → `rfp_submissions`) — runs per tenant.

(Today the organisation is the only tenant, so this works single-tenant immediately; the split
is designed so adding tenants needs no re-extraction.)

---

## 3. Hard gates (what blocks entry to `extracted_solicitations`)

Only these reject at extraction time:
- **Deadline past** (closed call).
- **Off-theme** (not health / out of theme vocabulary).
- **Not-an-RFP** (news, interview, grantee profile, guidance, donor-investment page).
- **Opportunity type** — keep **funding opportunities only** (grant / RFP / tender /
  award / EOI / procurement). **Reject jobs, scholarships, fellowships for now**
  (relax later; they get their own logic per the multi-model roadmap).
- **Language** — **English only for now**; French added when we go public-facing.

**Geography is NOT a gate** — it moves to the per-tenant scorer (tier 2). We keep
every geography in the extracted store.

---

## 4. `extracted_solicitations` schema

`*` = required to admit. Method: how the value is obtained.
Provenance columns (`*_confidence`, `*_method`) accompany the extracted fields so
the training/rating loop and ETL audit work.

### 4.1 Identity & links
| Field | Type | Method | Notes |
|---|---|---|---|
| `uid` (PK) | text | system | content-hash based |
| `opportunity_name` * | text | regex | |
| `opportunity_id` | text | regex | if present |
| `opportunity_url` * | text | regex | the listing/detail page |
| `apply_url` * | text | regex→LLM | the actual "Apply" link |
| `funding_opportunity_number` | text | regex | |

### 4.2 Funder & triangulation
| Field | Type | Method | Notes |
|---|---|---|---|
| `funder_name` * | text | regex→LLM tidy | the main donor |
| `agency_code` | text | dash-split / **donor_intel lookup** | acronym; derive from registry if absent |
| `grantmaking_entity` | text | regex→LLM | **distinct from funder** — who issues/administers (e.g. CGD funded by *Coefficient Giving*). Defaults to funder when same |
| `donor_uid` (FK) | text | link | **FK → `donor_intel`** for 360 complement (see §5) |

### 4.3 Narrative (LLM, house-style — see §7)
| Field | Type | Method | Notes |
|---|---|---|---|
| `brief_description` | text | LLM | 2–4 sentences; doubles as SEO meta-description |
| `full_description` (Project Overview) | text | LLM | 150–300 words, **original/SEO-safe**, no copy-paste |
| `applicant_fit_profile` | text | LLM | ideal applicant maturity/type from donor_intel + call |
| `project_stages` | text/array | LLM | stages considered for funding |

### 4.4 Eligibility (structured + narrative)
| Field | Type | Method | Notes |
|---|---|---|---|
| `what_is_funded` | bullets | LLM | scannable |
| `what_is_not_funded` | bullets | LLM | scannable |
| `eligibility_applicant_types` | array | LLM | NGO, Private for-profit, Academic, Government… |
| `eligibility_countries` | array | LLM | who may **apply** (may differ from work geography) |
| `eligibility_other` | array | LLM | Indigenous, Local org, board-registered… |

*(These map to the criteria already configured under org/donor intel; missing ones
are complemented from `donor_intel` via §5.)*

### 4.5 Money
| Field | Type | Method | Notes |
|---|---|---|---|
| `grant_amount` * | numeric | regex→LLM | |
| `award_floor` / `award_ceiling` | numeric | regex | if present |
| `total_program_funding` | numeric | regex | |
| `expected_awards` | numeric/text | regex | |
| `currency` | ISO code | regex→LLM | normalized |

### 4.6 Dates & window
| Field | Type | Method | Notes |
|---|---|---|---|
| `date_posted` | date | regex | keep |
| `deadline` * | date | regex→LLM | see deadline rule §6 |
| `funding_status` | enum | derived | **Open** by default; cron flips to **Closed** when deadline passes |
| `funding_window` | enum | LLM | One-off / Rolling |
| `expected_award_date` | date | LLM | |
| `time_to_award` | text | LLM | |
| `project_duration` | text | regex→LLM | |
| `submission_format` | text | regex→LLM | |

### 4.7 Classification
| Field | Type | Method | Notes |
|---|---|---|---|
| `solicitation_type` | enum | regex→LLM | RFP/CFP/EOI/NOFO/Tender… |
| `instrument_type` | enum | regex→LLM | Grant/Contract/Award… |
| `opportunity_type` | enum | classifier | grant/tender/award/job/scholarship → **gate**. See the note below — this and `instrument_type` are two axes, not two guesses at one answer |
| `focus_themes` (a.k.a. **Sector**) | array | LLM | reflect whole RFP; "Sector" is the public synonym |
| `program_areas` | array | LLM + taxonomy | canonical map (strip `Category -` prefix; `ID → Infectious Diseases`) |
| `geographic_scope` | array | regex-capture **+ LLM-validate** | **exact as listed**, all geos |
| `solicitation_language` | text | detector | default **English** |

#### `opportunity_type` vs `instrument_type` — two axes, not a contradiction

These answer different questions, separated by the moment of award:

| field | question | when | values |
|---|---|---|---|
| `opportunity_type` | **what pursuing this IS** — the coarse pursuit class the eligibility gate opts out of | BEFORE the award | Grant/funding call · Procurement · Consultancy · Training · Loan · Prize/Challenge · Announcement · Other |
| `instrument_type` | **the vehicle if you win** — what the donor↔beneficiary relationship becomes | AFTER the award | Grant · Cooperative Agreement · Contract · Loan · Equity/Investment · Prize/Award · Fellowship · Scholarship · Seed fund · In-kind/TA |
| `solicitation_type` | **how it is announced / how you apply** | — | NOFO · RFP · CFP · EOI · Tender · … |

So **"a grant call awarded as a Contract" is ordinary**, not a data error: a grant is
contracted once awarded, and a funder that words its agreement as a contract has not changed
what the opportunity was. 30 of 686 catalogue rows are that shape and none is wrong.

`core/award_type.py` is the single place this relationship lives. It:

* **canonicalises** `opportunity_type`, which drifted across code paths — "Grant/funding
  call" on 325 rows but bare "grant" on 23, "Announcement" on 11 and "announcement" on 44
* **complements** a missing axis from the one present, since they imply each other (187 rows;
  e.g. 148 say Procurement with no instrument, and a procurement is awarded via a contract).
  Inferred values are labelled as such and never written back as extracted facts.
* renders **one line** — "Grant/funding call, awarded as a grant" — instead of two rows a
  reviewer has to reconcile
* flags only combinations that are genuinely hard to explain. Over 686 rows: **623
  consistent, 55 unclassified** (an "Announcement" asserts no class), **7 unusual**
  (a procurement issuing a grant or equity), 1 with neither axis.

`Announcement` and `Other` mean "could not tell" and are never judged — a pairing rule must
not evaluate a value that was never asserted. Warning on the 30 legitimate grant-contract
rows would teach a reviewer to ignore the warning that matters on the 7.

### 4.8 Attachments & referenced documents (multi-collection-point)
Extracted just like the apply button — direct links + a clear hyperlinked label each.
| Field | Type | Method | Notes |
|---|---|---|---|
| `attachments` | JSONB array | regex+LLM | `[{url, label, doc_type}]` — PDFs/files shipped with the RFP |
| `resource_links` | JSONB array | regex+LLM | `[{url, label, type}]` — referenced **templates** (narrative, budget), full-RFP, guidance pages |

`doc_type`/`type` vocabulary: `full_rfp`, `narrative_template`, `budget_template`,
`guidance`, `annex`, `faq`, `other`.

### 4.9 Provenance
`source`, `source_uid`, `scraped_at`, `raw_text` (audit), `content_hash`,
and per-field `*_confidence` (high/med/low) + `*_method` (regex/llm/both/donor_intel)
+ `*_source_tier` (T1–T4, see §5.1) + `*_source_url` for eligibility/donor-derived
fields so authority is explicit and conflicts resolve top-down.

---

## 5. Donor triangulation & 360 profile

### 5.0 The correction (be precise about this)
The opportunity **landing page is usually just a summary / entry point**. The
authoritative eligibility logic is **distributed** across the full call package,
application instructions, donor policies, standard terms, templates, FAQs, amendments,
and country- or instrument-specific guidance. For regulated donors the binding rules
live in the call document(s) and anything **formally incorporated by the call** (e.g.
Grants.gov: full legal eligibility is in the *application instructions attached to each
opportunity*, even when the synopsis summarises it).

**Do NOT model triangulation as "scrape random donor pages."** Model it as a
**source hierarchy by authority**, and resolve conflicts top-down.

### 5.1 Source hierarchy (authority tiers)
Each captured datum is tagged with the tier it came from (`source_tier`), so the
system knows how much to trust it and can resolve conflicts (higher tier wins).

| Tier | What it is | What it answers | Use as eligibility evidence? |
|---|---|---|---|
| **T1 — Opportunity package** | call text, NOFO/RFP/RFA, application instructions, annexes, budget template, standard terms, amendments, official Q&A/addenda | **"Can we apply to THIS specific call?"** | **Yes — binding** |
| **T2 — Donor-wide rules** | grant/procurement policies, cost-eligibility, safeguarding, sanctions, audit, indirect-cost, co-financing, data & reporting rules | **"What compliance/budget/registration/reporting rules apply?"** | **Yes — binding** |
| **T3 — Strategic fit** | country strategies, programme strategy pages, investment roadmaps, annual work programmes, business forecasts, **historical award data** | **"Is this donor strategically aligned with us?"** | Pattern evidence only (esp. award history) — **not** an eligibility rule |
| **T4 — Discovery** | LinkedIn, newsletters, Devex, fundsforNGOs, ReliefWeb, aggregators | lead discovery / early warning | **No** — never decide eligibility from T4 unless it links back to a T1/T2 official source |

**Resolution rule:** T1 overrides T2 for a specific call; T2 fills what T1 omits; T3
informs *fit/scoring* (not eligibility); T4 only seeds discovery.

### 5.2 Per-donor-type authoritative source map
`donor_intel.donor_type` selects which extraction template/source-chain to apply.

- **US Gov — grants / cooperative agreements**: Grants.gov **+ the attached NOFO /
  application instructions** (binding eligibility) → awarding-agency policy (USAID /
  CDC / NIH) → SAM.gov registration & exclusion (active-registration / not-debarred).
- **US Gov — contracts**: SAM.gov Contract Opportunities + solicitation attachments &
  amendments + applicable federal acquisition rules (FAR). (Procurement, not grants.)
- **EU / European Commission**: EU Funding & Tenders Portal — call/topic page, **call
  document**, programme guide, work programme, model grant agreement,
  admissibility/eligibility criteria, portal Q&A. (Eligibility is set in the call
  guidelines.)
- **UK Gov / FCDO**: **Find a Grant** (grant opportunities + eligibility), **Contracts
  Finder** (contract opportunities + previous tenders/contracts above thresholds),
  FCDO eSourcing portal where applicable.
- **Multilateral Development Banks** (World Bank, AfDB, ADB, IsDB, IDB): project page,
  **procurement plan**, procurement notices, standard procurement documents, borrower
  procurement documents, bank procurement regulations. ⚠️ Many opportunities are
  **procurement under a government-borrower project**, not NGO grants — applicant is
  often the borrower/government.
- **UN agencies — procurement**: **UNGM** (tender notices, supplier registration,
  eligibility) → cross-check the issuing agency's own procurement page / eSourcing
  portal / terms.
- **UN agencies — programmatic calls** (UNICEF, UNDP, WHO, UNAIDS, UNFPA): the
  agency's own calls/procurement + **country-office** pages — more authoritative than
  aggregators.
- **Global health financing mechanisms** (Global Fund, Gavi, Unitaid, CEPI, GFF):
  application materials, eligibility policies, **allocation letters**, country support
  guidelines, **funding-request templates**, technical-review criteria,
  disease/programme-specific guidance. (Global Fund = country allocations via funding-
  request packages; Gavi support guidelines apply to eligible countries.)
- **Private foundations** (Gates, CIFF, Rockefeller, Wellcome, ELMA, Botnar): funding
  priorities, open-RFP page, applicant FAQ, grantmaking approach, tax-status rules,
  geographic focus, **grants database**. ⚠️ Many (e.g. Gates) **mostly invite directly**
  and rarely run open RFPs; **historical grants = pattern evidence, not eligibility**.
- **Bilateral development agencies** (AFD, GIZ, JICA, KOICA, Sida, Norad, SDC, AECID,
  AICS): agency procurement/grants portal, country strategy, programme pages, tender
  documents, standard conditions, **embassy/country-office** pages. ⚠️ Some centralise
  calls; others publish **country-specific** calls via embassies / implementing
  agencies / partner-country procurement systems → keep **both** a central donor
  profile **and** country-level source links.

### 5.3 How this wires into the schema/system
1. **Classify**: `donor_intel.donor_type` (the categories in §5.2) drives the
   source-chain template used for extraction and 360-completion.
2. **Link**: every extracted RFP carries `donor_uid` (FK → `donor_intel`).
3. **Capture with tier provenance**: each donor/eligibility datum stores
   `source_tier` (T1–T4) + `source_url` + `captured_at`, so authority is explicit and
   conflicts resolve top-down (§5.1).
4. **Complement at publish**: when a T1 solicitation field is silent, fill from T2
   donor-wide defaults (applicant types, registration, cost rules, templates), tagged
   as inherited (not call-specific). Never fill eligibility from T3/T4.
5. **Donor 360 workstream** (parallel): expand `donor_intel` into a full profile per
   §5.2 — per-type source links (central **and** country-level), donor-wide rule set,
   standard template library, strategy/award-history for fit. Once a source is found,
   we link and inherit.

`extracted_solicitations` is also the **training substrate**: invited researchers
rate/classify raw rows → improves org × donor × solicitation matching over time.
Award history (T3) is a especially strong signal for *fit*, used for scoring — never
as an eligibility gate.

---

## 6. Deadline rule (confidence-gated)

Three outcomes, not two:
- **Date found** (regex labeled-context match and/or LLM agreement) → store it,
  `deadline_confidence=high/med`, `funding_status=Open` (or Closed if past).
- **Certain there is no date** (LLM confirms rolling / year-round / explicitly no
  deadline, high confidence) → default **`Dec 31, {scan_year}`**, `funding_window=Rolling`.
- **Uncertain / extraction failed** → **do NOT default**; flag low-confidence →
  quarantine to Verify. (We only auto-default when *absolutely certain* it's missing.)

Pipeline: regex labeled-context date bank (`deadline`, `closing date`, `apply by`,
`applications close`, `due by`, `closes on`) + multi-format parser (`22 July 2026`,
`July 22, 2026`, `2026-07-22`, `22/07/2026`, optional `2:00 p.m. ET`) → classify each
candidate (submission vs posted vs award vs info-session) → LLM arbiter only on
0/multiple/relative dates → store value + confidence + source.

---

## 7. House style for LLM-written fields

For *exciting, readable, SEO-strong, AdSense-safe* output:
- **Voice**: clear, active, confident, third-person; plain English (gloss jargon).
  Factual-engaging — never hype/clickbait.
- **Grounding (hard rule)**: use only facts on the page or `donor_intel`. Never
  invent amounts/dates/eligibility; missing → omit/null. **Always paraphrase — never
  copy source sentences** (plagiarism + AdSense duplicate-content + SEO penalty; our
  pages must not duplicate the source's).
- `brief_description`: 2–4 sentences; hook first line → funder + what's funded + who +
  amount + deadline. Doubles as the SEO meta-description.
- `full_description`: 150–300 words, original phrasing, short scannable paragraphs;
  weave keywords (funder, theme, country, "grant/RFP/funding") naturally in the first
  100 words. This is the page's substance for AdSense.
- `what_is_funded` / `what_is_not_funded`: tight bullets.
- `applicant_fit_profile`: 2–3 sentences on ideal applicant maturity/type.
- Title tag = `opportunity_name`.

*(Bernard to provide guidance bullets; few-shot exemplars drawn from strong human
notes — export rows 7 Sida / 8 WHO foresight / 10 CGD.)*

---

## 8. Method-by-class summary (regex vs LLM)

- **Regex (capture)**: title, links, dates (first pass), amounts, currency, opp-number,
  funder name, applicant role.
- **Regex→LLM (validate/fallback)**: deadline, estimated_value, currency, duration,
  format, solicitation/instrument type, structured money fields.
- **LLM (semantic/synthesis)**: brief/full description, eligibility (all),
  focus_themes, program_areas, funding_window, expected_award_date, time_to_award,
  applicant_fit_profile, project_stages, key_risks, decision_note, geographic_scope
  validation, attachments/resource labels.
- **System/rule**: alignment_score, auto_recommendation, stage default, progress_status,
  funding_status, dedup, provenance.

The 11 **scoring criteria** (`qualification`…`decline_flags_present`) live in the
**Screened** layer and are decided **regex-vs-LLM-vs-human via the eval harness**
(`scripts/eval_llm_judge.py`, extended per-criterion).

---

## 9. Schema migrations required

1. **DROP** `search_date` (merge migration *Search Date* → `submitted_at`),
   `form_start_date`, `form_end_date`.
2. **SPLIT** `funding_agency` → `funding_agency` (full) + `agency_code` (acronym).
   Splitter handles both `-` and `–` (en-dash); auto-scan rows derive acronym from
   `donor_intel`.
3. **RESTRUCTURE** `program_area` — strip `Category -` prefix; canonical map.
4. **VALUE-MAP** `source` (auto→system, migration→excel); `submitted_by`/`_email`
   (auto-scan → org user).
5. **NEW** `extracted_solicitations` table (this schema).
6. **NEW** FK `extracted_solicitations.donor_uid → donor_intel`, and
   `rfp_submissions.extraction_uid → extracted_solicitations`.
7. **NEW** `donor_intel.donor_type` (categories in §5.2) — drives the per-donor
   extraction source-chain — plus donor-360 columns: per-type source links (central +
   country-level), donor-wide rule set, template library, strategy/award-history refs.

**Implemented:** the **additive, safe** subset (new `extracted_solicitations` table;
`rfp_submissions.agency_code` + `extraction_uid`; `donor_intel.donor_type` +
`is_dual_role_implementer` + `opportunity_listing_urls`) is in
`db/migrations/044_extracted_solicitations_and_donor_360.sql`. The destructive parts
(items 1–4: drops, funding_agency split backfill, program_area restructure, the
display-only source/submitted_by maps) are deferred there as a commented "PHASE 2"
block with the code-cleanup checklist — they break the app if run before the code is
updated.

---

## 10. Feed / early-detection (Google Alerts)

Google Alerts alone is structurally insufficient (keyword-shallow, latency, silent
drops). Plan:
1. **Email-forward inbox** (Bernard to set up): dedicated address subscribed to donor
   newsletters + where the team forwards spotted RFPs → parsed into
   `extracted_solicitations`. Catches launches before any crawler.
2. **Crawl-depth fixes** (the real gap per the 5-miss audit): follow **PDF** listings
   (WHO/AHPSR), descend into **child pages** (Wellcome schemes), handle Moodle-hosted
   calls (Sida `calls.sida.se`). This is the same work as attachment extraction.
3. **Add missing source: CGD** (`cgdev.org`) — genuinely absent from catalogue.
4. **Broaden/split alert queries** (Bernard); tighter site-watch on priority donors;
   grants.gov / simpler.grants.gov API (roadmap).

### 5-miss audit (2026-06-24)
- CGD — **missing from catalogue** → add.
- WHO/AHPSR — catalogued; missed because listing links to a **PDF**.
- Sida — catalogued (active page); the call was on the **inactive `calls.sida.se`** sub-site.
- Pandemic Fund — catalogued, exact URL; parse/depth.
- Wellcome — catalogued at `/schemes`; call is a **child page**.
→ Conclusion: mostly **extraction-depth**, not missing sources.

---

## 11. Build sequence

1. **This doc + migrations** (§9) — incl. `extracted_solicitations` table. *(zero-risk)*
2. **Extraction-first refactor** — pipeline writes raw → Extracted; scorer reads
   Extracted → Screened; move geography to tier-2.
3. **Deadline-confidence extractor** (§6).
4. **LLM synthesis fields** (§4.3/4.4, §7) in **shadow mode** first.
5. **Attachments + resource/template links** (§4.8) + crawl-depth (PDF/child) — also
   closes the feed misses.
6. **Donor 360 + triangulation** (§5).
7. **Records UI** — add "Extracted Solicitations" to Table dropdown; rename "Found
   RFPs" → "Screened Solicitations"; add researcher rate/classify action.
8. **Scoring-criteria A/B** (§8) — extend eval harness per-criterion.
9. **Multi-tenant screening split** (§2) — when a 2nd tenant arrives.
10. **Public-facing site** (later).

---

## 12. Confirmed decisions (2026-06-24)
- Keep `rfp_submissions` as the per-tenant Screened table; new `extracted_solicitations` upstream, FK-linked. ✓
- Reject jobs/scholarships/fellowships now; relax later. ✓
- English only now; French at public-facing. ✓
- Missing deadline → `Dec 31, {scan_year}` + `funding_window=Rolling` **only when certain**. ✓
- `geographic_scope` = regex-capture + LLM-validate. ✓
- LLM house style with SEO/AdSense; few-shot from human notes. ✓
- Scoring criteria decided by regex-vs-LLM-vs-human eval harness. ✓
