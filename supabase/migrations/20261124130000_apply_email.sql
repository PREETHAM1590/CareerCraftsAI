-- Per-member application email address (<local_part>@APPLY_EMAIL_DOMAIN) and
-- the portal mail it receives: verification codes, links and employer replies.
BEGIN;
CREATE TABLE IF NOT EXISTS public.apply_email_aliases (
  user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
  local_part varchar(64) NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.apply_email_aliases ENABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS public.apply_inbox_messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  provider_email_id varchar(100) NOT NULL,
  sender varchar(320) NOT NULL,
  subject varchar(500) NOT NULL DEFAULT '',
  text_body text NOT NULL DEFAULT '',
  codes jsonb NOT NULL DEFAULT '[]'::jsonb,
  links jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- The Nova run whose waiting code step this message filled, if any.
  filled_run_id uuid,
  received_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT apply_inbox_messages_provider_user UNIQUE (provider_email_id, user_id)
);
CREATE INDEX IF NOT EXISTS apply_inbox_messages_owner_received
  ON public.apply_inbox_messages (user_id, received_at DESC);
ALTER TABLE public.apply_inbox_messages ENABLE ROW LEVEL SECURITY;
-- Backend performs member-scoped access; no browser database grants.
COMMIT;
