-- Migration 028 - rename the 9 MUST/PREFER criterion columns to the self-
-- explanatory bid/no-bid keys (matches the MS Form column keys), plus
-- decision_rationale -> decision_note.
--
-- RENAME ONLY — the stored values are untouched (existing True/Partial/False
-- responses stay as-is; new MS-Form rich labels go into the SAME columns).
-- Normalisation to the 2/1/0 scale happens in code (core.scorer.criterion_score),
-- not here, so no data is rewritten. Column positions/types are unchanged.
--
--   must_1_govt_alignment      -> qualification
--   must_2_strategic_fit       -> strategic_fit
--   must_3_implementable       -> capacity
--   must_4_compliant           -> geographic_fit
--   must_5_resourcing          -> cofinancing
--   prefer_6_funding_quality   -> funding_quality
--   prefer_7_monitorable       -> funder_relationship
--   prefer_8_partnership       -> competitiveness
--   prefer_9_scale             -> bid_effort
--   decision_rationale         -> decision_note
--
-- Idempotent + re-runnable: each rename fires only when the old column still
-- exists and the new one doesn't yet.

do $$
declare
    r record;
begin
    for r in
        select * from (values
            ('must_1_govt_alignment',    'qualification'),
            ('must_2_strategic_fit',     'strategic_fit'),
            ('must_3_implementable',     'capacity'),
            ('must_4_compliant',         'geographic_fit'),
            ('must_5_resourcing',        'cofinancing'),
            ('prefer_6_funding_quality', 'funding_quality'),
            ('prefer_7_monitorable',     'funder_relationship'),
            ('prefer_8_partnership',     'competitiveness'),
            ('prefer_9_scale',           'bid_effort'),
            ('decision_rationale',       'decision_note')
        ) as t(old_name, new_name)
    loop
        if exists (
            select 1 from information_schema.columns
            where table_name = 'rfp_submissions' and column_name = r.old_name
        ) and not exists (
            select 1 from information_schema.columns
            where table_name = 'rfp_submissions' and column_name = r.new_name
        ) then
            execute format('alter table rfp_submissions rename column %I to %I',
                           r.old_name, r.new_name);
            raise notice 'renamed %.% -> %', 'rfp_submissions', r.old_name, r.new_name;
        end if;
    end loop;
end $$;
