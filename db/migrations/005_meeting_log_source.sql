-- Migration 005 — track origin of meeting_logs rows so re-syncs don't
-- duplicate migrated rows but preserve notes added via the app.

alter table meeting_logs
    add column if not exists source text not null default 'app';

create index if not exists meeting_logs_source_idx on meeting_logs(source);
