-- Migration 084 — login_logs: per-user sign-in history (IP + browser) for the Profile page.
-- =========================================================================
-- Each successful login records a row (best-effort, non-fatal) with the client IP and
-- user-agent, so a user can review recent sign-ins on their Profile and spot activity they
-- don't recognise. A user may read ONLY their OWN rows (RLS via the JWT `sub` claim); the
-- app captures logins on the RLS-bypassing service client. SAFE + ADDITIVE + RE-RUN-SAFE.
-- Requires app_current_jwt_sub() (migration 080).
-- =========================================================================

begin;

create table if not exists login_logs (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid references users(id) on delete cascade,
  email      text,
  at         timestamptz not null default now(),
  ip         text,
  user_agent text
);

create index if not exists login_logs_user_at_idx  on login_logs (user_id, at desc);
create index if not exists login_logs_email_at_idx on login_logs (email, at desc);

alter table login_logs enable row level security;
drop policy if exists login_logs_sel on login_logs;
drop policy if exists login_logs_ins on login_logs;

-- SELECT: only your OWN sign-in history.
create policy login_logs_sel on login_logs
  for select using (user_id = app_current_jwt_sub());
-- INSERT: allowed (login capture may run before a tenant JWT exists); the app sets user_id.
create policy login_logs_ins on login_logs
  for insert with check (true);

grant select, insert on login_logs to anon, authenticated;
grant all on login_logs to service_role;

commit;

-- ROLLBACK:  begin; drop table if exists login_logs; commit;
