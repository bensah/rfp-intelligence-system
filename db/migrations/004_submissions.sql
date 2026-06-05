-- Migration 004 — track multiple submissions per RFP.
-- The Excel screener has a "Submissions" column counting how many times
-- a single RFP was submitted to the donor (some calls allow multiple).
-- We default to 1 for all existing rows. Idempotent.

alter table rfp_submissions
  add column if not exists submissions integer not null default 1;
