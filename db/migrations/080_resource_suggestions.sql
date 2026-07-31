-- Migration 080 — resource_suggestions: the Phase B propose→review→apply queue.
-- =========================================================================
-- The SHARED central resources donor_intel + donor_sources are developer-Super-only to
-- EDIT (Phase A / migration 079). Phase B lets any NON-developer PROPOSE a field-level
-- change that a developer-tenant Super User APPROVES → auto-applies, or REJECTS.
--
-- Enforcement posture (matches the project stance, migration 068 + db/supabase_client.py):
--   * APP-LAYER is primary — core/suggestions.py gates every privileged op on
--     permissions.is_developer_super and runs it through the RLS-bypassing service_client().
--   * RLS here is DEFENSE-IN-DEPTH: a client (get_client()) may only see/withdraw its OWN
--     pending rows in its OWN tenant, and can NEVER move a row to approved/applied/rejected.
--
-- tenant_id is NULLABLE on purpose: in SINGLE-TENANT mode (no JWT) there is no tenant, but a
-- non-super admin is still a "contributor" who proposes — such rows carry tenant_id = NULL
-- and are governed purely by the app-layer proposer filter (RLS is inert with no JWT).
--
-- SAFE + ADDITIVE + RE-RUN-SAFE.
-- =========================================================================

begin;

-- acting-user helpers — read the JWT claims. app_current_tenant_id() is normally created by
-- migration 068; app_current_jwt_sub() is new here. Both are redefined idempotently so 080 is
-- self-contained and does not fail with 42883 when an earlier migration didn't apply cleanly.
create or replace function app_current_tenant_id() returns uuid
language sql stable as $$
  select nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'tenant_id', '')::uuid
$$;

create or replace function app_current_jwt_sub() returns uuid
language sql stable as $$
  select nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'sub', '')::uuid
$$;

-- Auto-stamp trigger fn (also from 068) — used by the stamp_tenant_resource_suggestions
-- trigger below. Redefined here so 080 doesn't fail when 068 hasn't applied.
create or replace function app_stamp_tenant_id() returns trigger
language plpgsql as $$
begin
  if new.tenant_id is null then
    new.tenant_id := app_current_tenant_id();
  end if;
  return new;
end $$;

