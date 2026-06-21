-- 039: split the conflated type into two orthogonal axes (Bernard's ontology):
--   solicitation_type — HOW you apply / how the call is announced
--       (NOFO, RFA, RFP, CFP, CFA, CfCN, EOI, LOI, RFI, RFQ, Tender, Bid, ITB,
--        Procurement notice, Unsolicited, Challenge)
--   instrument_type   — the donor↔beneficiary CONTRACT if awarded
--       (Grant, Cooperative Agreement, Contract, Loan, Equity/Investment,
--        Prize/Award, Fellowship, Scholarship, Seed fund, In-kind/TA)
-- The legacy opportunity_type / opportunity_types columns are kept (deprecated,
-- no longer written) — a backfill script splits them into the new fields.
-- Idempotent.

alter table rfp_submissions add column if not exists solicitation_type text;
alter table rfp_submissions add column if not exists instrument_type  text;

alter table scan_decisions  add column if not exists solicitation_type text;
alter table scan_decisions  add column if not exists instrument_type  text;

alter table source_registry add column if not exists solicitation_types text[];
alter table source_registry add column if not exists instrument_types   text[];

alter table donor_sources   add column if not exists solicitation_types text[];
alter table donor_sources   add column if not exists instrument_types   text[];
