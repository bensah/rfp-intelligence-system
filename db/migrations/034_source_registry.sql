-- Migration 034 — source_registry: a learning log of every HOST the scanner
-- meets, classified aggregator vs primary (vs blog / listing / unknown), so we
-- (a) never publish aggregator/blog/listing pages, (b) route known aggregators
-- through the title→primary-source resolver, and (c) let a human confirm new
-- hosts once, after which the classification is authoritative.
--
-- One row per host (normalized netloc, no leading www). The scanner records
-- encounters (best-effort, batched at end of scan); a human reviews pending rows
-- (scripts/review_sources.py or the admin Verify tab) and sets status=confirmed.
--   classification ∈ aggregator | primary | blog | listing | unknown
--   status         ∈ pending    | confirmed            (confirmed = human-set,
--                                                        overrides the detector)

create table if not exists source_registry (
    host            text primary key,
    classification  text not null default 'unknown',
    status          text not null default 'pending',
    detected_as     text,                      -- the detector's original guess
    sample_url      text,
    sample_title    text,
    hits            integer not null default 1,
    first_seen      timestamptz not null default now(),
    last_seen       timestamptz not null default now(),
    verified_by     text,
    verified_at     timestamptz,
    notes           text
);
create index if not exists source_registry_class_idx  on source_registry(classification);
create index if not exists source_registry_status_idx on source_registry(status);

-- RLS: permissive baseline (matches migration 023 / 027).
alter table source_registry enable row level security;
drop policy if exists source_registry_rfpis_baseline on source_registry;
create policy source_registry_rfpis_baseline on source_registry
    for all using (true) with check (true);
