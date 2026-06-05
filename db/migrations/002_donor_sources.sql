-- Migration 002 — add donor_sources table and triggered_by to scan_logs.
-- Idempotent: safe to run multiple times.

create table if not exists donor_sources (
    id                  uuid primary key default gen_random_uuid(),
    donor_name          text not null,
    donor_code          text,
    base_url            text,
    rfp_listing_url     text not null,
    scrape_method       text not null default 'html'
                        check (scrape_method in ('html','rss','rest_json','manual')),
    selectors           jsonb,
    notes               text,
    is_active           boolean not null default true,
    last_scraped_at     timestamptz,
    last_scrape_status  text,
    created_by          text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create unique index if not exists donor_sources_url_idx on donor_sources(rfp_listing_url);
create index if not exists donor_sources_active_idx on donor_sources(is_active);

-- scan_logs.triggered_by column (idempotent via information_schema check)
do $$
begin
    if not exists (
        select 1 from information_schema.columns
        where table_name = 'scan_logs' and column_name = 'triggered_by'
    ) then
        alter table scan_logs add column triggered_by text not null default 'cron';
        alter table scan_logs add constraint scan_logs_triggered_by_chk
            check (triggered_by in ('cron','manual','startup','test'));
    end if;
end $$;

-- updated_at trigger for donor_sources (reuses existing set_updated_at function)
drop trigger if exists donor_sources_updated_at on donor_sources;
create trigger donor_sources_updated_at
    before update on donor_sources
    for each row execute function set_updated_at();

-- Seed a few starter rows so the Admin > Donor Sources tab is not empty.
-- Idempotent via unique index on rfp_listing_url.
insert into donor_sources (donor_name, donor_code, base_url, rfp_listing_url, scrape_method, notes)
values
    ('Gates Foundation', 'BMGF', 'https://gcgh.grandchallenges.org',
     'https://gcgh.grandchallenges.org/challenges', 'html',
     'Grand Challenges in Global Health — periodic challenge launches.'),
    ('Wellcome Trust', 'Wellcome', 'https://wellcome.org',
     'https://wellcome.org/grant-funding/schemes', 'html',
     'Open funding schemes including Climate & Health, Discovery, Mental Health.'),
    ('Unitaid', 'Unitaid', 'https://unitaid.org',
     'https://unitaid.org/call-for-proposals/', 'html',
     'Calls for proposals — health products for HIV/TB/Malaria/MNCH/PPR.'),
    ('Gavi', 'Gavi', 'https://www.gavi.org',
     'https://www.gavi.org/about-us/work-us/rfps-eois', 'html',
     'RFPs and EOIs.'),
    ('Global Fund', 'Global Fund', 'https://www.theglobalfund.org',
     'https://www.theglobalfund.org/en/business-opportunities/', 'html',
     'Business opportunities portal.'),
    ('NIH', 'NIH', 'https://grants.nih.gov',
     'https://grants.nih.gov/grants/guide/rss/index.xml', 'rss',
     'NIH Guide RSS feed; LMIC + Fogarty calls.'),
    ('Grants.gov', 'Grants.gov', 'https://www.grants.gov',
     'https://api.grants.gov/v1/api/search2', 'rest_json',
     'US federal grants search API — filter by CFDA / opportunity status.')
on conflict (rfp_listing_url) do nothing;
