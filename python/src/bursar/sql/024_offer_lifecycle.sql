-- bursar: offer validity windows + subscription catalog linkage on upsert.

-- ── sync_billing_from_config — wire valid_from / valid_to ────────────────

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
    v_config_keys TEXT[];
BEGIN
    IF p_config ? 'subscriptions' AND jsonb_typeof(p_config->'subscriptions') = 'object' THEN
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(p_config->'subscriptions') k;

        IF v_config_keys IS NOT NULL THEN
            UPDATE public.billing_offers SET status = 'archived', updated_at = now()
            WHERE offer_key != ALL(v_config_keys) AND status = 'active';
        END IF;

        UPDATE public.billing_provider_refs SET active = false, updated_at = now()
        WHERE resource_type = 'offer';

        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'subscriptions')
        LOOP
            INSERT INTO public.billing_offers (
                offer_key, plan, interval, interval_count,
                grant_mode, grant_credits, grant_bucket, grant_replace_prior,
                valid_from, valid_to
            )
            VALUES (
                v_key,
                v_item->>'plan',
                COALESCE(v_item->>'interval', 'month'),
                COALESCE((v_item->>'interval_count')::INTEGER, 1),
                COALESCE(v_item#>>'{grant,mode}', 'allowance'),
                (v_item#>>'{grant,credits}')::INTEGER,
                v_item#>>'{grant,bucket}',
                COALESCE((v_item#>>'{grant,replace_prior}')::BOOLEAN, true),
                (v_item->>'valid_from')::TIMESTAMPTZ,
                (v_item->>'valid_to')::TIMESTAMPTZ
            )
            ON CONFLICT (offer_key) DO UPDATE SET
                plan = EXCLUDED.plan,
                interval = EXCLUDED.interval,
                interval_count = EXCLUDED.interval_count,
                grant_mode = EXCLUDED.grant_mode,
                grant_credits = EXCLUDED.grant_credits,
                grant_bucket = EXCLUDED.grant_bucket,
                grant_replace_prior = EXCLUDED.grant_replace_prior,
                valid_from = EXCLUDED.valid_from,
                valid_to = EXCLUDED.valid_to,
                status = 'active',
                updated_at = now();

            IF v_item ? 'providers' AND jsonb_typeof(v_item->'providers') = 'object' THEN
                FOR v_provider, v_ref IN SELECT * FROM jsonb_each(v_item->'providers')
                LOOP
                    PERFORM public._upsert_billing_provider_ref(
                        'offer', v_provider,
                        v_ref->>'price_id', v_ref->>'product_id',
                        v_ref->>'variant_id', v_ref->>'lookup_key',
                        v_key
                    );
                END LOOP;
            END IF;
        END LOOP;
    END IF;

    IF p_config ? 'topups' AND jsonb_typeof(p_config->'topups') = 'object' THEN
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(p_config->'topups') k;

        IF v_config_keys IS NOT NULL THEN
            UPDATE public.billing_credit_topups SET status = 'archived', updated_at = now()
            WHERE topup_key != ALL(v_config_keys) AND status = 'active';
        END IF;

        UPDATE public.billing_provider_refs SET active = false, updated_at = now()
        WHERE resource_type = 'topup';

        FOR v_key, v_item IN SELECT * FROM jsonb_each(p_config->'topups')
        LOOP
            INSERT INTO public.billing_credit_topups (
                topup_key, deposit_to, credits_per_unit,
                min_amount_minor, max_amount_minor, tax_behavior
            )
            VALUES (
                v_key,
                COALESCE(v_item->>'deposit_to', 'purchased'),
                COALESCE((v_item->>'credits_per_unit')::INTEGER, 1000),
                COALESCE((v_item->>'min_amount_minor')::INTEGER, 500),
                COALESCE((v_item->>'max_amount_minor')::INTEGER, 500000),
                COALESCE(v_item->>'tax_behavior', 'exclude_tax')
            )
            ON CONFLICT (topup_key) DO UPDATE SET
                deposit_to = EXCLUDED.deposit_to,
                credits_per_unit = EXCLUDED.credits_per_unit,
                min_amount_minor = EXCLUDED.min_amount_minor,
                max_amount_minor = EXCLUDED.max_amount_minor,
                tax_behavior = EXCLUDED.tax_behavior,
                status = 'active',
                updated_at = now();

            IF v_item ? 'providers' AND jsonb_typeof(v_item->'providers') = 'object' THEN
                FOR v_provider, v_ref IN SELECT * FROM jsonb_each(v_item->'providers')
                LOOP
                    PERFORM public._upsert_billing_provider_ref(
                        'topup', v_provider,
                        v_ref->>'price_id', v_ref->>'product_id',
                        v_ref->>'variant_id', v_ref->>'lookup_key',
                        v_key
                    );
                END LOOP;
            END IF;
        END LOOP;
    END IF;
END;
$$;

-- ── upsert_billing_subscription — record catalog linkage ────────────────

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
    v_catalog_version INTEGER;
    v_plan_version_id UUID;
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

    v_catalog_version := COALESCE(
        (p_state->>'catalog_version')::INTEGER,
        (SELECT version FROM public.bursar_config WHERE active = true LIMIT 1)
    );

    IF p_state->>'plan' IS NOT NULL AND v_catalog_version IS NOT NULL THEN
        SELECT id INTO v_plan_version_id
        FROM public.credit_plans
        WHERE plan_key = p_state->>'plan' AND config_version = v_catalog_version
        LIMIT 1;
    END IF;

    INSERT INTO public.billing_subscriptions (
        user_id, provider, provider_subscription_id, provider_customer_id,
        offer_key, plan, status, current_period_start,
        current_period_end, cancel_at_period_end, interval, interval_count,
        metadata, catalog_version, plan_version_id
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
        COALESCE((p_state->>'metadata')::JSONB, '{}'::jsonb),
        v_catalog_version,
        v_plan_version_id
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
        catalog_version = COALESCE(EXCLUDED.catalog_version, billing_subscriptions.catalog_version),
        plan_version_id = COALESCE(EXCLUDED.plan_version_id, billing_subscriptions.plan_version_id),
        updated_at = now();

    RETURN jsonb_build_object('status', 'ok');
END;
$$;

NOTIFY pgrst, 'reload schema';
