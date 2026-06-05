-- Migration 007 — same merge-on-sync pattern as meeting_logs:
-- give engagement_logs and narrative_logs a stable external_id so a
-- repeat sync UPDATES the existing row instead of appending a duplicate.

alter table engagement_logs
    add column if not exists external_id text,
    add column if not exists source text default 'app';

create index if not exists engagement_logs_external_id_idx
    on engagement_logs(external_id);

alter table narrative_logs
    add column if not exists external_id text,
    add column if not exists source text default 'app';

create index if not exists narrative_logs_external_id_idx
    on narrative_logs(external_id);
