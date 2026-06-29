-- Migration 056 — Data-model rename, AXIS 3: AWARD / FUNDING SIZE + CEILINGS (docs/DATA_MODEL.md).
-- org_* award/target fields live in the app_settings.org_profile JSON (migrated on read by
-- org_profile._migrate_keys) → not SQL. Here: the donor + call DB columns only. `currency` /
-- `currency_secured` are unit qualifiers (not comparison axes) and are kept as-is.
-- Idempotent helper: rename old->new only if old exists and new doesn't.

create or replace function _rfpis_rename(_t text, _old text, _new text) returns void as $$
begin
  if exists (select 1 from information_schema.columns where table_name=_t and column_name=_old)
     and not exists (select 1 from information_schema.columns where table_name=_t and column_name=_new) then
    execute format('alter table %I rename column %I to %I', _t, _old, _new);
  end if;
end $$ language plpgsql;

-- donor (donor_intel)
select _rfpis_rename('donor_intel', 'max_annual_budget_usd', 'donor_max_annual_budget');
select _rfpis_rename('donor_intel', 'max_prior_grant_usd',   'donor_max_prior_grant');
select _rfpis_rename('donor_intel', 'min_track_record_usd',  'donor_min_track_record');
select _rfpis_rename('donor_intel', 'award_low_usd',         'donor_award_low');
select _rfpis_rename('donor_intel', 'award_high_usd',        'donor_award_high');
-- call (rfp_submissions + extracted_solicitations)
select _rfpis_rename('rfp_submissions',         'estimated_value', 'call_award_value');
select _rfpis_rename('rfp_submissions',         'award_ceiling',   'call_award_ceiling');
select _rfpis_rename('rfp_submissions',         'award_floor',     'call_award_floor');
select _rfpis_rename('extracted_solicitations', 'award_ceiling',   'call_award_ceiling');
select _rfpis_rename('extracted_solicitations', 'award_floor',     'call_award_floor');

drop function _rfpis_rename(text, text, text);
