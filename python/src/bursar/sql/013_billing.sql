-- bursar: provider-agnostic billing lifecycle tables and RPCs.
-- Tracks subscriptions, invoices, payments, refunds, events, and
-- provider references — decoupled from any specific payment provider.
--
-- Depends on: credit_plans (004_plans.sql), user_credits (001_core_schema.sql)

-- ── Billing offers (commercial variants of a plan) ──────────────────────

CREATE TABLE IF NOT EXISTS public.billing_offers (
    offer_key TEXT PRIMARY KEY,
    plan_key TEXT NOT NULL,
    interval TEXT NOT NULL DEFAULT 'month',
    interval_count INTEGER NOT NULL DEFAULT 1,
    entitlement_mode TEXT NOT NULL DEFAULT 'allowance',
    cycle_grant_credits INTEGER,
    cycle_grant_tier TEXT,
    cycle_grant_replace_prior BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_billing_offers_plan_key ON public.billing_offers (plan_key);

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

-- ── Billing subscriptions ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_subscriptions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL,
    provider TEXT NOT NULL,
    provider_subscription_id TEXT NOT NULL,
    provider_customer_id TEXT,
    offer_key TEXT REFERENCES public.billing_offers(offer_key),
    plan_key TEXT,
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

-- ── Billing events (idempotency log) ────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_events (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing'
        CHECK (status IN ('processing', 'completed', 'failed')),
    payload JSONB,
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

-- ── Billing credit topups (config) ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.billing_credit_topups (
    topup_key TEXT PRIMARY KEY,
    tier TEXT NOT NULL DEFAULT 'purchased',
    currency TEXT NOT NULL DEFAULT 'USD',
    credits_per_major_unit INTEGER NOT NULL DEFAULT 1000,
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

-- ── RPC: set_user_plan (updated — accepts anchored plan_assigned_at) ────

DROP FUNCTION IF EXISTS public.set_user_plan(UUID, TEXT);
DROP FUNCTION IF EXISTS public.set_user_plan(UUID, TEXT, TIMESTAMPTZ);

CREATE OR REPLACE FUNCTION public.set_user_plan(
    p_user_id UUID,
    p_plan_key TEXT,
    p_plan_assigned_at TIMESTAMPTZ DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_id UUID;
    v_assigned_at TIMESTAMPTZ;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT id INTO v_plan_id
    FROM public.credit_plans
    WHERE plan_key = p_plan_key;

    IF v_plan_id IS NULL THEN
        RETURN jsonb_build_object('error', 'plan_not_found');
    END IF;

    v_assigned_at := COALESCE(p_plan_assigned_at, now());

    INSERT INTO public.user_credits (user_id, plan_id, plan_assigned_at)
    VALUES (p_user_id, v_plan_id, v_assigned_at)
    ON CONFLICT (user_id) DO UPDATE SET
        plan_id = v_plan_id,
        plan_assigned_at = v_assigned_at,
        updated_at = now();

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_assigned_at', v_assigned_at
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.set_user_plan(UUID, TEXT, TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;

-- ── RPC: sync_billing_from_config ───────────────────────────────────────

CREATE OR REPLACE FUNCTION public.sync_billing_from_config(p_config JSONB)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_key TEXT;
    v_item JSONB;
    v_ref JSONB;
    v_provider TEXT;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN;
    END IF;

    -- Sync billing offers (subscription plans)
    -- Note: offers are upserted, not deleted+rebuilt. Removing an offer from
    -- the config leaves a stale row, but this is intentional — active
    -- subscriptions reference offers via FK (billing_subscriptions.offer_key)
    -- and deleting would break them. Use resolve_billing_offer_by_price to
    -- find offers; absent keys simply won't resolve.
    IF p_config ? 'subscriptions' AND jsonb_typeof(p_config->'subscriptions') = 'object' THEN
        DELETE FROM public.billing_provider_refs
        WHERE resource_type = 'offer';
        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'subscriptions')
        LOOP
            INSERT INTO public.billing_offers (
                offer_key, plan_key, interval, interval_count,
                entitlement_mode, cycle_grant_credits, cycle_grant_tier,
                cycle_grant_replace_prior
            )
            VALUES (
                v_key,
                v_item->>'plan_key',
                COALESCE(v_item->>'interval', 'month'),
                COALESCE((v_item->>'interval_count')::INTEGER, 1),
                COALESCE(v_item->>'entitlement_mode', 'allowance'),
                (v_item->>'cycle_grant_credits')::INTEGER,
                v_item->>'cycle_grant_tier',
                COALESCE((v_item->>'cycle_grant_replace_prior')::BOOLEAN, true)
            )
            ON CONFLICT (offer_key) DO UPDATE SET
                plan_key = EXCLUDED.plan_key,
                interval = EXCLUDED.interval,
                interval_count = EXCLUDED.interval_count,
                entitlement_mode = EXCLUDED.entitlement_mode,
                cycle_grant_credits = EXCLUDED.cycle_grant_credits,
                cycle_grant_tier = EXCLUDED.cycle_grant_tier,
                cycle_grant_replace_prior = EXCLUDED.cycle_grant_replace_prior,
                updated_at = now();

            -- Sync provider refs for this offer
            IF v_item ? 'provider_refs' AND jsonb_typeof(v_item->'provider_refs') = 'object' THEN
                FOR v_provider, v_ref IN SELECT * FROM jsonb_each(v_item->'provider_refs')
                LOOP
                    INSERT INTO public.billing_provider_refs (
                        provider, price_id, product_id, variant_id,
                        lookup_key, resource_type, resource_key
                    )
                    VALUES (
                        v_provider,
                        v_ref->>'price_id',
                        v_ref->>'product_id',
                        v_ref->>'variant_id',
                        v_ref->>'lookup_key',
                        'offer',
                        v_key
                    )
                    ;
                END LOOP;
            END IF;
        END LOOP;
    END IF;

    -- Sync credit topups
    IF p_config ? 'credit_topups' AND jsonb_typeof(p_config->'credit_topups') = 'object' THEN
        DELETE FROM public.billing_provider_refs
        WHERE resource_type = 'topup';
        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'credit_topups')
        LOOP
            INSERT INTO public.billing_credit_topups (
                topup_key, tier, currency, credits_per_major_unit,
                min_amount_minor, max_amount_minor, tax_behavior
            )
            VALUES (
                v_key,
                COALESCE(v_item->>'tier', 'purchased'),
                COALESCE(v_item->>'currency', 'USD'),
                COALESCE((v_item->>'credits_per_major_unit')::INTEGER, 1000),
                COALESCE((v_item->>'min_amount_minor')::INTEGER, 500),
                COALESCE((v_item->>'max_amount_minor')::INTEGER, 500000),
                COALESCE(v_item->>'tax_behavior', 'exclude_tax')
            )
            ON CONFLICT (topup_key) DO UPDATE SET
                tier = EXCLUDED.tier,
                currency = EXCLUDED.currency,
                credits_per_major_unit = EXCLUDED.credits_per_major_unit,
                min_amount_minor = EXCLUDED.min_amount_minor,
                max_amount_minor = EXCLUDED.max_amount_minor,
                tax_behavior = EXCLUDED.tax_behavior,
                updated_at = now();

            -- Sync provider refs for this topup
            IF v_item ? 'provider_refs' AND jsonb_typeof(v_item->'provider_refs') = 'object' THEN
                FOR v_provider, v_ref IN SELECT * FROM jsonb_each(v_item->'provider_refs')
                LOOP
                    INSERT INTO public.billing_provider_refs (
                        provider, price_id, product_id, variant_id,
                        lookup_key, resource_type, resource_key
                    )
                    VALUES (
                        v_provider,
                        v_ref->>'price_id',
                        v_ref->>'product_id',
                        v_ref->>'variant_id',
                        v_ref->>'lookup_key',
                        'topup',
                        v_key
                    )
                    ;
                END LOOP;
            END IF;
        END LOOP;
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sync_billing_from_config(JSONB) FROM PUBLIC, anon, authenticated;

-- ── RPC: resolve_billing_offer_by_price ─────────────────────────────────

CREATE OR REPLACE FUNCTION public.resolve_billing_offer_by_price(
    p_provider TEXT,
    p_price_id TEXT DEFAULT NULL,
    p_product_id TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_ref RECORD;
    v_offer RECORD;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    IF p_price_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM public.billing_provider_refs
        WHERE provider = p_provider AND price_id = p_price_id AND resource_type = 'offer'
        LIMIT 1;
    ELSIF p_product_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM public.billing_provider_refs
        WHERE provider = p_provider AND product_id = p_product_id AND resource_type = 'offer'
        LIMIT 1;
    END IF;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_offer
    FROM public.billing_offers
    WHERE offer_key = v_ref.resource_key;

    IF v_offer.offer_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'offer_key', v_offer.offer_key,
        'plan_key', v_offer.plan_key,
        'interval', v_offer.interval,
        'interval_count', v_offer.interval_count,
        'entitlement_mode', v_offer.entitlement_mode,
        'cycle_grant_credits', v_offer.cycle_grant_credits,
        'cycle_grant_tier', v_offer.cycle_grant_tier,
        'cycle_grant_replace_prior', v_offer.cycle_grant_replace_prior
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.resolve_billing_offer_by_price(TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- ── RPC: resolve_credit_topup_by_price ──────────────────────────────────

CREATE OR REPLACE FUNCTION public.resolve_credit_topup_by_price(
    p_provider TEXT,
    p_price_id TEXT DEFAULT NULL,
    p_product_id TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_ref RECORD;
    v_topup RECORD;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    IF p_price_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM public.billing_provider_refs
        WHERE provider = p_provider AND price_id = p_price_id AND resource_type = 'topup'
        LIMIT 1;
    ELSIF p_product_id IS NOT NULL THEN
        SELECT * INTO v_ref
        FROM public.billing_provider_refs
        WHERE provider = p_provider AND product_id = p_product_id AND resource_type = 'topup'
        LIMIT 1;
    END IF;

    IF v_ref.resource_key IS NULL THEN
        RETURN NULL;
    END IF;

    SELECT * INTO v_topup
    FROM public.billing_credit_topups
    WHERE topup_key = v_ref.resource_key;

    IF v_topup.topup_key IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'topup_key', v_topup.topup_key,
        'tier', v_topup.tier,
        'currency', v_topup.currency,
        'credits_per_major_unit', v_topup.credits_per_major_unit,
        'min_amount_minor', v_topup.min_amount_minor,
        'max_amount_minor', v_topup.max_amount_minor,
        'tax_behavior', v_topup.tax_behavior
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.resolve_credit_topup_by_price(TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;

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
    v_existing_id UUID;
    v_existing_status TEXT;
    v_new_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT id, status INTO v_existing_id, v_existing_status
    FROM public.billing_events
    WHERE provider = p_provider AND provider_event_id = p_event_id
    FOR UPDATE;

    IF v_existing_id IS NOT NULL THEN
        IF v_existing_status = 'failed' THEN
            UPDATE public.billing_events
            SET status = 'processing',
                event_type = p_event_type,
                payload = p_payload,
                updated_at = now()
            WHERE id = v_existing_id;
            RETURN jsonb_build_object('status', 'retry', 'event_id', v_existing_id);
        END IF;
        RETURN jsonb_build_object('status', 'duplicate');
    END IF;

    BEGIN
        INSERT INTO public.billing_events (provider, provider_event_id, event_type, status, payload)
        VALUES (p_provider, p_event_id, p_event_type, 'processing', p_payload)
        RETURNING id INTO v_new_id;
    EXCEPTION
        WHEN unique_violation THEN
            SELECT id, status INTO v_existing_id, v_existing_status
            FROM public.billing_events
            WHERE provider = p_provider AND provider_event_id = p_event_id
            FOR UPDATE;

            IF v_existing_status = 'failed' THEN
                UPDATE public.billing_events
                SET status = 'processing',
                    event_type = p_event_type,
                    payload = p_payload,
                    updated_at = now()
                WHERE id = v_existing_id;
                RETURN jsonb_build_object('status', 'retry', 'event_id', v_existing_id);
            END IF;

            RETURN jsonb_build_object('status', 'duplicate');
    END;

    RETURN jsonb_build_object('status', 'claimed', 'event_id', v_new_id);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.claim_billing_event(TEXT, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;

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
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN;
    END IF;

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
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN;
    END IF;

    UPDATE public.billing_events
    SET status = 'failed', updated_at = now()
    WHERE provider = p_provider AND provider_event_id = p_event_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.fail_billing_event(TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- ── RPC: get_user_billing_subscription ────────────────────────────────────

CREATE OR REPLACE FUNCTION public.get_user_billing_subscription(
    p_user_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_row RECORD;
BEGIN
    SELECT * INTO v_row
    FROM public.billing_subscriptions
    WHERE user_id = p_user_id
    ORDER BY current_period_start DESC NULLS LAST, created_at DESC
    LIMIT 1;

    IF v_row.id IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN jsonb_build_object(
        'user_id', v_row.user_id,
        'provider', v_row.provider,
        'provider_subscription_id', v_row.provider_subscription_id,
        'provider_customer_id', v_row.provider_customer_id,
        'offer_key', v_row.offer_key,
        'plan_key', v_row.plan_key,
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

REVOKE EXECUTE ON FUNCTION public.get_user_billing_subscription(UUID) FROM PUBLIC, anon, authenticated;

-- ── Extend set_active_pricing_config to also sync billing config ────────

CREATE OR REPLACE FUNCTION public.set_active_pricing_config(
    p_config JSONB,
    p_label TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_new_id UUID;
    v_next_version INTEGER;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM public.credit_pricing_config;

    UPDATE public.credit_pricing_config SET active = false WHERE active = true;

    INSERT INTO public.credit_pricing_config (config, active, version, label)
    VALUES (p_config, true, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    PERFORM public.sync_plans_from_config(p_config);
    PERFORM public.sync_tiers_from_config(p_config);
    BEGIN
        PERFORM public.sync_billing_from_config(p_config);
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'billing config sync failed (pricing update still applied): %', SQLERRM;
    END;

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true
    );
END;
$$;
