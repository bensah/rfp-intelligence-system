-- Migration 064 — donor_intel.field_provenance (E3 auto-enrichment; owner 2026-06-30).
-- Tracks WHERE each donor field's value came from so auto-derived data is distinct from
-- human-verified data (and so the donor form can show auto values as "suggested"):
--   {"<field>": "human_verified" | "from_call" | "auto_created", "_meta": "auto_created"}
-- Mirrors the extracted_solicitations.field_provenance pattern (migration 044). The E3
-- loop fills BLANK donor fields from a call as 'from_call'; a human Save marks the fields
-- they touched 'human_verified'; an auto-created donor stub gets _meta='auto_created'.
-- Eligibility SCORING is unaffected (provenance is metadata, not a scored value).
-- Idempotent — safe to re-run.

alter table donor_intel
    add column if not exists field_provenance jsonb not null default '{}'::jsonb;
