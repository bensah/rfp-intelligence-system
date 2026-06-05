-- Migration 014 — add `is_resolved` to engagement_logs.
--
-- The Activity page's new "Pending Actions" tab lists open follow-ups
-- from BOTH meeting_logs and engagement_logs. meeting_logs has had an
-- `is_resolved` flag from day one; engagement_logs did not — every
-- engagement was a one-shot record with an `outcome` text field and no
-- way to mark "follow-up complete".
--
-- This migration brings parity: every engagement now has an
-- `is_resolved` flag defaulting to FALSE. The Pending Actions tab uses
-- the flag to filter to engagements still awaiting closure; users
-- toggle it from the engagements list the same way meeting actions get
-- toggled.

alter table engagement_logs
  add column if not exists is_resolved boolean not null default false;

-- Index for the Pending Actions query (filter on is_resolved=false).
create index if not exists engagement_logs_unresolved_idx
  on engagement_logs (is_resolved)
  where is_resolved = false;
