ALTER TABLE public.users ADD COLUMN IF NOT EXISTS memory_generation integer NOT NULL DEFAULT 0;
