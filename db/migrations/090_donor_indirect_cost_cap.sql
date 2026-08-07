-- 090 — Indirect-cost policy on donor intel (action #7).
--
-- The maximum indirect / overhead / administrative rate this funder reimburses, as a
-- PERCENTAGE of total project cost. Matched in MUST-5 against the org's own rate
-- (org_profile.org_indirect_cost_rate). Read CALL-FIRST: a specific call can set its
-- own cap and then the call governs; this column is the funder's standing guideline
-- and only fills what the call omits.
--
-- The pre-existing donor_indirect_cost_disallowed boolean is kept and treated as the
-- 0% case — which is what finally gives that column (0 of 190 donors filled) a meaning.
--
-- Idempotent: safe to re-run.
ALTER TABLE donor_intel
    ADD COLUMN IF NOT EXISTS donor_indirect_cost_max_pct numeric;

COMMENT ON COLUMN donor_intel.donor_indirect_cost_max_pct IS
    'Max indirect/overhead rate this funder reimburses, as a % of total project cost '
    '(0-100). NULL = not published; the MUST-5 indirect-cost component then stays '
    '"Not sure" and is excluded from the denominator. donor_indirect_cost_disallowed '
    '= true is equivalent to 0.';

-- Guard against nonsense percentages without failing the migration on existing data.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'donor_indirect_cost_max_pct_range'
    ) THEN
        ALTER TABLE donor_intel
            ADD CONSTRAINT donor_indirect_cost_max_pct_range
            CHECK (donor_indirect_cost_max_pct IS NULL
                   OR (donor_indirect_cost_max_pct >= 0
                       AND donor_indirect_cost_max_pct <= 100))
            NOT VALID;
    END IF;
END $$;
