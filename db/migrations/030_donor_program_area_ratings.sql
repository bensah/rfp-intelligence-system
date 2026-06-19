-- Migration 030 - donor_intel: add program_area_ratings — a JSON object mapping
-- each canonical program-area CHILD key (e.g. "IDs - Malaria & NTDs") to a 0–5
-- priority grade (0 absent · 1 very low · 2 low · 3 medium · 4 high · 5 very
-- high). Captured in the Donors edit form (Scope & fit → Strategic priority
-- areas) alongside priority_program_areas, and graded the SAME way on the org
-- fit profile (org_profile.program_area_ratings) so the matching engine can
-- correlate the two vectors into a graded strategic-fit score.
--
-- TEXT holding a JSON object (BLANK / '{}' = not graded). The vocabulary is the
-- shared taxonomy in core/program_area_classifier.py. Idempotent.

alter table donor_intel add column if not exists program_area_ratings text;
