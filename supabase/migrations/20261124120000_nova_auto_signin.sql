-- Per-login consent for Nova to sign in on its own. Off by default; the
-- consent time and text version are kept so the member's choice is auditable.
BEGIN;
ALTER TABLE public.portal_credentials
  ADD COLUMN IF NOT EXISTS auto_signin boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS consent_at timestamptz,
  ADD COLUMN IF NOT EXISTS consent_version varchar(20);
COMMIT;
