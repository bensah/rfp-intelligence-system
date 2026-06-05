-- Migration 009 — relax the scan_logs.triggered_by CHECK constraint.
--
-- Original constraint (migration 002) limited triggered_by to the literal
-- strings 'cron', 'manual', 'startup', 'test'. We now want to record WHO
-- triggered manual runs so audits are useful — e.g. "manual:user@x.com".
-- Dropping the CHECK lets the column accept free-form audit labels.

alter table scan_logs
    drop constraint if exists scan_logs_triggered_by_chk;
