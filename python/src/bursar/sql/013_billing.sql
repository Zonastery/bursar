-- bursar: provider-agnostic billing lifecycle tables and RPCs.
-- Tracks subscriptions, invoices, payments, refunds, events, and
-- provider references — decoupled from any specific payment provider.
--
-- Depends on: credit_plans (004_plans.sql), user_credits (001_core_schema.sql)

-- ── Billing offers (commercial variants of a plan) ──────────────────────

CREATE TABLE IF NOT EXISTS public.billing_offers (
    offer_key TEXT PRIMARY KEY,
    plan TEXT NOT NULL,
    interval TEXT NOT NULL DEFAULT 'month',
    interval_count INTEGER NOT NULL DEFAULT 1,
    grant_mode TEXT NOT NULL DEFAULT 'allowance',
    grant_credits INTEGER,
    grant_bucket TEXT,
    grant_replace_prior BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_billing_offers_plan ON public.billing_offers (plan);

ALTER TABLE public.billing_offers ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_offers'
        AND tablename = 'billing_offers'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_offers" ON public.billing_offers USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_offers_updated_at'
        AND tgrelid = 'public.billing_offers'::regclass
    ) THEN
        CREATE TRIGGER set_billing_offers_updated_at
            BEFORE UPDATE ON public.billing_offers
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── Provider references (maps provider IDs to billing offers / topups) ──

