-- Migration 057 — Data-model rename, AXIS 4: LEGAL STATUS & ELIGIBILITY (MUST-1; docs/DATA_MODEL.md).
-- org_* fields (legal_type, entity_type, has_established_pi, active_donors, funder_history) live in
-- the app_settings.org_profile JSON (migrated on read by org_profile._migrate_keys) → not SQL.
-- Here: the donor_intel requirement columns only. Idempotent rename helper.

create or replace function _rfpis_rename(_t text, _old text, _new text) returns void as $$
begin
  if exists (select 1 from information_schema.columns where table_name=_t and column_name=_old)
     and not exists (select 1 from information_schema.columns where table_name=_t and column_name=_new) then
    execute format('alter table %I rename column %I to %I', _t, _old, _new);
  end if;
end $$ language plpgsql;

select _rfpis_rename('donor_intel', 'entity_type_required',   'donor_entity_type_required');
select _rfpis_rename('donor_intel', 'hq_country_required',    'donor_hq_country_required');
select _rfpis_rename('donor_intel', 'registration_region',    'donor_registration_region');
select _rfpis_rename('donor_intel', 'requires_pi',            'donor_requires_pi');
select _rfpis_rename('donor_intel', 'pi_country_scope',       'donor_pi_country_scope');
select _rfpis_rename('donor_intel', 'prior_beneficiary_rule', 'donor_prior_beneficiary_rule');
select _rfpis_rename('donor_intel', 'ngo_eligible',           'donor_ngo_eligible');
select _rfpis_rename('donor_intel', 'for_profit_eligible',    'donor_for_profit_eligible');

drop function _rfpis_rename(text, text, text);
