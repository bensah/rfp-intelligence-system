# Per-Source Extraction Handlers — Fresh-Session Prompt

Paste this into a dedicated session to build source-specific extraction handlers,
batch by batch. Companion to `docs/SOURCE_VERIFICATION_PROMPT.md` (which checked
source HEALTH); this one defines, per source, HOW to read its pages and MAP each
field to our schema. Reference architecture: `docs/DATA_SCHEMA_ETL.md`.

=== PROMPT START ===

## Mission
For each catalogued donor source, write a **targeted extraction handler** that maps
that source's own page/API structure to our `extracted_solicitations` schema. Each
source is specific — `grants.gov` ≠ EU F&T portal ≠ UNGM ≠ research.swiss. Generic
full-page scraping produces wrong/empty fields (wrong reject reasons, missing
amounts/deadlines). We fix this source by source.

## Efficiency principle (NON-NEGOTIABLE)
Do NOT dump full pages to the LLM as the primary method — it's slow, costly, and
unreliable. Extract in this order, stopping at the first that yields clean fields:

1. **Official API** — grants.gov, simpler.grants.gov, EU Funding & Tenders portal
   (SEDIA search API), SAM.gov, UNGM. Structured, stable, cheap. Prefer always.
2. **Structured data embedded in the page** — JSON-LD (`<script type="application/
   ld+json">`, schema.org), OpenGraph/meta, microdata, embedded app JSON
   (`__NEXT_DATA__`, `window.__INITIAL_STATE__`, `.astro`/Nuxt payloads).
3. **RSS / Atom feed** if the source publishes one.
4. **CSS / XPath selectors** on stable HTML (use the detail page, not the listing).
5. **LLM fallback (gpt-oss:120b via Ollama Cloud)** — ONLY for the specific fields
   1–4 couldn't get, run on the RELEVANT extracted section, never the whole page.
   Record provenance method="llm".

JS-rendered SPAs (e.g. EU F&T portal): use the **API** (1), not Playwright on the
SPA. If no API, Playwright-render then apply (2)/(4). If the page yields <200 chars
of text and has no API, log it — don't pretend it extracted.

## Target schema (map into these `extracted_solicitations` columns)
Required*: `opportunity_name*`, `opportunity_url*`, `apply_url`, `funder_name*`,
`grant_amount` (headline = largest tier), `deadline*`.
Money: `award_floor`, `award_ceiling`, `total_program_funding`, `currency`,
`funding_tiers` jsonb `[{stage,amount_min,amount_max,currency,notes}]` (staged calls).
Dates: `date_posted`, `funding_window` (One-off/Rolling), `expected_award_date`.
Classify: `solicitation_type`, `instrument_type`, `opportunity_type`
(grant/tender/award/**announcement** for future calls/jobs/scholarship — gate),
`geographic_scope` (EXACT list as stated, all geos), `solicitation_language`.
Eligibility: `what_is_funded`, `what_is_not_funded`, `eligibility_applicant_types`,
`eligibility_countries`, `eligibility_other`.
Narrative (LLM, house style): `brief_description`, `full_description`,
`applicant_fit_profile`, `focus_themes`, `program_areas`.
Docs: `attachments` `[{url,label,doc_type}]`, `resource_links` (templates/guidance).
Provenance: set `field_provenance[field] = {method, confidence, source_tier, source_url}`.

## Where handlers live
`core/scraper.py` — existing `_scan_<source>()` handlers (grants.gov, grand
challenges, RVO, Packard …) + dispatch by source method/host. Add a new handler per
source and register it; return a list of candidate dicts using the schema keys
above. `core/extract.py` then runs the gate + LLM-fallback + writes the store, so a
handler only needs to FETCH + MAP. Keep handlers additive — never break existing ones.

## Per-source process (checklist, one source at a time)
1. Open the live LISTING url (from `donor_sources.rfp_listing_url`) and one DETAIL
   page. Note the document type (HTML/JSON/XML/.astro/Next) and whether an API exists.
2. Pick the method per the efficiency hierarchy. Find each schema field's exact
   location (API field / JSON-LD key / selector).
3. Write the handler; map fields; set provenance + source_tier (T1 = the call's own
   API/page).
4. **Test:** run it, confirm a sample call populates deadline + amount + geography +
   type CORRECTLY (compare to the live page). Verify expired/closed calls carry the
   right reject reason (deadline/closed, not geography).
5. Record the field-map in a one-line note (source → method → key mappings).

## Known source specifics (carry forward)
- **grants.gov / simpler.grants.gov** — use the API (already done; pattern to copy).
- **EU Funding & Tenders (HORIZON / EDCTP3)** — JS SPA; use the SEDIA search API for
  budget/deadline/eligibility. Currently yields ~190 chars via HTML → no amounts.
- **UNGM** — procurement notices; amounts often genuinely absent (OK, don't force).
- **research.swiss** — carries a "Closed call" status badge + a stated deadline;
  ensure both are read so closed calls reject with reason=deadline/closed.
- **GACD** — "Future … call topics 20XX" pages are ANNOUNCEMENTS of future calls →
  `opportunity_type=announcement`, not live RFPs (gate already rejects as not-an-rfp).
- **MDBs (World Bank/AfDB)** — procurement under government-borrower projects, not
  NGO grants; applicant is often the borrower.

## Batch order (highest yield first)
1. API-backed: grants.gov, simpler.grants.gov, EU F&T (EDCTP3/Horizon), SAM.gov.
2. High-yield HTML donors on the org's priority list (health, Africa).
3. Aggregators that resolve to primaries; then the long tail.
Do ~5 sources per batch; test each; commit per batch (local, no push unless asked).

## Guardrails
- Additive only; don't regress existing handlers. Test each before moving on.
- Respect robots/rate limits; reuse the page cache.
- Capture-as-stated (geography/eligibility = the call's own words, not org matching —
  org screening is downstream).
- If a source truly can't be parsed without heavy render, log it and move on; flag
  for an API/Playwright follow-up rather than dumping full pages to the LLM.

=== PROMPT END ===
