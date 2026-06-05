-- Migration 006 — add stable external_id to meeting_logs so Excel re-syncs
-- can UPDATE Excel-managed fields (date, donor, issues, actions, owner,
-- deadline) while PRESERVING app-managed fields (is_resolved, rfp_uid,
-- created_by). The external_id is a hash of (meeting_date, donor_title)
-- computed by the migration script.

alter table meeting_logs
    add column if not exists external_id text;

create index if not exists meeting_logs_external_id_idx
    on meeting_logs(external_id);
