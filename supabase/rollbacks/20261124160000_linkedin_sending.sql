BEGIN;
ALTER TABLE public.linkedin_outreach_queue DROP COLUMN IF EXISTS delivery, DROP COLUMN IF EXISTS delivery_error, DROP COLUMN IF EXISTS delivery_started_at;
COMMIT;
