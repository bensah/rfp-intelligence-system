-- RFPIS — RFP Intelligence System
-- Supabase / PostgreSQL schema. Run once in the Supabase SQL editor.
-- Org-agnostic: deploying-org profile lives in app_settings (see migration 003).

create extension if not exists "pgcrypto";

-- =========================================================================
-- users
-- =========================================================================
create table if not exists users (
    id              uuid primary key default gen_random_uuid(),
    email           text unique not null,
    name            text,
    role            text not null default 'collaborator'
                    check (role in ('admin','reviewer','collaborator')),
    password_hash   text,
    is_active       boolean not null default true,
    last_login_at   timestamptz,
    created_at      timestamptz not null default now()
);

-- =========================================================================
-- rfp_submissions  (replaces Excel Sheet1 / Form1 / RFP_Screener_bak)
-- =========================================================================
create table if not exists rfp_submissions (
    id                          uuid primary key default gen_random_uuid(),
    uid                         text unique not null,           -- e.g. BE-260202-1220
    form_id                     text unique not null,           -- same as uid for new records
    source                      text not null default 'auto'
                                check (source in ('auto','manual','migration')),
    submitted_by                text,
    submitted_by_email          text,
    submitted_at                timestamptz not null default now(),
    search_date                 timestamptz,                    -- date the RFP was discovered/searched
    form_start_date             timestamptz,
    form_end_date               timestamptz,

    -- 1. Opportunity Description
    opportunity_id              text,
    opportunity_title           text not null,
    brief_description           text,
    date_posted                 date,
    funding_agency              text,
    geographic_scope            text[],
    program_area                text[],
    focus_theme                 text,
    opportunity_link            text,
    applicant_role                   text,
    funding_window              text,
    submission_deadline         date,
    expected_award_date         date,
    time_to_award               text,
    estimated_value             numeric,
    currency                    text,
    project_duration            integer,
    submission_format           text,

    -- 2. Eligibility Scoring (Yes / Partial / No / null)
    feasibility                 text,
    must_1_govt_alignment       text,
    must_2_strategic_fit        text,
    must_3_implementable        text,
    must_4_compliant            text,
    must_5_resourcing           text,
    prefer_6_funding_quality    text,
    prefer_7_monitorable        text,
    prefer_8_partnership        text,
    prefer_9_scale              text,
    decline_flags_present       boolean not null default false,
    key_risks                   text,
    alignment_score             numeric,
    auto_recommendation         text,

    -- 3. Decision-making
    decision                    text,
    decision_date               date,
    decision_rationale          text,
    stage                       text,
    proposal_lead               text,
    contributors                text[],
    reviewers                   text[],
    support_roles               text,
    progress_status             text,
    amount_requested            numeric,
    date_completed              date,
    donor_decision              text,
    next_action                 text,
    assigned_to                 text,
    remarks                     text,
    notes                       text,
    action_deadline             date,
    last_update                 date,
    date_of_approval            date,
    amount_secured              numeric,
    currency_secured            text,
    donor_program_officer       text,
    next_step                   text,
    kickoff_date                date,

    -- Audit / lifecycle
    review_week                 text,                          -- e.g. 'Week 22 (25 May - 31 May)'
    is_duplicate                boolean not null default false,
    duplicate_of_uid            text,
    decision_overridden_by      text,
    decision_overridden_at      timestamptz,

    created_at                  timestamptz not null default now(),
    updated_at                  timestamptz not null default now()
);

create index if not exists rfp_submissions_review_week_idx on rfp_submissions(review_week);
create index if not exists rfp_submissions_decision_idx     on rfp_submissions(decision);
create index if not exists rfp_submissions_deadline_idx     on rfp_submissions(submission_deadline);
create index if not exists rfp_submissions_funder_idx       on rfp_submissions(funding_agency);
create index if not exists rfp_submissions_is_duplicate_idx on rfp_submissions(is_duplicate);

