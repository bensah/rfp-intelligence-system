-- Migration 055 — Data-model rename, AXIS 2: PROGRAM AREAS / THEMES (see docs/DATA_MODEL.md).
-- Source-prefixed scheme: org_* lives in the app_settings.org_profile JSON (migrated on read
-- by org_profile._migrate_keys), so only the donor_intel + call columns are physical renames.
-- The program-area TAXONOMY engine (program_area_classifier, PROGRAM_AREA_KEYWORDS, the
-- dropdowns.yaml `program_areas` vocab) is infrastructure and intentionally NOT renamed.
-- Idempotent: each rename runs only if the old column still exists and the new one doesn't.

do $$
begin
  -- donor: priority_program_areas -> donor_priority_areas
  if exists (select 1 from information_schema.columns
             where table_name='donor_intel' and column_name='priority_program_areas')
     and not exists (select 1 from information_schema.columns
             where table_name='donor_intel' and column_name='donor_priority_areas') then
    alter table donor_intel rename column priority_program_areas to donor_priority_areas;
  end if;

  -- donor: program_area_ratings -> donor_priority_ratings
  if exists (select 1 from information_schema.columns
             where table_name='donor_intel' and column_name='program_area_ratings')
     and not exists (select 1 from information_schema.columns
             where table_name='donor_intel' and column_name='donor_priority_ratings') then
    alter table donor_intel rename column program_area_ratings to donor_priority_ratings;
  end if;

  -- call: rfp_submissions.program_area -> call_domain_areas
  if exists (select 1 from information_schema.columns
             where table_name='rfp_submissions' and column_name='program_area')
     and not exists (select 1 from information_schema.columns
             where table_name='rfp_submissions' and column_name='call_domain_areas') then
    alter table rfp_submissions rename column program_area to call_domain_areas;
  end if;

  -- call: extracted_solicitations.program_areas -> call_domain_areas
  if exists (select 1 from information_schema.columns
             where table_name='extracted_solicitations' and column_name='program_areas')
     and not exists (select 1 from information_schema.columns
             where table_name='extracted_solicitations' and column_name='call_domain_areas') then
    alter table extracted_solicitations rename column program_areas to call_domain_areas;
  end if;
end $$;
