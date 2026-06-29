-- Migration 054 — Data-model rename, AXIS 1: GEOGRAPHY (see docs/DATA_MODEL.md).
-- Renames the geography columns to the source-prefixed, self-documenting scheme so the
-- scoring mapping is auditable: org_* / donor_* / call_*. The org geography lives in the
-- app_settings.org_profile JSON blob (migrated on read by org_profile._migrate_keys), so
-- only the donor_intel + call columns are physical renames here. Idempotent: each rename
-- runs only if the old column still exists and the new one doesn't.

do $$
begin
  -- donor: funding_scope_geographic -> donor_geographic_scope
  if exists (select 1 from information_schema.columns
             where table_name='donor_intel' and column_name='funding_scope_geographic')
     and not exists (select 1 from information_schema.columns
             where table_name='donor_intel' and column_name='donor_geographic_scope') then
    alter table donor_intel rename column funding_scope_geographic to donor_geographic_scope;
  end if;

  -- call: rfp_submissions.geographic_scope -> call_geographic_scope
  if exists (select 1 from information_schema.columns
             where table_name='rfp_submissions' and column_name='geographic_scope')
     and not exists (select 1 from information_schema.columns
             where table_name='rfp_submissions' and column_name='call_geographic_scope') then
    alter table rfp_submissions rename column geographic_scope to call_geographic_scope;
  end if;

  -- call: extracted_solicitations.geographic_scope -> call_geographic_scope
  if exists (select 1 from information_schema.columns
             where table_name='extracted_solicitations' and column_name='geographic_scope')
     and not exists (select 1 from information_schema.columns
             where table_name='extracted_solicitations' and column_name='call_geographic_scope') then
    alter table extracted_solicitations rename column geographic_scope to call_geographic_scope;
  end if;
end $$;
