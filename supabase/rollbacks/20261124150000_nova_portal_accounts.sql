BEGIN;
ALTER TABLE public.portal_credentials DROP COLUMN IF EXISTS created_by_nova;
DROP TABLE IF EXISTS public.portal_account_consents;
COMMIT;
