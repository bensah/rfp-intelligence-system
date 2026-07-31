-- Migration 081 — backfill tenant slugs so the super_user view-as URL is readable.
-- =========================================================================
-- Tenants created via the UI ("Add tenant" form) or self-service onboarding never set
-- `slug`, so their super_user view-as URL fell back to a raw UUID (?tenant=<uuid>) instead
-- of a readable, stable ?tenant=<slug>. This backfills a unique slug from the name for
-- every tenant missing one. (Going forward, both creation paths set a slug via
-- auth.tenant_context.make_tenant_slug.)
--
-- Slug = lower-case name, non-alphanumeric runs → '-', trimmed; de-duplicated against
-- existing slugs with a numeric suffix. SAFE + ADDITIVE + RE-RUN-SAFE (only touches rows
-- where slug is null/blank).
-- =========================================================================

begin;

do $$
declare
  r     record;
  base  text;
  cand  text;
  n     int;
begin
  for r in select id, name from tenants
            where slug is null or btrim(slug) = '' loop
    base := btrim(regexp_replace(lower(coalesce(r.name, 'tenant')),
                                 '[^a-z0-9]+', '-', 'g'), '-');
    if base is null or base = '' then
      base := 'tenant';
    end if;
    cand := base;
    n := 1;
    while exists (select 1 from tenants where slug = cand and id <> r.id) loop
      n := n + 1;
      cand := base || '-' || n;
    end loop;
    update tenants set slug = cand where id = r.id;
  end loop;
end $$;

commit;

-- =========================================================================
-- ROLLBACK: slugs are additive display keys; no rollback needed. To clear a
-- specific backfilled slug: update tenants set slug = null where id = '<id>';
-- =========================================================================
