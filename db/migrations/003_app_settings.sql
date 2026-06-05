-- Migration 003 — app-wide settings key/value store.
-- Currently holds the "year" used by all weekly dashboards so admins can
-- bump the review year without editing code.

create table if not exists app_settings (
    key         text primary key,
    value       text not null,
    updated_at  timestamptz not null default now(),
    updated_by  text
);

drop trigger if exists app_settings_updated_at on app_settings;
create trigger app_settings_updated_at
    before update on app_settings
    for each row execute function set_updated_at();

-- Seed the year if not already present
insert into app_settings (key, value)
values ('year', extract(year from now())::text)
on conflict (key) do nothing;
