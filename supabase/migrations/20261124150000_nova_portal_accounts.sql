-- Sites where a member lets Nova create a portal account for them, and
-- which saved logins Nova created. Consent time and text version are kept.
BEGIN;
CREATE TABLE IF NOT EXISTS public.portal_account_consents (
  user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  origin varchar(255) NOT NULL,
  consent_at timestamptz NOT NULL DEFAULT now(),
  consent_version varchar(20) NOT NULL,
  PRIMARY KEY (user_id, origin)
);
ALTER TABLE public.portal_account_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.portal_credentials
  ADD COLUMN IF NOT EXISTS created_by_nova boolean NOT NULL DEFAULT false;
-- Backend performs member-scoped access; no browser database grants.
COMMIT;
