-- Migration 019 — collapse the "Proceed as sub" decision into "Proceed".
--
-- As of 2026-06-06 the decision vocabulary is just Proceed / Park / Decline.
-- The Prime/Sub/Technical distinction now lives ONLY in `applicant_role`, so
-- "Proceed as sub" is redundant. Convert any legacy rows (auto-scanned or
-- Excel-imported) so the Tracking/Review filters and KPIs stay consistent.
--
-- Rows that were "Proceed as sub" keep their applicant_role = 'Sub' (set at
-- scan/import time), so no signal is lost. Idempotent: re-running is a no-op.

update rfp_submissions
   set decision = 'Proceed'
 where decision = 'Proceed as sub';

-- Belt-and-braces: where a row was sub-routed but role wasn't recorded, leave
-- role as-is (Prime default) — reviewers can set Role on the Review tab.