CREATE TABLE IF NOT EXISTS public.billing_provider_refs (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    price_id TEXT,
    product_id TEXT,
    variant_id TEXT,
    lookup_key TEXT,
    resource_type TEXT NOT NULL CHECK (resource_type IN ('offer', 'topup')),
    resource_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_provider_refs_price
    ON public.billing_provider_refs (provider, price_id)
    WHERE price_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_provider_refs_product
    ON public.billing_provider_refs (provider, product_id)
    WHERE product_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_billing_provider_refs_resource
    ON public.billing_provider_refs (resource_type, resource_key);

ALTER TABLE public.billing_provider_refs ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_provider_refs'
        AND tablename = 'billing_provider_refs'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_provider_refs" ON public.billing_provider_refs USING (false);
    END IF;
END;
$$;

-- ── Billing customers (provider → user mapping) ─────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_customers (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_customer_id TEXT NOT NULL,
    user_id UUID NOT NULL,
    email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_customers_provider
    ON public.billing_customers (provider, provider_customer_id);

CREATE INDEX IF NOT EXISTS idx_billing_customers_user
    ON public.billing_customers (user_id);

ALTER TABLE public.billing_customers ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_customers'
        AND tablename = 'billing_customers'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_customers" ON public.billing_customers USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_customers_updated_at'
        AND tgrelid = 'public.billing_customers'::regclass
    ) THEN
        CREATE TRIGGER set_billing_customers_updated_at
            BEFORE UPDATE ON public.billing_customers
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── Billing subscriptions ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_subscriptions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL,
    provider TEXT NOT NULL,
    provider_subscription_id TEXT NOT NULL,
    provider_customer_id TEXT,
    offer_key TEXT REFERENCES public.billing_offers(offer_key),
    plan TEXT,
    status TEXT NOT NULL DEFAULT 'incomplete',
    current_period_start TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
    interval TEXT,
    interval_count INTEGER,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_subscriptions_provider
    ON public.billing_subscriptions (provider, provider_subscription_id);

CREATE INDEX IF NOT EXISTS idx_billing_subscriptions_user
    ON public.billing_subscriptions (user_id);

CREATE INDEX IF NOT EXISTS idx_billing_subscriptions_status
    ON public.billing_subscriptions (status);

ALTER TABLE public.billing_subscriptions ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_subscriptions'
        AND tablename = 'billing_subscriptions'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_subscriptions" ON public.billing_subscriptions USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_subscriptions_updated_at'
        AND tgrelid = 'public.billing_subscriptions'::regclass
    ) THEN
        CREATE TRIGGER set_billing_subscriptions_updated_at
            BEFORE UPDATE ON public.billing_subscriptions
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── Billing events (idempotency log) ────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_events (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing'
        CHECK (status IN ('processing', 'completed', 'failed')),
    payload JSONB,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_events_provider
    ON public.billing_events (provider, provider_event_id);

CREATE INDEX IF NOT EXISTS idx_billing_events_status
    ON public.billing_events (status);

ALTER TABLE public.billing_events ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_events'
        AND tablename = 'billing_events'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_events" ON public.billing_events USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_events_updated_at'
        AND tgrelid = 'public.billing_events'::regclass
    ) THEN
        CREATE TRIGGER set_billing_events_updated_at
            BEFORE UPDATE ON public.billing_events
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── Billing invoices ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_invoices (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_invoice_id TEXT NOT NULL,
    provider_subscription_id TEXT,
    user_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    amount_paid_minor INTEGER,
    amount_due_minor INTEGER,
    currency TEXT NOT NULL DEFAULT 'USD',
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_invoices_provider
    ON public.billing_invoices (provider, provider_invoice_id);

CREATE INDEX IF NOT EXISTS idx_billing_invoices_user
    ON public.billing_invoices (user_id);

ALTER TABLE public.billing_invoices ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_invoices'
        AND tablename = 'billing_invoices'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_invoices" ON public.billing_invoices USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_invoices_updated_at'
        AND tgrelid = 'public.billing_invoices'::regclass
    ) THEN
        CREATE TRIGGER set_billing_invoices_updated_at
            BEFORE UPDATE ON public.billing_invoices
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── Billing payments ────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_payments (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_payment_id TEXT NOT NULL,
    provider_invoice_id TEXT,
    user_id UUID NOT NULL,
    amount_minor INTEGER NOT NULL,
    tax_minor INTEGER,
    currency TEXT NOT NULL DEFAULT 'USD',
    purpose TEXT NOT NULL DEFAULT 'unknown'
        CHECK (purpose IN ('subscription', 'credit_topup', 'unknown')),
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- updated_at is already defined in CREATE TABLE IF NOT EXISTS public.billing_payments above.

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_payments_provider
    ON public.billing_payments (provider, provider_payment_id);

CREATE INDEX IF NOT EXISTS idx_billing_payments_user
    ON public.billing_payments (user_id);

ALTER TABLE public.billing_payments ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_payments'
        AND tablename = 'billing_payments'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_payments" ON public.billing_payments USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_payments_updated_at'
        AND tgrelid = 'public.billing_payments'::regclass
    ) THEN
        CREATE TRIGGER set_billing_payments_updated_at
            BEFORE UPDATE ON public.billing_payments
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── Billing refunds ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_refunds (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_refund_id TEXT NOT NULL,
    provider_payment_id TEXT,
    user_id UUID NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    reason TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- updated_at is already defined in CREATE TABLE IF NOT EXISTS public.billing_refunds above.

CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_refunds_provider
    ON public.billing_refunds (provider, provider_refund_id);

ALTER TABLE public.billing_refunds ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_refunds'
        AND tablename = 'billing_refunds'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_refunds" ON public.billing_refunds USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_refunds_updated_at'
        AND tgrelid = 'public.billing_refunds'::regclass
    ) THEN
        CREATE TRIGGER set_billing_refunds_updated_at
            BEFORE UPDATE ON public.billing_refunds
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── Billing credit topups (config) ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_credit_topups (
    topup_key TEXT PRIMARY KEY,
    deposit_to TEXT NOT NULL DEFAULT 'purchased',
    credits_per_unit INTEGER NOT NULL DEFAULT 1000,
    min_amount_minor INTEGER NOT NULL DEFAULT 500,
    max_amount_minor INTEGER NOT NULL DEFAULT 500000,
    tax_behavior TEXT NOT NULL DEFAULT 'exclude_tax'
        CHECK (tax_behavior IN ('exclude_tax', 'include_tax')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.billing_credit_topups ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only billing_credit_topups'
        AND tablename = 'billing_credit_topups'
        AND schemaname = 'public'
    ) THEN
        CREATE POLICY "Server-only billing_credit_topups" ON public.billing_credit_topups USING (false);
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_billing_credit_topups_updated_at'
        AND tgrelid = 'public.billing_credit_topups'::regclass
    ) THEN
        CREATE TRIGGER set_billing_credit_topups_updated_at
            BEFORE UPDATE ON public.billing_credit_topups
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- ── RPC: set_user_plan (updated — accepts anchored plan_assigned_at) ────

