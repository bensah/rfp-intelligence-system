-- Migration 051 — store the LLM-extracted structured compliance/requirement flags
-- (core.llm_synthesis `compliance_flags`, incl. the MUST-1/MUST-2/MUST-3 call-side
-- signals: requires_pi, pi_country_scope, entity_type_required, registration_region,
-- prior_beneficiary_rule, experience_required, …) as JSON text on the RFP row, so the
-- Review's live re-derivation can re-merge them (call-detected signals show
-- consistently in view/edit, not just at scan time). Idempotent.
alter table rfp_submissions add column if not exists compliance_flags text;
