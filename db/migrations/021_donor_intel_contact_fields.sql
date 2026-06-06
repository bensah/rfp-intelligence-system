-- Migration 021 - donor_intel: add institutional-contact + provenance columns.
-- The donor matrix grew 14 fields after migration 020: public institutional
-- contacts (hq_*, main_phone, general_email, *linkedin*, profile URLs), the
-- named-individual contact fields (BLANK by design - never mass-compiled), and
-- provenance/typing columns (award_size_basis, online_source_check_status,
-- last_checked, row_type). Adding them so the current matrix + the published
-- docs/donor_intel_template.xlsx import without column-mismatch errors.
-- All TEXT (BLANK = 'not documented' / unknown -> never coerce to false).
-- Idempotent: ADD COLUMN IF NOT EXISTS.

alter table donor_intel add column if not exists hq_address                 text;
alter table donor_intel add column if not exists hq_country                 text;
alter table donor_intel add column if not exists main_phone                 text;
alter table donor_intel add column if not exists general_email              text;
alter table donor_intel add column if not exists contact_persons            text;
alter table donor_intel add column if not exists contact_emails             text;
alter table donor_intel add column if not exists contact_phones             text;
alter table donor_intel add column if not exists donor_linkedin_url         text;
alter table donor_intel add column if not exists contact_linkedin_urls      text;
alter table donor_intel add column if not exists other_profile_urls         text;
alter table donor_intel add column if not exists award_size_basis           text;
alter table donor_intel add column if not exists online_source_check_status text;
alter table donor_intel add column if not exists last_checked               text;
alter table donor_intel add column if not exists row_type                   text;
