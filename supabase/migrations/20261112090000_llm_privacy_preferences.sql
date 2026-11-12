-- Per-member switches for what the LLM gateway hides from the model
-- provider: contact details and IDs, and separately the member's name.
-- Additive only; existing members get both switches on.

ALTER TABLE public.user_preferences
    ADD COLUMN IF NOT EXISTS llm_hide_personal_info boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS llm_hide_name boolean NOT NULL DEFAULT true;
