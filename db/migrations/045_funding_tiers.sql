-- 045: Multi-stage / tiered funding amounts (additive, SAFE to run now).
--
-- Some calls fund in stages with different ceilings — e.g. Grand Challenges Nexa:
--   "Proof of Concept ... up to $200,000 USD" + "Transition to Scale ... up to $X".
-- A single grant_amount can't represent that, so we add a structured tiers field.
--
-- Convention (keeps the existing amount fields meaningful):
--   funding_tiers  — jsonb array, one object per stage:
--       [{"stage":"Proof of Concept","amount_min":null,"amount_max":200000,
--         "currency":"USD","notes":"per innovation"}, ...]
--   grant_amount   — headline figure (the MAX amount_max across tiers), for sorting/display
--   award_floor    — overall MIN across tiers; award_ceiling — overall MAX across tiers
--
-- Populated by the LLM extraction pass (multi-amount + stage labels are beyond
-- regex). Idempotent.

alter table extracted_solicitations
    add column if not exists funding_tiers jsonb default '[]'::jsonb;

-- Mirror on the per-tenant Screened table so the same tiers carry through.
alter table rfp_submissions
    add column if not exists funding_tiers jsonb default '[]'::jsonb;
