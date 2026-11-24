-- Token rotation is confirmed on first use: the new token waits here and the
-- current one keeps working until the extension uses the new one. Additive only.
ALTER TABLE public.extension_devices
    ADD COLUMN IF NOT EXISTS pending_token_hash varchar(64);
CREATE UNIQUE INDEX IF NOT EXISTS extension_devices_pending_token_idx
    ON public.extension_devices (pending_token_hash)
    WHERE pending_token_hash IS NOT NULL;
