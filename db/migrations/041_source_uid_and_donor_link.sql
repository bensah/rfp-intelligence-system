-- 041: Stable source UID + Donor-Intelligence linkage.
--
-- Goal: every source row — in BOTH the scan catalogue (donor_sources, URL-keyed)
-- and the curation registry (source_registry, host-keyed) — carries a stable,
-- human-readable unique identifier, and triangulates to a donor in the Donor
-- Intelligence Mapping table (donor_intel) so sources link cleanly to donors.
--
-- New columns (added to both tables):
--   source_uid     — stable, unique, human-readable key. Host-based; for the few
--                    catalogue hosts that carry >1 source row, a short hash of the
--                    listing URL is appended so it stays unique. In the registry it
--                    is simply the host (already the table's natural key).
--   donor_intel_id — FK into donor_intel(id): the donor this source belongs to.
--   donor_key      — snapshot of donor_intel.canonical_key (the human-readable
--                    triangulation key; 110/110 unique in donor_intel today).
-- donor_sources additionally gets:
--   host           — normalised netloc (strip leading "www."). This is the JOIN
--                    KEY between the URL-keyed catalogue and the host-keyed registry.
--
-- All idempotent (add column / create index "if not exists"). FK uses
-- ON DELETE SET NULL so retiring a donor_intel row never orphans a source.

-- donor_sources -------------------------------------------------------------
alter table donor_sources   add column if not exists source_uid     text;
alter table donor_sources   add column if not exists host           text;
alter table donor_sources   add column if not exists donor_intel_id bigint references donor_intel(id) on delete set null;
alter table donor_sources   add column if not exists donor_key      text;

-- source_registry (host is already the natural PK) --------------------------
alter table source_registry add column if not exists source_uid     text;
alter table source_registry add column if not exists donor_intel_id bigint references donor_intel(id) on delete set null;
alter table source_registry add column if not exists donor_key      text;

-- uniqueness + join indexes -------------------------------------------------
create unique index if not exists donor_sources_source_uid_key    on donor_sources(source_uid);
create unique index if not exists source_registry_source_uid_key  on source_registry(source_uid);
create index        if not exists donor_sources_host_idx          on donor_sources(host);
create index        if not exists donor_sources_donor_intel_idx   on donor_sources(donor_intel_id);
create index        if not exists source_registry_donor_intel_idx on source_registry(donor_intel_id);
