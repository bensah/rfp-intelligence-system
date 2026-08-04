-- Migration 086 — rename table active_grants -> applied_funding (+ column grant_id -> funding_id)
--
-- NOTE: this migration intentionally still NAMES the legacy `active_grants*` objects — it is
-- the transition step, and its guards must match the live legacy names to rename them. Every
-- other .sql now uses applied_funding / funding_id.
--
-- The table holds SUBMITTED / applied funding records (grant applications and their
-- reporting status), so "applied_funding" reflects the data far better than the legacy
-- "active_grants". The table rename was applied to the live DB via the Supabase UI; this
-- migration makes it reproducible for a from-scratch build and TIDIES the leftover object
-- names (Postgres does not rename indexes/constraints/policies/triggers when a table is
-- renamed, and the UI rename only caught a couple of them).
--
-- Fully IDEMPOTENT and GUARDED: every step is a no-op if it was already applied, so this
-- is safe to run against the live DB (where the table rename is already done) or a fresh
-- build (where the old migrations created `active_grants`). Column names are UNCHANGED.

-- 1) Table (no-op on live; renames on a fresh replay where 067 created active_grants).
do $$
begin
  if exists (select 1 from pg_class where relname = 'active_grants' and relkind = 'r') then
    alter table active_grants rename to applied_funding;
  end if;
end $$;

-- 1b) Column grant_id -> funding_id: a row here is a funding APPLICATION / opportunity, not
--     an active grant the org is running. Renaming it keeps that distinction clear (and
--     leaves room to add an actual active-grants table later). The unique constraint follows
--     the column automatically. No-op if already renamed.
do $$
begin
  if exists (select 1 from information_schema.columns
             where table_name = 'applied_funding' and column_name = 'grant_id') then
    alter table applied_funding rename column grant_id to funding_id;
  end if;
end $$;

-- 2) Indexes (ALTER INDEX IF EXISTS is a clean no-op when the name is already updated).
alter index if exists active_grants_form_id_idx rename to applied_funding_form_id_idx;
alter index if exists active_grants_due_idx     rename to applied_funding_due_idx;
alter index if exists idx_active_grants_tenant  rename to idx_applied_funding_tenant;

-- 3) Constraints (renaming a unique/pk constraint also renames its backing index).
do $$
begin
  if exists (select 1 from pg_constraint where conname = 'active_grants_grant_id_key'
             and conrelid = 'applied_funding'::regclass) then
    alter table applied_funding rename constraint active_grants_grant_id_key
      to applied_funding_funding_id_key;
  end if;
  if exists (select 1 from pg_constraint where conname = 'active_grants_tenant_id_fkey'
             and conrelid = 'applied_funding'::regclass) then
    alter table applied_funding rename constraint active_grants_tenant_id_fkey
      to applied_funding_tenant_id_fkey;
  end if;
end $$;

-- 4) RLS policies (bodies reference app_current_tenant_id()/tenant_id, not the name — the
--    policies themselves moved with the table; only their NAMES need tidying).
do $$
begin
  if exists (select 1 from pg_policies where tablename = 'applied_funding' and policyname = 'active_grants_ins') then
    alter policy active_grants_ins on applied_funding rename to applied_funding_ins;
  end if;
  if exists (select 1 from pg_policies where tablename = 'applied_funding' and policyname = 'active_grants_upd') then
    alter policy active_grants_upd on applied_funding rename to applied_funding_upd;
  end if;
  if exists (select 1 from pg_policies where tablename = 'applied_funding' and policyname = 'active_grants_del') then
    alter policy active_grants_del on applied_funding rename to applied_funding_del;
  end if;
end $$;

-- 5) Triggers (the trigger FUNCTIONS are generic — set_updated_at / app_stamp_tenant_id —
--    so only the trigger names need tidying).
do $$
begin
  if exists (select 1 from pg_trigger where tgname = 'active_grants_updated_at'
             and tgrelid = 'applied_funding'::regclass) then
    alter trigger active_grants_updated_at on applied_funding rename to applied_funding_updated_at;
  end if;
  if exists (select 1 from pg_trigger where tgname = 'stamp_tenant_active_grants'
             and tgrelid = 'applied_funding'::regclass) then
    alter trigger stamp_tenant_active_grants on applied_funding rename to stamp_tenant_applied_funding;
  end if;
end $$;

-- 6) Refresh the PostgREST schema cache so the REST endpoint tracks the new name.
notify pgrst, 'reload schema';
