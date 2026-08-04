-- Migration 087 — persist HUMAN component verdicts on the 9 criteria.
--
-- The Review screen lets a reviewer score each criterion's COMPONENT sub-factors (0/0.5/1),
-- but those numbers were never stored: _item_score_editor rolled them into ONE criterion
-- label and Save wrote only that label. So a reviewer who corrected a component watched it
-- revert on the next render, because the component panel re-derives from org profile /
-- donor intel / call text every time.
--
-- This column stores the reviewer's verdicts so they WIN over the derivation:
--     {"cofinancing": {"authorized_signatory": 1}, "bid_effort": {"bid_time": 1}}
--   criterion key -> component key -> score (0 / 0.5 / 1)
--
-- criteria_derive.factor_breakdown(..., overrides=...) merges them on top of the derived
-- items (see apply_component_overrides), stamping each as _override so the UI can show that
-- a human decided it. Only components the reviewer actually touched are written, so the
-- derivation still drives everything else and keeps improving as the source data improves.
--
-- Idempotent.

alter table rfp_submissions
    add column if not exists criteria_component_overrides jsonb not null default '{}'::jsonb;

comment on column rfp_submissions.criteria_component_overrides is
    'Human component verdicts per criterion {criterion: {component: score}}; merged over the '
    'derived factor breakdown so a reviewer''s answer beats the inference.';

notify pgrst, 'reload schema';
