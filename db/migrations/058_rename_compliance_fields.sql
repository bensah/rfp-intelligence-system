-- Migration 058 — Data-model rename, AXIS 5: COFINANCING & COMPLIANCE (MUST-5; docs/DATA_MODEL.md).
-- org_* compliance credentials live in the app_settings.org_profile JSON (migrated on read by
-- org_profile._migrate_keys; org_has_sam_uei/org_tax_exempt/org_funding_routes were already
-- prefixed) → not SQL. Here: the donor_intel requirement/route columns + the call's
-- compliance_flags. The MUST-5 FACTOR/ML keys (sam_uei, route, partnership, safeguarding, …)
-- are short-form internal ids and are NOT columns — untouched. Idempotent rename helper.

create or replace function _rfpis_rename(_t text, _old text, _new text) returns void as $$
begin
  if exists (select 1 from information_schema.columns where table_name=_t and column_name=_old)
     and not exists (select 1 from information_schema.columns where table_name=_t and column_name=_new) then
    execute format('alter table %I rename column %I to %I', _t, _old, _new);
  end if;
end $$ language plpgsql;

-- donor compliance gates
select _rfpis_rename('donor_intel', 'cost_sharing_match_required',          'donor_cost_sharing_match_required');
select _rfpis_rename('donor_intel', 'min_cofinancing_secured_pct',          'donor_min_cofinancing_secured_pct');
select _rfpis_rename('donor_intel', 'prefinance_required',                  'donor_prefinance_required');
select _rfpis_rename('donor_intel', 'audited_financials_required',          'donor_audited_financials_required');
select _rfpis_rename('donor_intel', 'audit_report_required',                'donor_audit_report_required');
select _rfpis_rename('donor_intel', 'sam_uei_registration_required',        'donor_sam_uei_registration_required');
select _rfpis_rename('donor_intel', 'tax_exempt_status_required',           'donor_tax_exempt_status_required');
select _rfpis_rename('donor_intel', 'safeguarding_policy_required',         'donor_safeguarding_policy_required');
select _rfpis_rename('donor_intel', 'authorized_signatory_signoff_required','donor_authorized_signatory_signoff_required');
select _rfpis_rename('donor_intel', 'welcome_registration_required',        'donor_welcome_registration_required');
select _rfpis_rename('donor_intel', 'partner_mou_required',                 'donor_partner_mou_required');
select _rfpis_rename('donor_intel', 'govt_mou_required',                    'donor_govt_mou_required');
select _rfpis_rename('donor_intel', 'govt_endorsement_letter_required',     'donor_govt_endorsement_letter_required');
select _rfpis_rename('donor_intel', 'local_board_required',                 'donor_local_board_required');
select _rfpis_rename('donor_intel', 'local_registration_required',          'donor_local_registration_required');
select _rfpis_rename('donor_intel', 'local_partner_required',               'donor_local_partner_required');
select _rfpis_rename('donor_intel', 'partnership_mandatory',                'donor_partnership_mandatory');
select _rfpis_rename('donor_intel', 'funding_platform_registration_required','donor_funding_platform_registration_required');
select _rfpis_rename('donor_intel', 'submission_portal_url',                'donor_submission_portal_url');
-- donor funding-route flags
select _rfpis_rename('donor_intel', 'grant_route',                'donor_grant_route');
select _rfpis_rename('donor_intel', 'procurement_tender_route',   'donor_procurement_tender_route');
select _rfpis_rename('donor_intel', 'loan_dev_finance_route',     'donor_loan_dev_finance_route');
select _rfpis_rename('donor_intel', 'subrecipient_partner_possible','donor_subrecipient_partner_possible');
select _rfpis_rename('donor_intel', 'direct_local_org_eligible',  'donor_direct_local_org_eligible');
select _rfpis_rename('donor_intel', 'govt_or_ccm_route_required',  'donor_govt_or_ccm_route_required');
-- call
select _rfpis_rename('rfp_submissions', 'compliance_flags', 'call_compliance_flags');

drop function _rfpis_rename(text, text, text);
