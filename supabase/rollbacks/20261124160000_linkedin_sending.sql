BEGIN;
ALTER TABLE public.linkedin_outreach_queue DROP COLUMN IF EXISTS delivery, DROP COLUMN IF EXISTS delivery_error;
DROP TABLE IF EXISTS public.linkedin_accounts;
COMMIT;
