-- Migration 023 — Row-Level Security policies for the anon-key read path.
--
-- Background
-- ----------
-- Until now the Streamlit Cloud secret SUPABASE_KEY held the service_role
-- key, which bypasses RLS entirely. That's expedient (every query just
-- works) but it means the running Streamlit deploy has full DB admin
-- equivalence — anyone who can reach the deploy or extract the secret
-- has unrestricted read/write on every table.
--
-- The proper Supabase pattern:
--   • Streamlit Cloud secret SUPABASE_KEY = anon (sb_publishable_…) key.
--   • Local .env SUPABASE_KEY (for migrate.py, import_donor_intel.py,
--     run_scan.py, etc.) = service_role (sb_secret_…) key.
--   • Every table has explicit RLS policies that describe what the anon
--     role (i.e. the live app) is allowed to do.
--
-- For an internal CHAI BDT tool with auth-gated access at the Streamlit
-- layer, we use a permissive baseline: anon can do anything. The win over
-- "service_role on the client" is that the anon key is rotatable
-- separately AND the policies are now ready to tighten — when you decide
-- e.g. "regular users should only read RFPs assigned to their office,
-- not all of them", that's a single ALTER POLICY here, no code change.
--
-- TODO follow-ups (deferred — not blocking):
--   1. donor_intel / donor_contacts / app_settings: split SELECT vs.
--      INSERT/UPDATE/DELETE so the live app can browse but only Admin
--      users (asserted by claims or app-level role lookup) can mutate.
--   2. rfp_submissions: filter by office_id once we move beyond a single
--      country deployment.
--   3. users / password_reset_requests: scope SELECT to row.owner so a
--      teller can't list other tellers' password-reset emails.
--
-- Re-running this file is safe: enable-rls is idempotent, and the policy
-- block uses DROP-IF-EXISTS + CREATE.

-- helper: enable RLS + (re)create one permissive policy per table.
do $$
declare
  t text;
  tables text[] := array[
    'active_grants',
    'app_settings',
    'donor_contacts',
    'donor_intel',
    'donor_source_seeds',
    'donor_sources',
    'engagement_logs',
    'meeting_logs',
    'meeting_schedule',
    'narrative_logs',
    'password_reset_requests',
    'rfp_submissions',
    'scan_logs',
    'users'
  ];
  policy_name text;
begin
  foreach t in array tables
  loop
    -- enable RLS (idempotent)
    execute format('alter table %I enable row level security', t);

    -- drop any prior baseline policy of ours so this stays re-runnable
    policy_name := t || '_rfpis_baseline';
    execute format('drop policy if exists %I on %I', policy_name, t);

    -- create the baseline FOR ALL policy — matches today's behaviour but
    -- now expressed as policy you can tighten table-by-table.
    execute format(
      'create policy %I on %I for all using (true) with check (true)',
      policy_name, t
    );

    raise notice 'rls baseline applied: %', t;
  end loop;
end $$;
