BEGIN;
ALTER TABLE public.portal_credentials
  DROP COLUMN IF EXISTS auto_signin,
  DROP COLUMN IF EXISTS consent_at,
  DROP COLUMN IF EXISTS consent_version;
COMMIT;
