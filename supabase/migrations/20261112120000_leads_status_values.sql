-- The leads API (app/api/v1/leads.py VALID_STATUSES) and the leads page write
-- 'hot', 'contacted' and 'closed', which the original constraint rejected, so
-- those writes failed with a CheckViolation. 'converted' stays allowed so rows
-- written before the API's status set changed still pass. Additive only.
ALTER TABLE public.leads DROP CONSTRAINT IF EXISTS leads_status_check;
ALTER TABLE public.leads ADD CONSTRAINT leads_status_check
    CHECK (status IN ('cold', 'warm', 'hot', 'contacted', 'replied', 'closed', 'converted'));
