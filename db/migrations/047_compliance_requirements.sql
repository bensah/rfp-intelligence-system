-- 047 — LLM-extracted compliance / co-financing hard-gates on rfp_submissions.
-- compliance_requirements : a plain-text list (written by core.llm_synthesis from
--   the RFP body) of the cost-share / eligibility / registration / partner / audit
--   requirements the call imposes — surfaced to reviewers so a hidden hard-gate
--   never reaches a client near deadline. Complements the structured donor_intel
--   compliance fields that feed the MUST-5 score.
alter table rfp_submissions add column if not exists compliance_requirements text;