DO $$ DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'set_user_plan' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

-- SUPERSEDED by 016_plan_versioning.sql -- this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.set_user_plan(UUID, TEXT, TIMESTAMPTZ)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN RETURN NULL; END;
$$;

-- ── RPC: sync_billing_from_config ───────────────────────────────────────
-- SUPERSEDED by 017_billing_lifecycle.sql -- this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.sync_billing_from_config(p_config JSONB)
RETURNS VOID LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN NULL; END;
$$;

-- ── RPC: resolve_billing_offer_by_price ─────────────────────────────────
-- SUPERSEDED by 017_billing_lifecycle.sql -- this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.resolve_billing_offer_by_price(p_provider TEXT, p_price_id TEXT DEFAULT NULL, p_product_id TEXT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN RETURN NULL; END;
$$;

-- ── RPC: resolve_credit_topup_by_price ──────────────────────────────────
-- SUPERSEDED by 017_billing_lifecycle.sql -- this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.resolve_credit_topup_by_price(p_provider TEXT, p_price_id TEXT DEFAULT NULL, p_product_id TEXT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN RETURN NULL; END;
$$;

-- ── RPC: upsert_billing_customer ──────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.upsert_billing_customer(
    p_provider TEXT,
    p_provider_customer_id TEXT,
    p_user_id UUID,
    p_email TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_existing_user UUID;
BEGIN
    SELECT user_id INTO v_existing_user
    FROM public.billing_customers
    WHERE provider = p_provider AND provider_customer_id = p_provider_customer_id;

    IF v_existing_user IS NOT NULL AND v_existing_user <> p_user_id THEN
        RETURN jsonb_build_object(
            'error', 'user_id_mismatch',
            'message', 'provider customer already mapped to a different user'
        );
    END IF;

    INSERT INTO public.billing_customers (provider, provider_customer_id, user_id, email)
    VALUES (p_provider, p_provider_customer_id, p_user_id, p_email)
    ON CONFLICT (provider, provider_customer_id) DO UPDATE SET
        email = COALESCE(EXCLUDED.email, billing_customers.email),
        updated_at = now();

    RETURN jsonb_build_object('status', 'ok');
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_billing_customer(TEXT, TEXT, UUID, TEXT) FROM PUBLIC, anon, authenticated;

-- ── RPC: get_billing_customer ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_billing_customer(
    p_provider TEXT,
    p_provider_customer_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_user_id UUID;
BEGIN
    SELECT user_id INTO v_user_id
    FROM public.billing_customers
    WHERE provider = p_provider AND provider_customer_id = p_provider_customer_id
    LIMIT 1;

    IF v_user_id IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object('user_id', v_user_id);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_billing_customer(TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- ── RPC: upsert_billing_subscription ───────────────────────────────────────

CREATE OR REPLACE FUNCTION public.upsert_billing_subscription(
    p_state JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_existing_user UUID;
BEGIN
    SELECT user_id INTO v_existing_user
    FROM public.billing_subscriptions
    WHERE provider = p_state->>'provider'
      AND provider_subscription_id = p_state->>'provider_subscription_id';

    IF v_existing_user IS NOT NULL
       AND v_existing_user <> (p_state->>'user_id')::UUID THEN
        RETURN jsonb_build_object(
            'error', 'user_id_mismatch',
            'message', 'provider subscription already mapped to a different user'
        );
    END IF;

    INSERT INTO public.billing_subscriptions (
        user_id, provider, provider_subscription_id, provider_customer_id,
        offer_key, plan, status, current_period_start,
        current_period_end, cancel_at_period_end, interval, interval_count, metadata
    )
    VALUES (
        (p_state->>'user_id')::UUID,
        p_state->>'provider',
        p_state->>'provider_subscription_id',
        p_state->>'provider_customer_id',
        p_state->>'offer_key',
        p_state->>'plan',
        COALESCE(p_state->>'status', 'incomplete'),
        (p_state->>'current_period_start')::TIMESTAMPTZ,
        (p_state->>'current_period_end')::TIMESTAMPTZ,
        COALESCE((p_state->>'cancel_at_period_end')::BOOLEAN, false),
        p_state->>'interval',
        (p_state->>'interval_count')::INTEGER,
        (p_state->>'metadata')::JSONB
    )
    ON CONFLICT (provider, provider_subscription_id) DO UPDATE SET
        provider_customer_id = COALESCE(EXCLUDED.provider_customer_id, billing_subscriptions.provider_customer_id),
        offer_key = COALESCE(EXCLUDED.offer_key, billing_subscriptions.offer_key),
        plan = COALESCE(EXCLUDED.plan, billing_subscriptions.plan),
        status = EXCLUDED.status,
        current_period_start = COALESCE(EXCLUDED.current_period_start, billing_subscriptions.current_period_start),
        current_period_end = COALESCE(EXCLUDED.current_period_end, billing_subscriptions.current_period_end),
        cancel_at_period_end = EXCLUDED.cancel_at_period_end,
        interval = COALESCE(EXCLUDED.interval, billing_subscriptions.interval),
        interval_count = COALESCE(EXCLUDED.interval_count, billing_subscriptions.interval_count),
        metadata = CASE WHEN (p_state->>'metadata') IS NOT NULL THEN (p_state->>'metadata')::JSONB ELSE billing_subscriptions.metadata END,
        updated_at = now();

    RETURN jsonb_build_object('status', 'ok');
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_billing_subscription(JSONB) FROM PUBLIC, anon, authenticated;

-- ── RPC: get_billing_subscription ──────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_billing_subscription(
    p_provider TEXT,
    p_provider_subscription_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_row RECORD;
BEGIN
    SELECT
        user_id, provider, provider_subscription_id, provider_customer_id,
        offer_key, plan, status, current_period_start,
        current_period_end, cancel_at_period_end, interval, interval_count, metadata
    INTO v_row
    FROM public.billing_subscriptions
    WHERE provider = p_provider AND provider_subscription_id = p_provider_subscription_id
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'user_id', v_row.user_id,
        'provider', v_row.provider,
        'provider_subscription_id', v_row.provider_subscription_id,
        'provider_customer_id', v_row.provider_customer_id,
        'offer_key', v_row.offer_key,
        'plan', v_row.plan,
        'status', v_row.status,
        'current_period_start', v_row.current_period_start,
        'current_period_end', v_row.current_period_end,
        'cancel_at_period_end', v_row.cancel_at_period_end,
        'interval', v_row.interval,
        'interval_count', v_row.interval_count,
        'metadata', v_row.metadata
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_billing_subscription(TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- ── RPC: claim_billing_event (idempotent claim) ─────────────────────────

CREATE OR REPLACE FUNCTION public.claim_billing_event(
    p_provider TEXT,
    p_event_id TEXT,
    p_event_type TEXT,
    p_payload JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_existing RECORD;
    v_new_id UUID;
BEGIN
    BEGIN
        INSERT INTO public.billing_events (provider, provider_event_id, event_type, status, payload)
        VALUES (p_provider, p_event_id, p_event_type, 'processing', p_payload)
        RETURNING id INTO v_new_id;

        RETURN jsonb_build_object('status', 'claimed', 'event_id', v_new_id);
    EXCEPTION
        WHEN unique_violation THEN
            -- A concurrent caller already inserted this event. Re-fetch with
            -- FOR UPDATE (blocks until the inserter commits) and dispatch by
            -- status. This single copy of the status-handling logic eliminates
            -- the duplicate-code risk of the pre-INSERT SELECT pattern.
            SELECT * INTO v_existing
            FROM public.billing_events
            WHERE provider = p_provider AND provider_event_id = p_event_id
            FOR UPDATE;

            IF NOT FOUND THEN
                RETURN jsonb_build_object('status', 'duplicate');
            END IF;

            IF v_existing.status = 'completed' THEN
                RETURN jsonb_build_object('status', 'duplicate');
            END IF;

            IF v_existing.status = 'processing' THEN
                IF v_existing.retry_count >= 3 THEN
                    RETURN jsonb_build_object('status', 'max_retries_exceeded');
                END IF;
                IF v_existing.updated_at < now() - interval '5 minutes' THEN
                    UPDATE public.billing_events
                    SET status = 'processing', updated_at = now(), retry_count = v_existing.retry_count + 1
                    WHERE id = v_existing.id;
                    RETURN jsonb_build_object('status', 'claimed', 'event_id', v_existing.id);
                ELSE
                    RETURN jsonb_build_object('status', 'retry');
                END IF;
            END IF;

            IF v_existing.status = 'failed' THEN
                IF v_existing.retry_count >= 3 THEN
                    RETURN jsonb_build_object('status', 'max_retries_exceeded');
                END IF;
                UPDATE public.billing_events
                SET status = 'processing', updated_at = now(), retry_count = v_existing.retry_count + 1
                WHERE id = v_existing.id;
                RETURN jsonb_build_object('status', 'claimed', 'event_id', v_existing.id);
            END IF;

            RETURN jsonb_build_object('status', 'duplicate');
    END;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.claim_billing_event(TEXT, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;

-- ── RPC: reclaim_billing_event ──────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.reclaim_billing_event(
    p_provider TEXT,
    p_event_id TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_existing RECORD;
BEGIN
    SELECT * INTO v_existing FROM public.billing_events
    WHERE provider = p_provider AND provider_event_id = p_event_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('status', 'not_found');
    END IF;

    IF v_existing.status = 'completed' THEN
        RETURN jsonb_build_object('status', 'already_completed');
    END IF;

    IF v_existing.status = 'processing' THEN
        IF v_existing.updated_at >= now() - interval '5 minutes' THEN
            RETURN jsonb_build_object('status', 'retry');
        END IF;
    END IF;

    IF v_existing.status = 'failed' AND v_existing.retry_count >= 3 THEN
        RETURN jsonb_build_object('status', 'max_retries_exceeded');
    END IF;

    UPDATE public.billing_events
    SET status = 'processing', updated_at = now(), retry_count = v_existing.retry_count + 1
    WHERE id = v_existing.id;

    RETURN jsonb_build_object('status', 'reclaimed', 'event_id', v_existing.id);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.reclaim_billing_event(TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- ── RPC: complete_billing_event ─────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.complete_billing_event(
    p_provider TEXT,
    p_event_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    UPDATE public.billing_events
    SET status = 'completed', updated_at = now()
    WHERE provider = p_provider AND provider_event_id = p_event_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.complete_billing_event(TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- ── RPC: fail_billing_event ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.fail_billing_event(
    p_provider TEXT,
    p_event_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    UPDATE public.billing_events
    SET status = 'failed', updated_at = now()
    WHERE provider = p_provider AND provider_event_id = p_event_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.fail_billing_event(TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- ── RPC: get_user_billing_subscription ────────────────────────────────────
-- SUPERSEDED by 018_multi_provider_subs.sql -- this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.get_user_billing_subscription(UUID)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN RETURN NULL; END;
$$;

-- ── Extend set_active_bursar_config to also sync billing config ────────
-- SUPERSEDED by 016_plan_versioning.sql -- this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.set_active_bursar_config(p_config JSONB, p_label TEXT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN RETURN NULL; END;
$$;

-- Per-function GRANTs to service_role for LIVE billing RPCs in this migration.
-- The 012 migration handles GRANTs for all bursar functions via per-function
-- GRANTs (replacing the old blanket GRANT).
GRANT EXECUTE ON FUNCTION public.upsert_billing_customer(TEXT, TEXT, UUID, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_billing_customer(TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.upsert_billing_subscription(JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_billing_subscription(TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_billing_event(TEXT, TEXT, TEXT, JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.reclaim_billing_event(TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_billing_event(TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.fail_billing_event(TEXT, TEXT) TO service_role;

NOTIFY pgrst, 'reload schema';
