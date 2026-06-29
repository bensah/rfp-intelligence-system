-- 046 — Application-access fields on rfp_submissions.
-- apply_url    : the direct application-portal URL (captured at extraction:
--                extracted_solicitations.apply_url) so the Tracking page can show
--                an "Apply" button that opens the portal.
-- how_to_apply : LLM-synthesised, high-level step-by-step "how to apply" guide for
--                this specific call (written by core.llm_synthesis during the
--                review-synthesis pass), shown on the Tracking card.
alter table rfp_submissions add column if not exists apply_url     text;
alter table rfp_submissions add column if not exists how_to_apply  text;
