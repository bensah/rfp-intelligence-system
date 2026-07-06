-- 066: RLS baseline for tables created AFTER migration 023.
--
-- Migration 023 enabled Row-Level Security + a permissive "anon can do anything"
-- baseline policy on the tables that existed then. Four tables were created LATER and
-- never added to that set:
--   extracted_solicitations (044) · scan_decisions (027) · source_registry (034) ·
--   rfp_seen (033)
-- When RLS is ON for such a table (Supabase enables it on public tables) but no policy
-- exists, the anon-key app (Streamlit Cloud) is DENIED all writes:
--   "new row violates row-level security policy for table extracted_solicitations"
--   (SQLSTATE 42501) — which made every Run Extraction store 0 rows while the
--   service-role local key silently bypassed it.
--
-- Fix: apply the SAME permissive baseline as 023 (anon = full access via the auth-gated
-- app; the service_role key still bypasses RLS). Idempotent (drop-if-exists + create,
-- enable-rls is a no-op if already on) and existence-guarded, so it's safe to re-run and
-- skips any table not present in this environment.
do $$
declare
  t text;
  tables text[] := array[
    'extracted_solicitations',
    'scan_decisions',
    'source_registry',
    'rfp_seen'
  ];
  policy_name text;
begin
  foreach t in array tables
  loop
    if to_regclass('public.' || t) is null then
      raise notice 'skip (absent): %', t;
      continue;
    end if;
    execute format('alter table %I enable row level security', t);
    policy_name := t || '_rfpis_baseline';
    execute format('drop policy if exists %I on %I', policy_name, t);
    execute format(
      'create policy %I on %I for all using (true) with check (true)',
      policy_name, t);
    raise notice 'rls baseline applied: %', t;
  end loop;
end $$;
