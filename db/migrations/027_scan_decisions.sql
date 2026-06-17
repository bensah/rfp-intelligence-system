-- Migration 027 - scan_decisions: labeled-data capture for the learning pipeline
-- (ML Phase 1). Every row is one training signal:
--   * system_reject   — the scan gate dropped a candidate (label = reason
--                        category: not-an-rfp / type / geo / country / theme /
--                        deadline / language / feasibility). Usually no rfp_uid
--                        (rejected candidates aren't inserted).
--   * human_decision  — a reviewer set Proceed / Park / Decline on a record.
--   * feedback        — a reviewer flagged a record good / bad (👍 / 👎).
-- Features are captured inline (+ a jsonb `features` bag for extras) so a later
-- phase can train a classifier without re-crawling. Append-only.

create table if not exists scan_decisions (
    id                  uuid primary key default gen_random_uuid(),
    created_at          timestamptz not null default now(),
    event_type          text not null,            -- system_reject | human_decision | feedback
    label               text,                     -- reason-category | Proceed/Park/Decline | good/bad
    reason              text,                     -- full reason / note
    rfp_uid             text,                     -- rfp_submissions.uid when the row exists
    opportunity_title   text,
    opportunity_link    text,
    funding_agency      text,
    source              text,                     -- auto / migration / scan source
    geographic_scope    text,
    submission_deadline date,
    alignment_score     numeric,
    features            jsonb,
    decided_by          text                      -- user email (human events)
);
create index if not exists scan_decisions_event_idx on scan_decisions(event_type);
create index if not exists scan_decisions_link_idx  on scan_decisions(opportunity_link);
create index if not exists scan_decisions_uid_idx   on scan_decisions(rfp_uid);

-- RLS: match the permissive baseline used by migration 023 (anon/app role can
-- read+write; tighten later if needed). Re-runnable.
alter table scan_decisions enable row level security;
drop policy if exists scan_decisions_rfpis_baseline on scan_decisions;
create policy scan_decisions_rfpis_baseline on scan_decisions
    for all using (true) with check (true);
