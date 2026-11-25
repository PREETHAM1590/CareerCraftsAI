-- Destructive rollback: received portal mail is deleted with the table.
BEGIN;
DROP TABLE IF EXISTS public.apply_inbox_messages;
DROP TABLE IF EXISTS public.apply_email_aliases;
COMMIT;