-- =========================================================================
-- meeting_logs  (Monday team call notes)
-- =========================================================================
create table if not exists meeting_logs (
    id              uuid primary key default gen_random_uuid(),
    meeting_date    date not null,
    donor_title     text,
    rfp_uid         text references rfp_submissions(uid),
    remarks         text,
    actions         text,
    owner           text,
    deadline        date,
    is_resolved     boolean not null default false,
    created_by      text,
    created_at      timestamptz not null default now()
);

create index if not exists meeting_logs_date_idx on meeting_logs(meeting_date);
create index if not exists meeting_logs_rfp_idx  on meeting_logs(rfp_uid);

-- =========================================================================
-- meeting_schedule  (rota: who note-takes / presents / chairs each Monday)
-- =========================================================================
create table if not exists meeting_schedule (
    id              uuid primary key default gen_random_uuid(),
    call_date       date unique not null,
    note_taker      text,
    rfp_presenter   text,
    meeting_orgr   text,
    created_at      timestamptz not null default now()
);

-- =========================================================================
-- engagement_logs  (KR2.2 donor engagements)
-- =========================================================================
create table if not exists engagement_logs (
    id                  uuid primary key default gen_random_uuid(),
    engagement_date     date not null,
    donor               text,
    engagement_type     text,
    format              text,
    internal_lead           text,
    donor_contacts      text,
    purpose             text,
    outcome             text,
    linked_rfp_uid      text references rfp_submissions(uid),
    created_by          text,
    created_at          timestamptz not null default now()
);

create index if not exists engagement_logs_date_idx on engagement_logs(engagement_date);

-- =========================================================================
-- active_grants  (KR2.3 reporting deadlines for awarded grants)
-- =========================================================================
create table if not exists active_grants (
    id                  uuid primary key default gen_random_uuid(),
    grant_id            text not null,
    donor_title         text,
    form_id_link        text,
    award_date          date,
    end_date            date,
    report_type         text,
    report_due_date     date,
    submitted_date      date,
    status              text,
    owner               text,
    remarks             text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists active_grants_form_id_idx on active_grants(form_id_link);
create index if not exists active_grants_due_idx     on active_grants(report_due_date);

-- =========================================================================
-- narrative_logs  (KR2.4 country narrative versions)
-- =========================================================================
create table if not exists narrative_logs (
    id              uuid primary key default gen_random_uuid(),
    version_date    date not null,
    narrative_title text,
    used_in         text,
    used_with       text,
    date_used       date,
    status          text,
    link_location   text,
    owner           text,
    created_at      timestamptz not null default now()
);

-- =========================================================================
-- donor_sources  (curated per-donor RFP listing URLs for targeted scraping)
-- =========================================================================
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

-- =========================================================================
-- scan_logs  (one row per scraper source per run; manual or cron)
-- =========================================================================
create table if not exists scan_logs (
    id              uuid primary key default gen_random_uuid(),
    scan_date       timestamptz not null default now(),
    source          text,
    triggered_by    text not null default 'cron'
                    check (triggered_by in ('cron','manual','startup','test')),
    rfps_found      integer not null default 0,
    rfps_new        integer not null default 0,
    rfps_duplicate  integer not null default 0,
    errors          text,
    duration_sec    numeric
);

create index if not exists scan_logs_date_idx on scan_logs(scan_date);

-- =========================================================================
-- updated_at triggers
-- =========================================================================
create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end $$;

drop trigger if exists rfp_submissions_updated_at on rfp_submissions;
create trigger rfp_submissions_updated_at
    before update on rfp_submissions
    for each row execute function set_updated_at();

drop trigger if exists active_grants_updated_at on active_grants;
create trigger active_grants_updated_at
    before update on active_grants
    for each row execute function set_updated_at();

drop trigger if exists donor_sources_updated_at on donor_sources;
create trigger donor_sources_updated_at
    before update on donor_sources
    for each row execute function set_updated_at();
