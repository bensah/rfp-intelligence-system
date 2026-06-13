-- Migration 025 - donor_intel: add the qualitative donor-intelligence profile
-- fields surfaced in the Donors page edit form (About / Funding footprint /
-- Intelligence / Past projects). Until these columns exist the form shows the
-- inputs but silently drops them on save (app_pages/donors.py filters the
-- payload to real columns); applying this makes them persist.
-- All TEXT (BLANK = 'not documented' / unknown -> never coerce to false);
-- past_projects_json holds a JSON array of {title, amount, currency, year,
-- country}. Idempotent: ADD COLUMN IF NOT EXISTS.

-- About this donor
alter table donor_intel add column if not exists summary_description     text;
alter table donor_intel add column if not exists mission                 text;
alter table donor_intel add column if not exists vision                  text;
alter table donor_intel add column if not exists donor_values            text;
alter table donor_intel add column if not exists strategy_url            text;

-- Funding footprint (counts + money, free text so "~120/year" is fine)
alter table donor_intel add column if not exists total_awards            text;
alter table donor_intel add column if not exists total_funding_to_date   text;
alter table donor_intel add column if not exists current_awards          text;
alter table donor_intel add column if not exists past_awards             text;
alter table donor_intel add column if not exists projected_budget        text;
alter table donor_intel add column if not exists projected_budget_period text;

-- Intelligence (qualitative profile)
alter table donor_intel add column if not exists funding_cycle           text;
alter table donor_intel add column if not exists recent_activity         text;
alter table donor_intel add column if not exists application_process     text;
alter table donor_intel add column if not exists reporting_requirements  text;

-- Past projects (JSON array: [{title, amount, currency, year, country}, ...])
alter table donor_intel add column if not exists past_projects_json      text;
