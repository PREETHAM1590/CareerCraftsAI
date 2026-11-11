-- Company careers boards a member saved ("search this portal directly"). Additive
-- only. The backend re-resolves careers_url against its ATS allow-list on every
-- use, so family/tenant are display and lookup hints, never fetch targets.
CREATE TABLE IF NOT EXISTS public.saved_job_boards (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    source_id varchar(200) NOT NULL,
    family varchar(40) NOT NULL,
    tenant text NOT NULL,
    label varchar(150) NOT NULL,
    careers_url text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT saved_job_boards_user_source UNIQUE (user_id, source_id)
);

CREATE INDEX IF NOT EXISTS saved_job_boards_user_idx ON public.saved_job_boards (user_id);

ALTER TABLE public.saved_job_boards ENABLE ROW LEVEL SECURITY;
-- Backend performs member-scoped access; no browser database grants.
REVOKE ALL ON public.saved_job_boards FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.saved_job_boards TO service_role;
DROP POLICY IF EXISTS saved_job_boards_service ON public.saved_job_boards;
CREATE POLICY saved_job_boards_service ON public.saved_job_boards
    TO service_role USING (true) WITH CHECK (true);

-- Safe downgrade: only removes what this migration introduced.
-- DROP TABLE IF EXISTS public.saved_job_boards;
