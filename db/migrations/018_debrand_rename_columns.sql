-- Migration 018 — de-brand DB columns (strip the "chai_" prefix).
--
-- The app is a generic, multi-tenant product (RFPIS); "chai_" column
-- names were carried over from the original Excel screener and leak the
-- reference-deployment's branding into every install's schema. This
-- renames the two the organisation-prefixed columns to provider-neutral names that
-- already match their UI labels:
--
--   rfp_submissions.chai_role   -> applicant_role   ("Applicant role" / "Role")
--   engagement_logs.chai_lead   -> internal_lead    ("Internal lead")
--
-- Postgres automatically updates dependent objects (views, indexes,
-- constraints) on a column rename, so no further DDL is needed.
--
-- IMPORTANT: the application code already expects the new column names.
-- Run this migration BEFORE (or together with) deploying that code, or
-- the Activity/Actions, Pipeline, Grants, and Report pages will error
-- with "column ... does not exist".
--
-- Idempotent: guarded so re-running (or running against a fresh DB that
-- already has the new names) is a no-op.

do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_name = 'rfp_submissions' and column_name = 'chai_role'
  ) then
    alter table rfp_submissions rename column chai_role to applicant_role;
  end if;

  if exists (
    select 1 from information_schema.columns
    where table_name = 'engagement_logs' and column_name = 'chai_lead'
  ) then
    alter table engagement_logs rename column chai_lead to internal_lead;
  end if;
end $$;
