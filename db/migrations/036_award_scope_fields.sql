-- Migration 036 — distinct award-scope fields on rfp_submissions.
-- grants.gov (and later the LLM extractor) expose these separately; storing them
-- as their own columns lets the public site report award range + how many awards
-- (e.g. total $100M / 100 awards → ~$1M each), beyond the single estimated_value.

alter table rfp_submissions
    add column if not exists award_floor             numeric,  -- min per-award
    add column if not exists award_ceiling           numeric,  -- max per-award
    add column if not exists total_program_funding   numeric,  -- total allocation
    add column if not exists expected_awards         integer,  -- # of awards
    add column if not exists funding_opportunity_number text;   -- FON (e.g. PA-FPH-27-001)
