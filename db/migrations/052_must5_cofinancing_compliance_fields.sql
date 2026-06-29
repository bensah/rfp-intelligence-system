-- Migration 052 — donor_intel: NEW MUST-5 (Cofinancing & compliance) fields.
-- The rest of the MUST-5 hard gates already exist as columns (audited_financials_required,
-- audit_report_required, sam_uei_registration_required, tax_exempt_status_required,
-- safeguarding_policy_required, authorized_signatory_signoff_required, welcome_registration_required,
-- partner_mou_required, govt_endorsement_letter_required, local_board_required, partnership_mandatory,
-- cost_sharing_match_required, min_cofinancing_secured_pct, prefinance_required) and the
-- funding-platform URL reuses the EXISTING submission_portal_url (matched against the org's
-- donor_registrations) — so only these two are added. All TEXT ("yes"/null flag convention).
-- Idempotent.

-- Government MOU required (host-government MOU the org must hold). "yes"/null flag.
alter table donor_intel add column if not exists govt_mou_required text;

-- Funding-platform / portal registration the applicant must hold BEFORE applying
-- (e.g. CIHR ResearchNet, grants.gov SAM). Matched against the existing
-- submission_portal_url. "yes"/null flag.
alter table donor_intel add column if not exists funding_platform_registration_required text;
