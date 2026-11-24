-- Private Telegram chat a member linked for Nova's one-time code requests.
BEGIN;
CREATE TABLE IF NOT EXISTS public.telegram_links (
  user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
  chat_id bigint NOT NULL UNIQUE,
  linked_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.telegram_links ENABLE ROW LEVEL SECURITY;
-- Backend performs member-scoped access; no browser database grants.
COMMIT;
