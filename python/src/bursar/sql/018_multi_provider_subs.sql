-- bursar: multi-provider subscription awareness — coexistence and migration.
--
-- Phase 4:
--   - get_user_billing_subscription: optional provider filter
--   - get_user_billing_subscriptions: return all subs as JSON array
--   - deactivate_other_provider_subscriptions: cancel subs from other providers

-- ── Drop old get_user_billing_subscription (signature changes) ───────────

DO $$ DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'get_user_billing_subscription'
          AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;


-- ── get_user_billing_subscription — optional provider filter ────────────
--
-- When p_provider is specified, returns the subscription for that provider.
-- When NULL (default), returns the latest subscription across all providers
-- (backward-compatible behavior).

CREATE OR REPLACE FUNCTION public.get_user_billing_subscription(
    p_user_id UUID,
    p_provider TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_row RECORD;
BEGIN
    IF p_provider IS NOT NULL THEN
        SELECT * INTO v_row
        FROM public.billing_subscriptions
        WHERE user_id = p_user_id AND provider = p_provider
        ORDER BY current_period_start DESC NULLS LAST, created_at DESC
        LIMIT 1;
    ELSE
        SELECT * INTO v_row
        FROM public.billing_subscriptions
        WHERE user_id = p_user_id
        ORDER BY current_period_start DESC NULLS LAST, created_at DESC
        LIMIT 1;
    END IF;

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

REVOKE EXECUTE ON FUNCTION public.get_user_billing_subscription(UUID, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_user_billing_subscription(UUID, TEXT) TO service_role;


-- ── get_user_billing_subscriptions — return all subs ────────────────────

CREATE OR REPLACE FUNCTION public.get_user_billing_subscriptions(
    p_user_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_result JSONB;
BEGIN
    SELECT COALESCE(jsonb_agg(
        jsonb_build_object(
            'user_id', bs.user_id,
            'provider', bs.provider,
            'provider_subscription_id', bs.provider_subscription_id,
            'provider_customer_id', bs.provider_customer_id,
            'offer_key', bs.offer_key,
            'plan', bs.plan,
            'status', bs.status,
            'current_period_start', bs.current_period_start,
            'current_period_end', bs.current_period_end,
            'cancel_at_period_end', bs.cancel_at_period_end,
            'interval', bs.interval,
            'interval_count', bs.interval_count,
            'metadata', bs.metadata
        )
        ORDER BY bs.current_period_start DESC NULLS LAST, bs.created_at DESC
    ), '[]'::JSONB) INTO v_result
    FROM public.billing_subscriptions bs
    WHERE bs.user_id = p_user_id;

    RETURN v_result;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_user_billing_subscriptions(UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_user_billing_subscriptions(UUID) TO service_role;


-- ── deactivate_other_provider_subscriptions — cancel prior provider subs ─

CREATE OR REPLACE FUNCTION public.deactivate_other_provider_subscriptions(
    p_user_id UUID,
    p_keep_provider TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE public.billing_subscriptions
    SET status = 'canceled',
        cancel_at_period_end = true,
        updated_at = now()
    WHERE user_id = p_user_id
      AND provider != p_keep_provider
      AND status IN ('active', 'trialing');

    GET DIAGNOSTICS v_count = ROW_COUNT;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'keep_provider', p_keep_provider,
        'deactivated_count', v_count
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.deactivate_other_provider_subscriptions(UUID, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.deactivate_other_provider_subscriptions(UUID, TEXT) TO service_role;

NOTIFY pgrst, 'reload schema';
