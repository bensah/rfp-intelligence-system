-- Migration 060 — Data-model rename, AXIS 8: NON-COMPARISON DONOR FAMILIES (docs/DATA_MODEL.md).
-- Prefix the remaining donor_intel columns donor_* for table uniformity: legacy *_fit program-
-- area flags, the per-proposal *_required doc family, narrative/profile, and contacts. EXCLUDED:
-- system cols, anything shared with rfp/extracted (notes), the central name col donor, and the
-- axis-6 fields (migration 059). None are scoring-comparison inputs. Idempotent rename helper.

create or replace function _rfpis_rename(_t text,_old text,_new text) returns void as $$
begin
  if exists (select 1 from information_schema.columns where table_name=_t and column_name=_old)
     and not exists (select 1 from information_schema.columns where table_name=_t and column_name=_new) then
    execute format('alter table %I rename column %I to %I', _t, _old, _new);
  end if;
end $$ language plpgsql;

select _rfpis_rename('donor_intel', 'active_route_status', 'donor_active_route_status');
select _rfpis_rename('donor_intel', 'agriculture_food_security_fit', 'donor_agriculture_food_security_fit');
select _rfpis_rename('donor_intel', 'aliases', 'donor_aliases');
select _rfpis_rename('donor_intel', 'application_deadlines', 'donor_application_deadlines');
select _rfpis_rename('donor_intel', 'application_process', 'donor_application_process');
select _rfpis_rename('donor_intel', 'bank_details_required', 'donor_bank_details_required');
select _rfpis_rename('donor_intel', 'budget_narrative_required', 'donor_budget_narrative_required');
select _rfpis_rename('donor_intel', 'climate_environment_fit', 'donor_climate_environment_fit');
select _rfpis_rename('donor_intel', 'concept_note_required', 'donor_concept_note_required');
select _rfpis_rename('donor_intel', 'contact_emails', 'donor_contact_emails');
select _rfpis_rename('donor_intel', 'contact_linkedin_urls', 'donor_contact_linkedin_urls');
select _rfpis_rename('donor_intel', 'contact_persons', 'donor_contact_persons');
select _rfpis_rename('donor_intel', 'contact_phones', 'donor_contact_phones');
select _rfpis_rename('donor_intel', 'current_awards', 'donor_current_awards');
select _rfpis_rename('donor_intel', 'cvs_key_personnel_required', 'donor_cvs_key_personnel_required');
select _rfpis_rename('donor_intel', 'data_management_plan_required', 'donor_data_management_plan_required');
select _rfpis_rename('donor_intel', 'detailed_budget_required', 'donor_detailed_budget_required');
select _rfpis_rename('donor_intel', 'digital_health_data_ai_fit', 'donor_digital_health_data_ai_fit');
select _rfpis_rename('donor_intel', 'due_diligence_questionnaire_required', 'donor_due_diligence_questionnaire_required');
select _rfpis_rename('donor_intel', 'economic_development_fit', 'donor_economic_development_fit');
select _rfpis_rename('donor_intel', 'education_fit', 'donor_education_fit');
select _rfpis_rename('donor_intel', 'eligibility_notes', 'donor_eligibility_notes');
select _rfpis_rename('donor_intel', 'environmental_safeguard_required', 'donor_environmental_safeguard_required');
select _rfpis_rename('donor_intel', 'ethics_irb_approval_required', 'donor_ethics_irb_approval_required');
select _rfpis_rename('donor_intel', 'evidence_summary', 'donor_evidence_summary');
select _rfpis_rename('donor_intel', 'founded', 'donor_founded');
select _rfpis_rename('donor_intel', 'full_technical_proposal_required', 'donor_full_technical_proposal_required');
select _rfpis_rename('donor_intel', 'funded_geographies', 'donor_funded_geographies');
select _rfpis_rename('donor_intel', 'funding_cycle', 'donor_funding_cycle');
select _rfpis_rename('donor_intel', 'funding_mechanism', 'donor_funding_mechanism');
select _rfpis_rename('donor_intel', 'funding_platform_url', 'donor_funding_platform_url');
select _rfpis_rename('donor_intel', 'funding_programs', 'donor_funding_programs');
select _rfpis_rename('donor_intel', 'funding_tiers_json', 'donor_funding_tiers_json');
select _rfpis_rename('donor_intel', 'gaps_risks', 'donor_gaps_risks');
select _rfpis_rename('donor_intel', 'gender_inclusion_plan_required', 'donor_gender_inclusion_plan_required');
select _rfpis_rename('donor_intel', 'general_email', 'donor_general_email');
select _rfpis_rename('donor_intel', 'governance_equity_rights_fit', 'donor_governance_equity_rights_fit');
select _rfpis_rename('donor_intel', 'hiv_aids_fit', 'donor_hiv_aids_fit');
select _rfpis_rename('donor_intel', 'hq_address', 'donor_hq_address');
select _rfpis_rename('donor_intel', 'hq_country', 'donor_hq_country');
select _rfpis_rename('donor_intel', 'hss_fit', 'donor_hss_fit');
select _rfpis_rename('donor_intel', 'immunization_vaccines_fit', 'donor_immunization_vaccines_fit');
select _rfpis_rename('donor_intel', 'in_scope', 'donor_in_scope');
select _rfpis_rename('donor_intel', 'independent_entity_required', 'donor_independent_entity_required');
select _rfpis_rename('donor_intel', 'infectious_diseases_fit', 'donor_infectious_diseases_fit');
select _rfpis_rename('donor_intel', 'invitation_solicited', 'donor_invitation_solicited');
select _rfpis_rename('donor_intel', 'is_dual_role_implementer', 'donor_is_dual_role_implementer');
select _rfpis_rename('donor_intel', 'letters_of_support_required', 'donor_letters_of_support_required');
select _rfpis_rename('donor_intel', 'logframe_results_framework_required', 'donor_logframe_results_framework_required');
select _rfpis_rename('donor_intel', 'main_phone', 'donor_main_phone');
select _rfpis_rename('donor_intel', 'malaria_fit', 'donor_malaria_fit');
select _rfpis_rename('donor_intel', 'mande_plan_required', 'donor_mande_plan_required');
select _rfpis_rename('donor_intel', 'max_request_pct_of_budget', 'donor_max_request_pct_of_budget');
select _rfpis_rename('donor_intel', 'mission', 'donor_mission');
select _rfpis_rename('donor_intel', 'mnch_fit', 'donor_mnch_fit');
select _rfpis_rename('donor_intel', 'ncds_fit', 'donor_ncds_fit');
select _rfpis_rename('donor_intel', 'nutrition_fit', 'donor_nutrition_fit');
select _rfpis_rename('donor_intel', 'online_portal_submission', 'donor_online_portal_submission');
select _rfpis_rename('donor_intel', 'open_call_unsolicited', 'donor_open_call_unsolicited');
select _rfpis_rename('donor_intel', 'opportunity_listing_urls', 'donor_opportunity_listing_urls');
select _rfpis_rename('donor_intel', 'other_profile_urls', 'donor_other_profile_urls');
select _rfpis_rename('donor_intel', 'out_of_scope', 'donor_out_of_scope');
select _rfpis_rename('donor_intel', 'parent_organization', 'donor_parent_organization');
select _rfpis_rename('donor_intel', 'past_awards', 'donor_past_awards');
select _rfpis_rename('donor_intel', 'past_projects_json', 'donor_past_projects_json');
select _rfpis_rename('donor_intel', 'prior_track_record_required', 'donor_prior_track_record_required');
select _rfpis_rename('donor_intel', 'procurement_plan_required', 'donor_procurement_plan_required');
select _rfpis_rename('donor_intel', 'projected_budget', 'donor_projected_budget');
select _rfpis_rename('donor_intel', 'projected_budget_period', 'donor_projected_budget_period');
select _rfpis_rename('donor_intel', 'recent_activity', 'donor_recent_activity');
select _rfpis_rename('donor_intel', 'recommended_approach', 'donor_recommended_approach');
select _rfpis_rename('donor_intel', 'references_required', 'donor_references_required');
select _rfpis_rename('donor_intel', 'registration_certificate_required', 'donor_registration_certificate_required');
select _rfpis_rename('donor_intel', 'reporting_requirements', 'donor_reporting_requirements');
select _rfpis_rename('donor_intel', 'required_partner_country', 'donor_required_partner_country');
select _rfpis_rename('donor_intel', 'required_partner_type', 'donor_required_partner_type');
select _rfpis_rename('donor_intel', 'risk_management_plan_required', 'donor_risk_management_plan_required');
select _rfpis_rename('donor_intel', 'selection_criteria', 'donor_selection_criteria');
select _rfpis_rename('donor_intel', 'source_urls', 'donor_source_urls');
select _rfpis_rename('donor_intel', 'srhr_family_planning_fit', 'donor_srhr_family_planning_fit');
select _rfpis_rename('donor_intel', 'strategic_fit_notes', 'donor_strategic_fit_notes');
select _rfpis_rename('donor_intel', 'strategic_priorities', 'donor_strategic_priorities');
select _rfpis_rename('donor_intel', 'strategy_url', 'donor_strategy_url');
select _rfpis_rename('donor_intel', 'summary_description', 'donor_summary_description');
select _rfpis_rename('donor_intel', 'sustainability_exit_plan_required', 'donor_sustainability_exit_plan_required');
select _rfpis_rename('donor_intel', 'tb_fit', 'donor_tb_fit');
select _rfpis_rename('donor_intel', 'theory_of_change_required', 'donor_theory_of_change_required');
select _rfpis_rename('donor_intel', 'total_annual_funding_global', 'donor_total_annual_funding_global');
select _rfpis_rename('donor_intel', 'total_awards', 'donor_total_awards');
select _rfpis_rename('donor_intel', 'total_funding_to_date', 'donor_total_funding_to_date');
select _rfpis_rename('donor_intel', 'two_stage_application', 'donor_two_stage_application');
select _rfpis_rename('donor_intel', 'verification_caveats', 'donor_verification_caveats');
select _rfpis_rename('donor_intel', 'verification_level', 'donor_verification_level');
select _rfpis_rename('donor_intel', 'vision', 'donor_vision');
select _rfpis_rename('donor_intel', 'website', 'donor_website');
select _rfpis_rename('donor_intel', 'workplan_timeline_required', 'donor_workplan_timeline_required');

drop function _rfpis_rename(text, text, text);
