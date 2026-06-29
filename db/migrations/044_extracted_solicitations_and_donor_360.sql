-- 044: Extraction-first architecture — `extracted_solicitations` (global, raw,
-- org-agnostic) + donor-360 columns + safe additive columns on rfp_submissions.
--
-- Spec: docs/DATA_SCHEMA_ETL.md (§4 schema, §5 donor 360, §9 migrations).
--
-- This migration is **ADDITIVE and SAFE to run now** — it only CREATEs a new
-- table and ADDs columns (all `if not exists`). It does NOT drop or rewrite any
-- existing column, so it cannot break the running app. The destructive steps
-- (dropping search_date/form_*, remapping source/submitted_by, splitting
-- funding_agency, restructuring program_area) are DEFERRED — see the commented
-- "PHASE 2" block at the bottom and the code-cleanup checklist there. Do NOT run
-- Phase 2 until that code work is merged.
--
-- Conventions follow migration 041: idempotent adds, FK `on delete set null`,
-- donor link via donor_intel(id) [bigint PK] + a donor_key snapshot.

-- ===========================================================================
-- 1. NEW TABLE: extracted_solicitations  (the global, public-facing raw store)
-- ===========================================================================
create table if not exists extracted_solicitations (
    -- identity & links
    uid                        text primary key,          -- content-hash based
    opportunity_name           text not null,
    opportunity_id             text,
    opportunity_url            text not null,
    apply_url                  text,
    funding_opportunity_number text,

    -- funder & triangulation (to donor_intel)
    funder_name                text,
    agency_code                text,
    grantmaking_entity         text,                       -- distinct from funder
    donor_intel_id             bigint references donor_intel(id) on delete set null,
    donor_key                  text,                       -- canonical_key snapshot

    -- narrative (LLM, house-style)
    brief_description          text,
    full_description           text,
    applicant_fit_profile      text,
    project_stages             text,

    -- eligibility (structured + narrative; "as stated", not org MUST/PREFER)
    what_is_funded             text,
    what_is_not_funded         text,
    eligibility_applicant_types jsonb default '[]'::jsonb,
    eligibility_countries      jsonb default '[]'::jsonb,
    eligibility_other          jsonb default '[]'::jsonb,

    -- money
    grant_amount               numeric,
    award_floor                numeric,
    award_ceiling              numeric,
    total_program_funding      numeric,
    expected_awards            text,
    currency                   text,

    -- dates & window
    date_posted                date,
    deadline                   date,
    deadline_confidence        text,                       -- high|med|low
    funding_status             text default 'Open',        -- Open|Closed
    funding_window             text,                       -- One-off|Rolling
    expected_award_date        date,
    time_to_award              text,
    project_duration           text,
    submission_format          text,

    -- classification (raw, ALL geographies — geography is NOT an entry gate)
    solicitation_type          text,
    instrument_type            text,
    opportunity_type           text,                       -- gate: funding only
    focus_themes               jsonb default '[]'::jsonb,
    program_areas              jsonb default '[]'::jsonb,  -- canonical keys
    geographic_scope           jsonb default '[]'::jsonb,  -- exact as listed
    solicitation_language      text default 'English',

    -- attachments & referenced documents
    attachments                jsonb default '[]'::jsonb,  -- [{url,label,doc_type}]
    resource_links             jsonb default '[]'::jsonb,  -- [{url,label,type}]

    -- provenance / audit (per-field method+confidence+tier kept in field_provenance)
    source                     text,
    source_uid                 text,
    raw_text                   text,
    content_hash               text,
    extraction_confidence      text,                       -- overall high|med|low
    field_provenance           jsonb default '{}'::jsonb,  -- {field:{method,confidence,source_tier,source_url}}
    scraped_at                 timestamptz default now(),
    created_at                 timestamptz default now(),
    updated_at                 timestamptz default now()
);

create index if not exists extr_sol_donor_intel_idx on extracted_solicitations(donor_intel_id);
create index if not exists extr_sol_oppurl_idx       on extracted_solicitations(opportunity_url);
create index if not exists extr_sol_content_hash_idx on extracted_solicitations(content_hash);
create index if not exists extr_sol_status_idx       on extracted_solicitations(funding_status);
create index if not exists extr_sol_deadline_idx     on extracted_solicitations(deadline);
create index if not exists extr_sol_source_idx       on extracted_solicitations(source);

-- ===========================================================================
-- 2. rfp_submissions (Screened) — additive only
-- ===========================================================================
-- agency_code: the acronym, split from funding_agency. funding_agency KEEPS the
-- full name (no rename — 32 files reference it). Population is a later backfill.
alter table rfp_submissions add column if not exists agency_code   text;
-- FK from the per-tenant Screened row back to the global extracted row.
alter table rfp_submissions add column if not exists extraction_uid text
    references extracted_solicitations(uid) on delete set null;
create index if not exists rfp_sub_extraction_uid_idx on rfp_submissions(extraction_uid);

-- ===========================================================================
-- 3. donor_intel — donor-360 additive columns
-- ===========================================================================
-- donor_type: descriptive label complementary to donor_category (drives the
--   per-donor extraction source-template; see DATA_SCHEMA_ETL.md §5.2).
-- is_dual_role_implementer: 'yes' for implementers that also publish calls
--   (CHAI, Care, Sightsavers…), captured as funders.
-- opportunity_listing_urls: the donor's RFP/tender/grant listing page(s) — the
--   "donor source URLs" for scanning (pipe-separated).
alter table donor_intel add column if not exists donor_type               text;
alter table donor_intel add column if not exists is_dual_role_implementer text;
alter table donor_intel add column if not exists opportunity_listing_urls text;

-- ===========================================================================
-- PHASE 2 — DEFERRED / DESTRUCTIVE.  *** DO NOT RUN until the code below is updated ***
-- ===========================================================================
-- These break the running app if executed now. Each needs a code change first.
--
-- (a) DROP redundant columns on rfp_submissions.
--     BEFORE dropping, backfill submitted_at from search_date:
--       update rfp_submissions set submitted_at = coalesce(submitted_at, search_date)
--         where submitted_at is null and search_date is not null;
--     Then, after removing all reads/writes in these files —
--       core/scan_pipeline.py, core/found_loader.py, core/notifications.py,
--       views/rfp_records.py, views/submit_form.py, views/summary_rfp.py,
--       views/report.py, scripts/migrate_excel.py, scripts/backfill_form1_meta.py,
--       scripts/recompute_review_weeks.py, scripts/dedup_existing.py —
--       alter table rfp_submissions drop column if exists search_date;
--       alter table rfp_submissions drop column if exists form_start_date;
--       alter table rfp_submissions drop column if exists form_end_date;
--
-- (b) SPLIT funding_agency -> agency_code (backfill, NOT a DB rewrite).
--     Run scripts/backfill_agency_code.py (to be written): for each row, split on
--     " - " / " – " (en-dash) into agency_code (acronym) + funding_agency (full);
--     for rows without a dash, look up the acronym in donor_intel(donor_short).
--     funding_agency stays the full name. (32 files read funding_agency — keep it.)
--
-- (c) RESTRUCTURE program_area — strip "Category -" prefix + map to canonical
--     keys (ID -> Infectious Diseases, etc.). Do as a Python backfill against the
--     program_area_classifier taxonomy, not raw SQL.
--
-- (d) VALUE-MAPS are DISPLAY-ONLY — do NOT rewrite the stored values:
--       source 'auto'->'system', 'migration'->'excel';  submitted_by 'auto-scan'
--       -> org user name.  Code checks `source == 'auto'`, so remap in the UI
--       layer (a label map), NOT in the database.
