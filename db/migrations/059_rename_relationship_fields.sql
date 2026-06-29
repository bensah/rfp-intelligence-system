-- Migration 059 — Data-model rename, AXIS 6: RELATIONSHIP / COMPETITIVENESS / BID-EFFORT
-- (PREFER-7/8/9; docs/DATA_MODEL.md). org_* fields (donor_registrations, founding_year) live in
-- the app_settings.org_profile JSON (migrated on read by org_profile._migrate_keys) → not SQL.
-- org_has_bd_team / org_is_multi_country / org_is_grassroot are already prefixed (settings record).
-- Here: the donor competitiveness flags + the call submission deadline. Idempotent rename helper.

create or replace function _rfpis_rename(_t text, _old text, _new text) returns void as $$
begin
  if exists (select 1 from information_schema.columns where table_name=_t and column_name=_old)
     and not exists (select 1 from information_schema.columns where table_name=_t and column_name=_new) then
    execute format('alter table %I rename column %I to %I', _t, _old, _new);
  end if;
end $$ language plpgsql;

-- donor (donor_intel)
select _rfpis_rename('donor_intel', 'funders_collaborators',      'donor_funders_collaborators');
select _rfpis_rename('donor_intel', 'multi_country_encouraged',   'donor_multi_country_encouraged');
select _rfpis_rename('donor_intel', 'global_multi_country_scope', 'donor_global_multi_country_scope');
select _rfpis_rename('donor_intel', 'lmic_africa_focus',          'donor_lmic_africa_focus');
-- call (rfp_submissions) — the deadline column (its index follows the column rename automatically)
select _rfpis_rename('rfp_submissions', 'submission_deadline', 'call_submission_deadline');

drop function _rfpis_rename(text, text, text);
