-- LinkedIn sending (behind LINKEDIN_OUTREACH_ENABLED): the member's LinkedIn
-- account as connected through Unipile's hosted login (an account id only,
-- never a password or session cookie), and each outreach item's delivery.
BEGIN;
CREATE TABLE IF NOT EXISTS public.linkedin_accounts (
  user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
  unipile_account_id varchar(100) NOT NULL UNIQUE,
  connected_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.linkedin_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.linkedin_outreach_queue
  ADD COLUMN IF NOT EXISTS delivery varchar(20),
  ADD COLUMN IF NOT EXISTS delivery_error text;
-- Backend performs member-scoped access; no browser database grants.
COMMIT;
