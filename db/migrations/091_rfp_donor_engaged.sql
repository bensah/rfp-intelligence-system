-- 091 — "Donor already engaged" on the opportunity (action #10).
--
-- A HUMAN answer, per opportunity. The system cannot know in real time whether anyone
-- has approached this funder about THIS call — a meeting, a concept note or an EOI
-- leaves no trace a crawler can see. PREFER-7 previously INFERRED it from the per-donor
-- org_engaged_donors list, which answers a different question ("have we ever engaged
-- this funder?") and is empty in practice.
--
--   yes     — we have engaged this funder about this opportunity
--   partial — contact made via a third party on our behalf
--   no      — no contact about this opportunity
--   NULL    — nobody has answered; the tier is EXCLUDED from PREFER-7 rather than
--             scored 0. The system is not entitled to guess.
--
-- Idempotent: safe to re-run.
ALTER TABLE rfp_submissions
    ADD COLUMN IF NOT EXISTS donor_engaged text;

COMMENT ON COLUMN rfp_submissions.donor_engaged IS
    'Reviewer-set: has anyone engaged this funder about THIS opportunity? '
    'yes | partial (via a third party on our behalf) | no. NULL = unanswered, '
    'excluded from PREFER-7 rather than scored.';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'rfp_submissions_donor_engaged_vals'
    ) THEN
        ALTER TABLE rfp_submissions
            ADD CONSTRAINT rfp_submissions_donor_engaged_vals
            CHECK (donor_engaged IS NULL
                   OR donor_engaged IN ('yes', 'partial', 'no'))
            NOT VALID;
    END IF;
END $$;
