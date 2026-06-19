-- Migration 033 - rfp_seen ledger (permanent de-dup tombstones).
--
-- A PERMANENT record of every RFP signature that has EVER entered the system, so
-- a previously-found opportunity can never silently re-enter the pipeline — even
-- after its live rfp_submissions row is DELETED.
--
-- Why this is needed: declined / parked / proceeded RFPs still LIVE in
-- rfp_submissions, so the live deduplicator (core/deduplicator.py, which pulls
-- is_duplicate=false rows) already catches a re-scan of them. But every delete in
-- the app is a HARD delete — once a row is removed, its signature vanishes from
-- rfp_submissions and the scanner would re-insert it as brand new. This ledger is
-- the backstop: tombstones are written at ingest time (scan / manual / Excel) and
-- on a one-time backfill below, and are NEVER deleted, so the set only grows.
--
-- It stores the SAME minimal projection core.deduplicator.find_duplicates reads,
-- so suppression reuses that matcher verbatim (no second normalisation).
-- Idempotent; safe to re-run (the backfill is ON CONFLICT DO NOTHING).

create table if not exists rfp_seen (
    id                  bigserial primary key,
    uid                 text unique,
    opportunity_id      text,
    opportunity_title   text,
    opportunity_link    text,
    funding_agency      text,
    submission_deadline text,
    estimated_value     numeric,
    reason              text,            -- ingested | manual | migration | backfill | deleted
    created_at          timestamptz default now()
);

create index if not exists idx_rfp_seen_oppid on rfp_seen (opportunity_id);
create index if not exists idx_rfp_seen_link  on rfp_seen (opportunity_link);

-- One-time backfill: remember everything currently in rfp_submissions (every
-- source, every decision, duplicates included). Tombstones are never deleted, so
-- this set only grows from here.
insert into rfp_seen (uid, opportunity_id, opportunity_title, opportunity_link,
                      funding_agency, submission_deadline, estimated_value, reason)
select uid, opportunity_id, opportunity_title, opportunity_link,
       funding_agency, submission_deadline::text, estimated_value, 'backfill'
from rfp_submissions
where uid is not null
on conflict (uid) do nothing;
