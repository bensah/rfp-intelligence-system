-- Migration 022 - donor_contacts: many contacts per donor.
-- The donor_intel row carries ONE set of institutional/official contact fields
-- (hq_*, general_email, main_phone, donor_linkedin_url, ...). This table adds the
-- one-to-many list: focal persons and additional contacts (official channels OR
-- people the team has engaged), so the matrix becomes a comprehensive, private
-- donor CRM. Managed from the Donors page edit dialog (admin / super-user).
--
-- Privacy: this is a private, authorised-users-only list. Personal contact data
-- (names, emails, phones, LinkedIn) should be sourced from official published
-- pages or first-party relationships - never mass-compiled or guessed.
--
-- FK on canonical_key (donor_intel.canonical_key is UNIQUE and stable). ON DELETE
-- CASCADE so removing a donor clears its contacts. Re-importing the matrix upserts
-- on canonical_key (no delete), so UI-added contacts SURVIVE a matrix re-import.

create table if not exists donor_contacts (
    id            bigserial primary key,
    canonical_key text not null references donor_intel(canonical_key) on delete cascade,
    contact_name  text,
    role_title    text,
    email         text,
    phone         text,
    linkedin_url  text,
    address       text,
    is_official   boolean not null default false,
    notes         text,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists donor_contacts_key_idx on donor_contacts(canonical_key);