create table if not exists resource_suggestions (
  id                uuid primary key default gen_random_uuid(),

  -- which shared resource this proposal targets
  resource_type     text not null
                    check (resource_type in ('donor_intel','donor_sources')),
  -- identity of the target ROW; NULL == a create-new-row proposal.
  --   donor_intel   -> the canonical_key (text business key everything upserts on)
  --   donor_sources -> the uuid id (the .eq("id",...) update key), stored as text
  target_id         text,
  target_label      text,                               -- donor/source name at propose time

  -- structured, field-level proposal
  proposed_diff     jsonb not null,                     -- {column: proposed_value, ...}
  base_snapshot     jsonb not null default '{}'::jsonb, -- {column: value_at_propose_time}

  -- proposer identity (proposer's tenant; NULL in single-tenant mode)
  proposer_user_id  uuid not null,
  proposer_email    text not null,
  tenant_id         uuid references tenants(id) on delete cascade,
  rationale         text,

  -- lifecycle
  status            text not null default 'pending'
                    check (status in ('pending','approved','rejected','applied','withdrawn')),

  -- reviewer (developer super) decision
  reviewer_user_id  uuid,
  reviewer_email    text,
  review_note       text,
  decided_at        timestamptz,
  applied_at        timestamptz,
  -- the row the apply actually wrote (developer-set): for an ADD proposal this records
  -- the newly-created key WITHOUT mutating the immutable proposer-supplied target_id.
  applied_target_id text,

  created_at        timestamptz not null default now()
);

create index if not exists resource_suggestions_pending_idx
  on resource_suggestions (created_at desc)
  where status = 'pending';                             -- the dev inbox + pending_count()
create index if not exists resource_suggestions_mine_idx
  on resource_suggestions (proposer_user_id, created_at desc);   -- list_mine()
create index if not exists resource_suggestions_target_idx
  on resource_suggestions (resource_type, target_id);   -- collision / history lookups

-- ---------------------------------------------------------------------------
-- RLS — defense-in-depth. Developers bypass this via service_client() (app gate).
-- ---------------------------------------------------------------------------
alter table resource_suggestions enable row level security;

drop policy if exists resource_suggestions_sel on resource_suggestions;
drop policy if exists resource_suggestions_ins on resource_suggestions;
drop policy if exists resource_suggestions_upd on resource_suggestions;
drop policy if exists resource_suggestions_del on resource_suggestions;

-- SELECT: only your OWN rows in your OWN tenant.
create policy resource_suggestions_sel on resource_suggestions
  for select
  using (tenant_id is not distinct from app_current_tenant_id()
         and proposer_user_id = app_current_jwt_sub());

-- INSERT: only a PENDING proposal, AS yourself, in your OWN tenant.
create policy resource_suggestions_ins on resource_suggestions
  for insert
  with check (tenant_id is not distinct from app_current_tenant_id()
              and proposer_user_id = app_current_jwt_sub()
              and status = 'pending');

-- UPDATE (client path = WITHDRAW only): own pending row → may only stay pending or become
-- withdrawn. Clients can NEVER reach approved/applied/rejected — those come solely from the
-- developer path on service_client() (RLS-bypassing).
create policy resource_suggestions_upd on resource_suggestions
  for update
  using (tenant_id is not distinct from app_current_tenant_id()
         and proposer_user_id = app_current_jwt_sub()
         and status = 'pending')
  with check (tenant_id is not distinct from app_current_tenant_id()
              and proposer_user_id = app_current_jwt_sub()
              and status in ('pending','withdrawn'));

-- DELETE: own PENDING rows only (matches UPDATE) — a decided row's audit trail can't be
-- erased by the proposer. Belt-and-braces: the DELETE privilege is withheld from
-- anon/authenticated below anyway (the app only ever soft-withdraws via UPDATE).
create policy resource_suggestions_del on resource_suggestions
  for delete
  using (tenant_id is not distinct from app_current_tenant_id()
         and proposer_user_id = app_current_jwt_sub()
         and status = 'pending');

-- Content-freeze: once filed, the PROPOSER-supplied content is immutable — no UPDATE (by
-- anyone, client OR service path) may change what was proposed. This binds the developer's
-- approve decision to exactly the content that was reviewed (a proposer cannot swap
-- proposed_diff / target_id / base_snapshot / label after filing while status stays
-- 'pending'). Only the lifecycle columns (status, reviewer_*, review_note, decided_at,
-- applied_at, applied_target_id) may change. Neither the withdraw nor the approve/reject
-- path ever legitimately edits the frozen set, so this is transparent to both.
create or replace function freeze_resource_suggestion_content() returns trigger
language plpgsql as $$
begin
  new.resource_type    := old.resource_type;
  new.target_id        := old.target_id;
  new.target_label     := old.target_label;
  new.proposed_diff    := old.proposed_diff;
  new.base_snapshot    := old.base_snapshot;
  new.rationale        := old.rationale;
  new.proposer_user_id := old.proposer_user_id;
  new.proposer_email   := old.proposer_email;
  new.tenant_id        := old.tenant_id;
  new.created_at       := old.created_at;
  return new;
end $$;

drop trigger if exists freeze_resource_suggestion_content on resource_suggestions;
create trigger freeze_resource_suggestion_content
  before update on resource_suggestions
  for each row execute function freeze_resource_suggestion_content();

-- auto-stamp tenant_id from the caller's JWT (068 idiom) when an insert omits it.
drop trigger if exists stamp_tenant_resource_suggestions on resource_suggestions;
create trigger stamp_tenant_resource_suggestions
  before insert on resource_suggestions
  for each row execute function app_stamp_tenant_id();

-- Client roles may read/insert/update (propose + withdraw) but NOT hard-delete — the
-- review audit trail is append-then-soft-transition only. service_role (developer path)
-- keeps full access for maintenance.
grant select, insert, update on resource_suggestions to anon, authenticated;
grant select, insert, update, delete on resource_suggestions to service_role;

commit;

-- =========================================================================
-- ROLLBACK:
--   begin;
--     drop table if exists resource_suggestions;
--     drop function if exists app_current_jwt_sub();
--   commit;
-- =========================================================================
