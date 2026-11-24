-- LinkedIn sending (behind LINKEDIN_OUTREACH_ENABLED): each outreach item's
-- delivery through the member's own extension.
BEGIN;
ALTER TABLE public.linkedin_outreach_queue
  ADD COLUMN IF NOT EXISTS delivery varchar(20),
  ADD COLUMN IF NOT EXISTS delivery_error text,
  ADD COLUMN IF NOT EXISTS delivery_started_at timestamptz;
COMMIT;
