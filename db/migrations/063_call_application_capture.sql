-- Migration 063 — Workstream 2 (LLM extraction enrichment; owner 2026-06-29).
-- Two new call-level (rfp_submissions) output columns. These are CRITERION/workflow
-- OUTPUTS, so per the data-model convention (docs/DATA_MODEL.md, axis 7) they keep BARE
-- names — like how_to_apply / compliance_requirements alongside them.
--   * application_checklist — the concrete deliverables an applicant must submit
--     (concept note, full proposal, budget, logframe, registration cert, CVs, letters
--     of support, …). LLM-extracted from the call text; deterministic regex fallback.
--   * eligibility_specifics — call-SPECIFIC eligibility constraints beyond the generic
--     country/theme fit (e.g. "Activities must focus on UNESCO World Heritage Sites").
-- (Selection process is folded into how_to_apply — no column needed.)
-- Stored as text (one item per "• …" line) to match the existing how_to_apply /
-- compliance_requirements fields and display via the same _section() helper.
-- Idempotent add — safe to re-run.

do $$
begin
  if not exists (select 1 from information_schema.columns
                 where table_name='rfp_submissions' and column_name='application_checklist') then
    alter table rfp_submissions add column application_checklist text;
  end if;
  if not exists (select 1 from information_schema.columns
                 where table_name='rfp_submissions' and column_name='eligibility_specifics') then
    alter table rfp_submissions add column eligibility_specifics text;
  end if;
end $$;
