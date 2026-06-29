-- Migration 050 — drop the redundant donor_intel.eligible_entity_types column
-- added in 049. Applicant LEGAL-TYPE eligibility is already captured by the
-- existing ngo_eligible / for_profit_eligible flags, which MUST-1 item A reuses;
-- a parallel list duplicated them. Idempotent.
alter table donor_intel drop column if exists eligible_entity_types;
