-- 092 — An explicit CO-FINANCING requirement on donor intel (owner 2026-08-10).
--
-- MUST-5 needs to know one thing to score co-financing: does this funder require the
-- applicant to commit its OWN funds alongside the award? Until now that had to be
-- inferred from three differently-named columns:
--
--   donor_cost_sharing_match_required      "Cost sharing match required"
--   donor_state_party_cofinancing_required "Government / counterpart co-financing required"
--   donor_min_cofinancing_secured_pct      a numeric threshold
--
-- None of them is called "co-financing required", so the donor form had no field a
-- researcher would recognise as the question, and the matching had to guess across three.
-- This column is the plainly-named answer, using the SAME tri-state as every other
-- requirement on that form: 'yes' (Required) | 'no' (Not required) | 'not_sure'.
--
-- The three columns above are KEPT and still activate the component, so no curated record
-- loses its meaning; this one takes precedence when it is answered.
--
-- Pre-financing already has its own column (donor_prefinance_required), converted to the
-- same tri-state in the same release. Co-financing and pre-financing are DIFFERENT
-- requirements — committing your own funds vs carrying the cost up front — and are scored
-- against two different org capabilities.
--
-- Idempotent: safe to re-run.
ALTER TABLE donor_intel
    ADD COLUMN IF NOT EXISTS donor_cofinancing_required text;

COMMENT ON COLUMN donor_intel.donor_cofinancing_required IS
    'Does this funder require the applicant to co-finance (commit its own funds alongside '
    'the award) as a condition of eligibility? yes | no | not_sure. NULL/not_sure leaves '
    'the MUST-5 co-financing component unscored and out of the denominator. Distinct from '
    'donor_prefinance_required, which asks whether the applicant must fund activities up '
    'front and be reimbursed later.';

-- Keep the vocabulary to the tri-state the form writes. NOT VALID so existing rows are
-- never rejected; new/updated rows are checked.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'donor_cofinancing_required_tristate'
    ) THEN
        ALTER TABLE donor_intel
            ADD CONSTRAINT donor_cofinancing_required_tristate
            CHECK (donor_cofinancing_required IS NULL
                   OR donor_cofinancing_required IN ('yes', 'no', 'not_sure'))
            NOT VALID;
    END IF;
END $$;

-- SEED from the legacy columns so curated research is not silently lost. Only where the
-- new column is still NULL, and only from an explicit answer — a blank legacy column says
-- nothing and must stay saying nothing.
UPDATE donor_intel
   SET donor_cofinancing_required = 'yes'
 WHERE donor_cofinancing_required IS NULL
   AND (lower(coalesce(donor_cost_sharing_match_required, '')) IN ('yes', 'true', 'required')
     OR lower(coalesce(donor_state_party_cofinancing_required, '')) IN ('yes', 'true', 'required')
     OR coalesce(nullif(regexp_replace(coalesce(donor_min_cofinancing_secured_pct::text, ''),
                                       '[^0-9.]', '', 'g'), '')::numeric, 0) > 0);

UPDATE donor_intel
   SET donor_cofinancing_required = 'no'
 WHERE donor_cofinancing_required IS NULL
   AND lower(coalesce(donor_cost_sharing_match_required, '')) IN ('no', 'false', 'not required');
