-- Server-side auto-submit control, the submit approval audit trail, and
-- extension token expiry. Additive only: no column is dropped and no user
-- data is deleted.

-- Off by default; approve-submit enforces it with a rolling 24-hour cap.
ALTER TABLE public.user_preferences
    ADD COLUMN IF NOT EXISTS auto_submit_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS auto_submit_daily_cap integer NOT NULL DEFAULT 10;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'user_preferences_auto_submit_daily_cap_check'
    ) THEN
        ALTER TABLE public.user_preferences
            ADD CONSTRAINT user_preferences_auto_submit_daily_cap_check
            CHECK (auto_submit_daily_cap BETWEEN 1 AND 50);
    END IF;
END $$;

-- Device tokens expire; the extension rotates them before they do. Tokens
-- paired before this migration get a full lifetime from now rather than
-- being cut off mid-application.
ALTER TABLE public.extension_devices
    ADD COLUMN IF NOT EXISTS expires_at timestamptz;
UPDATE public.extension_devices
    SET expires_at = now() + interval '90 days'
    WHERE expires_at IS NULL AND revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS public.application_submit_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    job_application_id uuid REFERENCES public.job_applications(id) ON DELETE CASCADE,
    extension_task_id uuid REFERENCES public.extension_tasks(id) ON DELETE SET NULL,
    device_id uuid REFERENCES public.extension_devices(id) ON DELETE SET NULL,
    auto_approved boolean NOT NULL DEFAULT false,
    approved_by varchar(30) NOT NULL
        CHECK (approved_by IN ('member', 'auto_submit_setting')),
    review_hash varchar(64) NOT NULL,
    review_url text,
    answers jsonb NOT NULL DEFAULT '[]',
    screenshot_path text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS application_submit_approvals_user_created_idx
    ON public.application_submit_approvals (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS application_submit_approvals_application_idx
    ON public.application_submit_approvals (job_application_id);

-- Backend-only, like extension_tasks: ownership is checked by FastAPI.
ALTER TABLE public.application_submit_approvals ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.application_submit_approvals FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.application_submit_approvals TO service_role;
DROP POLICY IF EXISTS application_submit_approvals_service ON public.application_submit_approvals;
CREATE POLICY application_submit_approvals_service ON public.application_submit_approvals
    TO service_role USING (true) WITH CHECK (true);
