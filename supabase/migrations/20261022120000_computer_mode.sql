ALTER TABLE public.users ADD COLUMN IF NOT EXISTS computer_mode text NOT NULL
DEFAULT 'auto' CHECK (computer_mode IN ('auto','browser','cloud'));

-- Browser-mode liveness: refreshed only by the extension's command long-poll.
ALTER TABLE public.extension_devices ADD COLUMN IF NOT EXISTS agent_seen_at timestamptz;
