-- Migration 088 — stop re-scans rewriting history, and surface merge conflicts.
--
-- TWO problems with the rescan merge (core/scan_pipeline._build_merge_payload):
--
-- 1. `search_date` — the date the opportunity was FIRST discovered — was overwritten with
--    now() on every rescan ("always refresh search_date … for last-seen diagnostics"). That
--    destroys the discovery timeline: rows found months ago all showed today's date, so
--    "when did we find this?" and every search→submission cycle-time metric were wrong.
--    search_date must be IMMUTABLE; last-seen belongs in its own column.
--
-- 2. When a rescan's value CONTRADICTS a populated cell, the merge silently discards it
--    (it only fills blanks — correct, since human/earlier data must win). But the user never
--    learns the funder changed a deadline or an amount. Those contradictions are now
--    recorded for review instead of dropped.
--
--     merge_conflicts = {"call_submission_deadline": {"kept": "2026-08-12",
--                                                     "incoming": "2026-09-30",
--                                                     "seen_at": "2026-08-04T…"}}
--
-- Excel migration is deliberately UNAFFECTED: that path overwrites by design, because a
-- human typed the workbook and their intent should win over machine-scraped values.
--
-- Idempotent.

alter table rfp_submissions
    add column if not exists last_seen_at   timestamptz,
    add column if not exists merge_conflicts jsonb not null default '{}'::jsonb;

comment on column rfp_submissions.search_date is
    'IMMUTABLE: when this opportunity was FIRST discovered. Never rewritten by a rescan — '
    'see last_seen_at for the most recent sighting.';
comment on column rfp_submissions.last_seen_at is
    'Most recent rescan that matched this row (the old, wrong use of search_date).';
comment on column rfp_submissions.merge_conflicts is
    'Rescan values that CONTRADICT a populated cell: {field: {kept, incoming, seen_at}}. '
    'The stored value is kept; this flags the difference for human review.';

-- Seed last_seen_at so existing rows have a sensible value from day one.
update rfp_submissions
   set last_seen_at = coalesce(last_seen_at, updated_at, search_date)
 where last_seen_at is null;

notify pgrst, 'reload schema';
