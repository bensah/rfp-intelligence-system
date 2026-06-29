-- Migration 048 — enforce "decision stays Pending until a HUMAN decides".
--
-- Reverses the auto-promote behaviour of migration 013. Per the 2026-06-25 rule,
-- `decision` is the HUMAN's call and must stay NULL (= "Pending") until a reviewer
-- sets it via the Review tab or the Records edit form — both of which stamp
-- `decision_overridden_by`. Leaving `decision = auto_recommendation` on auto rows
-- pollutes the learning signal (we'd train on our own guess) and hides the
-- Pending state in the UI.
--
-- Safe scope: ONLY auto-scanned rows (source = 'auto') that NO human has touched
-- (decision_overridden_by IS NULL). Human-decided rows (Review/Records set
-- decision_overridden_by) and non-auto rows (submitted forms, migrated Excel
-- records whose `decision` is a real human "Bid Decision") are left untouched.
--
-- Going forward the insert path already leaves `decision` NULL (auto_score /
-- _build_row never write it), so no further auto-fill occurs. Do NOT re-run
-- migration 013 — it is superseded by this one.
--
-- The Tracking / Screen / Records views already coalesce decision -> auto_recommendation
-- for display bucketing, so un-reviewed rows still group correctly while showing
-- "Pending" as their human decision.

update rfp_submissions
   set decision = null
 where source = 'auto'
   and decision is not null
   and decision_overridden_by is null;
