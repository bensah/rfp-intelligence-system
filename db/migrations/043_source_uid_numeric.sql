-- 043: make source_uid a SEQUENTIAL NUMERIC id (bigint) instead of the host
-- string, keeping it as the first column. Each table gets its own sequence so
-- new rows auto-number; existing rows are numbered 1..N by age. host remains the
-- readable string + the catalogue<->registry join key. Rebuild via temp-swap
-- (preserves PK/CHECK/FK/indexes/RLS/grants/trigger). Atomic; validated by
-- execute-then-rollback before commit.

begin;

-- ---- donor_sources: source_uid text(host) -> sequential bigint, still first column ----
create sequence if not exists donor_sources_source_uid_seq;
create table donor_sources_new (
  source_uid bigint not null default nextval('donor_sources_source_uid_seq'),
  id uuid default gen_random_uuid() not null,
  donor_name text not null,
  donor_code text,
  base_url text,
  rfp_listing_url text not null,
  scrape_method text default 'html'::text not null,
  selectors jsonb,
  notes text,
  is_active boolean default true not null,
  last_scraped_at timestamptz,
  last_scrape_status text,
  created_by text,
  created_at timestamptz default now() not null,
  updated_at timestamptz default now() not null,
  opportunity_types text[],
  access_model text,
  source_class text,
  solicitation_types text[],
  instrument_types text[],
  host text,
  donor_intel_id bigint,
  donor_key text
);
insert into donor_sources_new (source_uid, id, donor_name, donor_code, base_url, rfp_listing_url, scrape_method, selectors, notes, is_active, last_scraped_at, last_scrape_status, created_by, created_at, updated_at, opportunity_types, access_model, source_class, solicitation_types, instrument_types, host, donor_intel_id, donor_key)
  select row_number() over (order by created_at, donor_name), id, donor_name, donor_code, base_url, rfp_listing_url, scrape_method, selectors, notes, is_active, last_scraped_at, last_scrape_status, created_by, created_at, updated_at, opportunity_types, access_model, source_class, solicitation_types, instrument_types, host, donor_intel_id, donor_key from donor_sources;
select setval('donor_sources_source_uid_seq', (select coalesce(max(source_uid),0) from donor_sources_new));
alter sequence donor_sources_source_uid_seq owned by donor_sources_new.source_uid;
drop table donor_sources;
alter table donor_sources_new rename to donor_sources;
alter table donor_sources add constraint donor_sources_scrape_method_check CHECK ((scrape_method = ANY (ARRAY['html'::text, 'html_js'::text, 'rss'::text, 'rest_json'::text, 'manual'::text])));
alter table donor_sources add constraint donor_sources_donor_intel_id_fkey FOREIGN KEY (donor_intel_id) REFERENCES donor_intel(id) ON DELETE SET NULL;
alter table donor_sources add constraint donor_sources_pkey PRIMARY KEY (id);
CREATE UNIQUE INDEX donor_sources_url_idx ON public.donor_sources USING btree (rfp_listing_url);
CREATE INDEX donor_sources_active_idx ON public.donor_sources USING btree (is_active);
CREATE UNIQUE INDEX donor_sources_source_uid_key ON public.donor_sources USING btree (source_uid);
CREATE INDEX donor_sources_host_idx ON public.donor_sources USING btree (host);
CREATE INDEX donor_sources_donor_intel_idx ON public.donor_sources USING btree (donor_intel_id);
alter table donor_sources enable row level security;
create policy donor_sources_rfpis_baseline on donor_sources as permissive for all to public using (true) with check (true);
grant all on table donor_sources to anon, authenticated, service_role;
CREATE TRIGGER donor_sources_updated_at BEFORE UPDATE ON public.donor_sources FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- source_registry: source_uid text(host) -> sequential bigint, still first column ----
create sequence if not exists source_registry_source_uid_seq;
create table source_registry_new (
  source_uid bigint not null default nextval('source_registry_source_uid_seq'),
  host text not null,
  classification text default 'unknown'::text not null,
  status text default 'pending'::text not null,
  detected_as text,
  sample_url text,
  sample_title text,
  hits integer default 1 not null,
  first_seen timestamptz default now() not null,
  last_seen timestamptz default now() not null,
  verified_by text,
  verified_at timestamptz,
  notes text,
  source_class text,
  access_model text,
  ingestion_method text,
  has_api boolean default false not null,
  opportunity_types text[],
  donor_name text,
  donor_code text,
  solicitation_types text[],
  instrument_types text[],
  in_catalogue boolean default false,
  donor_intel_id bigint,
  donor_key text
);
insert into source_registry_new (source_uid, host, classification, status, detected_as, sample_url, sample_title, hits, first_seen, last_seen, verified_by, verified_at, notes, source_class, access_model, ingestion_method, has_api, opportunity_types, donor_name, donor_code, solicitation_types, instrument_types, in_catalogue, donor_intel_id, donor_key)
  select row_number() over (order by first_seen, host), host, classification, status, detected_as, sample_url, sample_title, hits, first_seen, last_seen, verified_by, verified_at, notes, source_class, access_model, ingestion_method, has_api, opportunity_types, donor_name, donor_code, solicitation_types, instrument_types, in_catalogue, donor_intel_id, donor_key from source_registry;
select setval('source_registry_source_uid_seq', (select coalesce(max(source_uid),0) from source_registry_new));
alter sequence source_registry_source_uid_seq owned by source_registry_new.source_uid;
drop table source_registry;
alter table source_registry_new rename to source_registry;
alter table source_registry add constraint source_registry_donor_intel_id_fkey FOREIGN KEY (donor_intel_id) REFERENCES donor_intel(id) ON DELETE SET NULL;
alter table source_registry add constraint source_registry_pkey PRIMARY KEY (host);
CREATE INDEX source_registry_class_idx ON public.source_registry USING btree (classification);
CREATE INDEX source_registry_status_idx ON public.source_registry USING btree (status);
CREATE INDEX source_registry_in_catalogue_idx ON public.source_registry USING btree (in_catalogue);
CREATE UNIQUE INDEX source_registry_source_uid_key ON public.source_registry USING btree (source_uid);
CREATE INDEX source_registry_donor_intel_idx ON public.source_registry USING btree (donor_intel_id);
alter table source_registry enable row level security;
create policy source_registry_rfpis_baseline on source_registry as permissive for all to public using (true) with check (true);
grant all on table source_registry to anon, authenticated, service_role;

notify pgrst, 'reload schema';

commit;
