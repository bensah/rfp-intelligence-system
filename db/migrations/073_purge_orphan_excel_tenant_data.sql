-- Migration 073 — clean up Excel-migration data that leaked across tenants.
-- =========================================================================
-- Context: the Home page auto-ran the the sample country team Excel sync (source='migration')
-- for EVERY session, including a fresh Example Tenant super_user. migrate_excel.py
-- writes source='migration' rows but does NOT set tenant_id, so those rows are either
-- the organisation's (tenant_id backfilled to the sample country team by migration 067) or ORPHANS
-- (tenant_id IS NULL) — and a NULL-tenant row shows in EVERY tenant until read-isolation
-- is live. The auto-sync itself is now deactivated in multi-tenant mode (code change).
--
-- This script (idempotent, SAFE):
--   1. DELETES any migration row WRONGLY tagged to Example Tenant (defensive — there
--      usually are none, since the sync runs as service-role with no tenant claim).
--   2. RE-HOMES orphan migration rows (tenant_id IS NULL) to the sample country team, the Excel's
--      rightful owner, so they stop appearing in other tenants. Comment out block (2) if
--      you prefer to leave them NULL.
-- the sample country team's own tagged data is never touched.
--
-- Run the DIAGNOSTIC first (separate query) to see the current tag distribution:
--   select coalesce(t.name,'(NULL / no tenant)') as tenant, r.source, count(*)
--     from rfp_submissions r left join tenants t on t.id = r.tenant_id
--    group by 1,2 order by 3 desc;
-- =========================================================================

begin;

do $$
declare
  _tenant uuid;
  _org   uuid;
  _t      text;
  _tables text[] := array[
    'rfp_submissions','meeting_logs','meeting_schedule',
    'engagement_logs','active_grants','narrative_logs'];
  _has_src boolean;
  _has_tid boolean;
  _n bigint;
begin
  select id into _tenant from tenants where name = 'Example Tenant' limit 1;
  select id into _org   from tenants where name = 'the sample country team'      limit 1;

  foreach _t in array _tables loop
    if to_regclass(_t) is null then continue; end if;
    select exists(select 1 from information_schema.columns
                  where table_name = _t and column_name = 'source')    into _has_src;
    select exists(select 1 from information_schema.columns
                  where table_name = _t and column_name = 'tenant_id') into _has_tid;
    if not (_has_src and _has_tid) then continue; end if;

    -- 1) Purge any excel rows tagged to the second tenant (leaves the organisation's data intact).
    if _tenant is not null then
      execute format('delete from %I where tenant_id = $1 and source = %L', _t, 'migration')
        using _tenant;
      get diagnostics _n = row_count;
      raise notice 'deleted % second-tenant-tagged migration row(s) from %', _n, _t;
    end if;

    -- 2) Re-home orphan (NULL-tenant) excel rows to the sample country team (their owner).
    --    Comment out this IF block to leave orphans as-is.
    if _org is not null then
      execute format('update %I set tenant_id = $1 where tenant_id is null and source = %L',
                     _t, 'migration') using _org;
      get diagnostics _n = row_count;
      raise notice 're-homed % orphan migration row(s) to the sample country team in %', _n, _t;
    end if;
  end loop;
end $$;

commit;

-- =========================================================================
-- NOTE: even after this, a fresh tenant (a second tenant) will still SEE the organisation's rows until
-- tenant READ-isolation is active (RLS migration 068, or app-layer tenant filtering on
-- the data reads). This script only removes second-tenant-tagged / orphan rows; it does not
-- add isolation. See the chat message for the recommended next step.
-- =========================================================================
