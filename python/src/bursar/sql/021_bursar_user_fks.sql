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
--
-- Skipped entirely when public."user" is absent (bare Postgres / Supabase-only).
-- Each constraint is added idempotently so setup() can re-apply all migrations.

DO $$
BEGIN
    IF to_regclass('public.user') IS NULL THEN
        RAISE NOTICE 'Skipping 021_bursar_user_fks: public."user" not present';
        RETURN;
    END IF;

    -- ── user_credits: add FK to public."user" ─────────────────────────────
    BEGIN
        ALTER TABLE public.user_credits
            ADD CONSTRAINT user_credits_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    -- ── Credit child tables: re-add FKs with CASCADE ──────────────────────
    ALTER TABLE IF EXISTS public.credit_transactions
        DROP CONSTRAINT IF EXISTS credit_transactions_user_id_fkey;
    BEGIN
        ALTER TABLE public.credit_transactions
            ADD CONSTRAINT credit_transactions_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    ALTER TABLE IF EXISTS public.credit_reservations
        DROP CONSTRAINT IF EXISTS credit_reservations_user_id_fkey;
    BEGIN
        ALTER TABLE public.credit_reservations
            ADD CONSTRAINT credit_reservations_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    ALTER TABLE IF EXISTS public.credit_team_members
        DROP CONSTRAINT IF EXISTS credit_team_members_user_id_fkey;
    BEGIN
        ALTER TABLE public.credit_team_members
            ADD CONSTRAINT credit_team_members_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    ALTER TABLE IF EXISTS public.credit_spend_caps
        DROP CONSTRAINT IF EXISTS credit_spend_caps_user_id_fkey;
    BEGIN
        ALTER TABLE public.credit_spend_caps
            ADD CONSTRAINT credit_spend_caps_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    ALTER TABLE IF EXISTS public.credit_usage_window
        DROP CONSTRAINT IF EXISTS credit_usage_window_user_id_fkey;
    BEGIN
        ALTER TABLE public.credit_usage_window
            ADD CONSTRAINT credit_usage_window_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public.user_credits(user_id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    -- user_credit_buckets already has ON DELETE CASCADE — no change needed.

    -- ── Billing tables: each gets a direct FK to public."user" ────────────
    BEGIN
        ALTER TABLE public.billing_customers
            ADD CONSTRAINT billing_customers_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    BEGIN
        ALTER TABLE public.billing_subscriptions
            ADD CONSTRAINT billing_subscriptions_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    BEGIN
        ALTER TABLE public.billing_invoices
            ADD CONSTRAINT billing_invoices_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    BEGIN
        ALTER TABLE public.billing_payments
            ADD CONSTRAINT billing_payments_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    BEGIN
        ALTER TABLE public.billing_refunds
            ADD CONSTRAINT billing_refunds_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    BEGIN
        ALTER TABLE public.billing_disputes
            ADD CONSTRAINT billing_disputes_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;

    BEGIN
        ALTER TABLE public.billing_preferences
            ADD CONSTRAINT billing_preferences_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES public."user"(id) ON DELETE CASCADE;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
END $$;
