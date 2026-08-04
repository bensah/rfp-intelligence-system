-- Migration 011 — add `source` to applied_funding so the Excel sync can
-- safely delete rows that disappeared from the workbook without touching
-- rows that were added via the Admin > Data > Active Grants UI.

alter table applied_funding
    add column if not exists source text default 'app';

-- Backfill: every existing row right now was Excel-sourced (we hadn't
-- introduced the app-only path yet), so mark them as 'migration'. After
-- this runs the next sync can clean up stale rows.
update applied_funding
   set source = 'migration'
 where source is null or source = 'app';
