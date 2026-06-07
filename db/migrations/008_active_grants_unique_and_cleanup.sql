-- Migration 008 — fix duplicate accumulation across log tables.
--
-- Root causes:
--   * active_grants had no UNIQUE constraint on grant_id, so every sync
--     appended fresh duplicates AND the new `upsert(on_conflict='grant_id')`
--     fails with "no unique or exclusion constraint matching" (42P10).
--   * meeting_logs / engagement_logs / narrative_logs rows from syncs that
--     ran BEFORE migrations 006/007 have NULL external_id, so the merge
--     logic can't recognise them on the next sync and either creates
--     more duplicates or leaves the old ones stranded.
--
-- This migration:
--   1. Dedups active_grants by grant_id (keeps the most-recently-updated row).
--   2. Adds UNIQUE constraint so the upsert can work.
--   3. Wipes all migration-origin rows (or NULL-external_id rows) from
--      meeting/engagement/narrative tables so the next Excel sync rebuilds
--      them cleanly with proper external_ids.
--
-- After running this migration: Admin → Settings → 🔄 Sync now.

-- -------------------------------------------------------------------------
-- 1. Dedup active_grants by grant_id (keep newest by updated_at, then id).
-- -------------------------------------------------------------------------
delete from active_grants a
using active_grants b
where a.grant_id = b.grant_id
  and (
        a.updated_at < b.updated_at
        or (a.updated_at = b.updated_at and a.id < b.id)
      );

-- 2. Add the missing UNIQUE constraint so upsert(on_conflict='grant_id') works.
--    Idempotent guard via pg_constraint so re-runs don't error with
--    "relation already exists" (original migration lacked this — re-running
--    after the constraint was already added would crash mid-batch).
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'active_grants_grant_id_key'
  ) then
    alter table active_grants
      add constraint active_grants_grant_id_key unique (grant_id);
  end if;
end $$;

-- -------------------------------------------------------------------------
-- 3. Wipe migration-origin rows from log tables. App-added rows (source='app')
--    are PRESERVED. Rows with NULL source/external_id are pre-006/007 rows
--    that we can't reliably re-key — they get wiped too.
-- -------------------------------------------------------------------------
delete from meeting_logs
where source is null
   or source = 'migration'
   or external_id is null;

delete from engagement_logs
where source is null
   or source = 'migration'
   or external_id is null;

delete from narrative_logs
where source is null
   or source = 'migration'
   or external_id is null;
