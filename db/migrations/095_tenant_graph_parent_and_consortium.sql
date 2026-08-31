-- Migration 095 — TENANT GRAPH Phase 1: parent link + consortium-scoring consent.
-- =========================================================================
-- Foundation for proxy scoring (docs/TENANT_GRAPH_SCORING_DESIGN.md). Adds two
-- additive columns to `tenants`:
--
--   * parent_tenant_id  — self-referential link to a PARENT tenant. A child (a
--     country/team org) points at its parent org. Nullable; set later, in any
--     direction (assign an existing tenant to a parent whenever the parent exists).
--     Used at scoring time to let a child inherit the parent's TRANSFERABLE standing
--     (registration, authorized signatory, relationships, competitiveness) — never its
--     self-only criteria (strategy, capacity, co-financing, bid effort).
--
--   * share_for_consortium_scoring — opt-in consent for THIS tenant's whitelisted
--     profile fields to be read when it is named as a co-applicant (lead/sub) on
--     another tenant's RFP. Default FALSE: a co-applicant is "named but unresolved"
--     for scoring until it opts in. Parent<->child inheritance does NOT depend on this
--     flag — it is authorized by the ownership link.
--
-- Guardrails: a tenant cannot be its own parent (CHECK), and a parent chain cannot
-- form a CYCLE (trigger walks the chain; SECURITY DEFINER so the walk sees the whole
-- tree regardless of the caller's RLS). Depth is otherwise unrestricted — the scoring
-- resolver decides how far to consult.
--
-- Tenant CREATION + APPROVAL is unchanged: an admin's new tenant still lands
-- status='pending' for a super_user to approve (migration 082); this migration adds
-- no rows and no real tenant data. Parent linking is done later in the UI.
--
-- SAFE + ADDITIVE + RE-RUN-SAFE. Paste into the Supabase SQL editor and run.
-- =========================================================================

begin;

-- 1. Columns ---------------------------------------------------------------
alter table tenants
  add column if not exists parent_tenant_id uuid
    references tenants(id) on delete set null;

alter table tenants
  add column if not exists share_for_consortium_scoring boolean not null default false;

create index if not exists idx_tenants_parent on tenants(parent_tenant_id);

-- 2. No self-parenting -----------------------------------------------------
alter table tenants drop constraint if exists tenants_parent_not_self;
alter table tenants add constraint tenants_parent_not_self
  check (parent_tenant_id is null or parent_tenant_id <> id);

-- 3. No cycles (A->B->...->A) ----------------------------------------------
-- SECURITY DEFINER + a pinned search_path so the upward walk reads the full tenant
-- tree even when the writer is a tenant-scoped (RLS-limited) client.
create or replace function tenants_no_parent_cycle()
  returns trigger
  language plpgsql
  security definer
  set search_path = public
as $$
declare
  cur  uuid := new.parent_tenant_id;
  hops int  := 0;
begin
  while cur is not null loop
    if cur = new.id then
      raise exception 'parent_tenant_id would create a cycle for tenant %', new.id
        using errcode = 'check_violation';
    end if;
    select parent_tenant_id into cur from tenants where id = cur;
    hops := hops + 1;
    if hops > 100 then
      raise exception 'parent chain too deep (possible cycle) for tenant %', new.id
        using errcode = 'check_violation';
    end if;
  end loop;
  return new;
end;
$$;

drop trigger if exists trg_tenants_no_parent_cycle on tenants;
create trigger trg_tenants_no_parent_cycle
  before insert or update of parent_tenant_id on tenants
  for each row
  when (new.parent_tenant_id is not null)
  execute function tenants_no_parent_cycle();

commit;

-- =========================================================================
-- VERIFY (run after; all three should return rows / expected values):
--   select column_name, data_type, is_nullable, column_default
--     from information_schema.columns
--    where table_name = 'tenants'
--      and column_name in ('parent_tenant_id','share_for_consortium_scoring')
--    order by column_name;
--
--   select conname from pg_constraint where conname = 'tenants_parent_not_self';
--   select tgname  from pg_trigger    where tgname  = 'trg_tenants_no_parent_cycle';
-- =========================================================================
-- ROLLBACK:
--   begin;
--     drop trigger  if exists trg_tenants_no_parent_cycle on tenants;
--     drop function if exists tenants_no_parent_cycle();
--     alter table tenants drop constraint if exists tenants_parent_not_self;
--     drop index  if exists idx_tenants_parent;
--     alter table tenants drop column if exists share_for_consortium_scoring;
--     alter table tenants drop column if exists parent_tenant_id;
--   commit;
-- =========================================================================
