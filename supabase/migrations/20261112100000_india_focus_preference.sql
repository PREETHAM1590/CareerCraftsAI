-- Per-member India-first switch for job search and auto-apply: roles open
-- only outside India are hidden and never applied to while it is on.
-- Additive only; existing members get it on.

ALTER TABLE public.user_preferences
    ADD COLUMN IF NOT EXISTS india_focus boolean NOT NULL DEFAULT true;
