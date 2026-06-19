-- Migration 032 - donor_intel: structured ELIGIBILITY conditions that feed the
-- computed Qualification (MUST-1) hard-AND. Each activates a qualification check
-- only when documented; any activated condition the applicant fails -> "No, not
-- eligible". Captures the exclusionary / numeric-threshold conditions real funders
-- impose (early-stage-only, budget ceilings/floors, US-HQ-only, no-INGO-affiliate,
-- named-partner, pre-registration). All TEXT (BLANK = not documented). Idempotent.

-- Yes/no flags (render as checkboxes in the donor form's Requirements group)
alter table donor_intel add column if not exists independent_entity_required   text;  -- excludes INGO affiliates/branches
alter table donor_intel add column if not exists welcome_registration_required  text;  -- pre-registration / senior-leadership pre-approval (e.g. Wellcome)

-- Valued conditions
alter table donor_intel add column if not exists hq_country_required        text;  -- applicant must be HQ'd here (e.g. United States)
alter table donor_intel add column if not exists org_stage_required         text;  -- "early-stage" / "established" / "any"
alter table donor_intel add column if not exists max_annual_budget_usd      text;  -- eligibility CEILING on applicant size
alter table donor_intel add column if not exists min_track_record_usd       text;  -- eligibility FLOOR on largest grant managed
alter table donor_intel add column if not exists required_partner_type      text;  -- e.g. "Academic / research institutions"
alter table donor_intel add column if not exists required_partner_country   text;  -- e.g. "United Kingdom" (NIHR)
alter table donor_intel add column if not exists max_request_pct_of_budget  text;  -- request may be <= X% of total project budget
alter table donor_intel add column if not exists min_cofinancing_secured_pct text; -- must have secured >= Y% from other sources
