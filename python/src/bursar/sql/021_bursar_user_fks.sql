-- bursar + Better Auth FK integration.
--
-- Adds ON DELETE CASCADE foreign keys from bursar's credit and billing tables
-- to public."user"(id). When BA's deleteUser() removes a user, the database
-- automatically cascades to clean up all bursar records — no application-level
-- cleanup needed.
--
-- Credit cascade chain:  public."user" → user_credits → (transactions,
-- reservations, team_members, spend_caps, usage_window, credit_buckets)
--
-- Billing tables: each gets a direct FK to public."user".

-- ── user_credits: add FK to public."user" ─────────────────────────────────

ALTER TABLE IF EXISTS public.user_credits
    ADD CONSTRAINT user_credits_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

-- ── Credit child tables: drop existing FK to user_credits, re-add with CASCADE ──

ALTER TABLE IF EXISTS public.credit_transactions
    DROP CONSTRAINT IF EXISTS credit_transactions_user_id_fkey,
    ADD CONSTRAINT credit_transactions_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.credit_reservations
    DROP CONSTRAINT IF EXISTS credit_reservations_user_id_fkey,
    ADD CONSTRAINT credit_reservations_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.credit_team_members
    DROP CONSTRAINT IF EXISTS credit_team_members_user_id_fkey,
    ADD CONSTRAINT credit_team_members_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.credit_spend_caps
    DROP CONSTRAINT IF EXISTS credit_spend_caps_user_id_fkey,
    ADD CONSTRAINT credit_spend_caps_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.credit_usage_window
    DROP CONSTRAINT IF EXISTS credit_usage_window_user_id_fkey,
    ADD CONSTRAINT credit_usage_window_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;

-- user_credit_buckets already has ON DELETE CASCADE — no change needed.

-- ── Billing tables: each gets a direct FK to public."user" ──────────────

ALTER TABLE IF EXISTS public.billing_customers
    ADD CONSTRAINT billing_customers_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.billing_subscriptions
    ADD CONSTRAINT billing_subscriptions_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.billing_invoices
    ADD CONSTRAINT billing_invoices_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.billing_payments
    ADD CONSTRAINT billing_payments_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.billing_refunds
    ADD CONSTRAINT billing_refunds_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.billing_disputes
    ADD CONSTRAINT billing_disputes_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;

ALTER TABLE IF EXISTS public.billing_preferences
    ADD CONSTRAINT billing_preferences_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
