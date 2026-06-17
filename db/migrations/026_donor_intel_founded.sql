-- Migration 026 - donor_intel: add `founded` (year the organisation was
-- established). Surfaced in the Donors edit form as a year dropdown and in the
-- View / Share summaries. TEXT (stores the year as a string, e.g. "2006";
-- BLANK = not documented). Idempotent: ADD COLUMN IF NOT EXISTS.

alter table donor_intel add column if not exists founded text;
