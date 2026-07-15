-- bursar: config-driven signup grants, data-only draft publish, scoped catalog sync,
-- auditable plan migration, and deferred performance changes.

-- ── Signup grant failures (observable diagnostics) ───────────────────────

CREATE TABLE IF NOT EXISTS public.signup_grant_failures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    error JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_signup_grant_failures_user
    ON public.signup_grant_failures (user_id, created_at DESC);

ALTER TABLE public.signup_grant_failures ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only signup_grant_failures'
          AND tablename = 'signup_grant_failures'
    ) THEN
        CREATE POLICY "Server-only signup_grant_failures"
            ON public.signup_grant_failures USING (false);
    END IF;
END;
$$;

-- ── grant_signup_bonus — read ledger.signup_grant { amount, bucket } ─────

CREATE OR REPLACE FUNCTION public.grant_signup_bonus()
RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_config JSONB;
  v_grant JSONB;
  v_bonus NUMERIC;
  v_bucket TEXT;
  v_result JSONB;
BEGIN
  SELECT config INTO v_config
  FROM public.bursar_config
  WHERE active = TRUE
  LIMIT 1;

  IF v_config IS NULL THEN
    RETURN NEW;
  END IF;

  v_grant := v_config #> '{ledger,signup_grant}';

  IF v_grant IS NULL OR jsonb_typeof(v_grant) <> 'object' THEN
    RETURN NEW;
  END IF;

  v_bonus := COALESCE((v_grant->>'amount')::numeric, 0);
  v_bucket := v_grant->>'bucket';

  IF v_bonus <= 0 OR v_bucket IS NULL OR v_bucket = '' THEN
    RETURN NEW;
  END IF;

  v_result := public.credits_add_internal(NEW.id, v_bonus, 'signup_bonus', NULL, v_bucket);

  IF v_result ? 'error' THEN
    INSERT INTO public.signup_grant_failures (user_id, error)
    VALUES (NEW.id, v_result);
    RAISE WARNING 'grant_signup_bonus failed for user %: %', NEW.id, v_result;
  END IF;

  RETURN NEW;
END;
$$;

-- ── publish_bursar_config — data-only draft (no live catalog mutation) ──

CREATE OR REPLACE FUNCTION public.publish_bursar_config(
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
    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM public.bursar_config;

    INSERT INTO public.bursar_config (config, active, version, label)
    VALUES (p_config, false, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', false
    );
END;
$$;

-- ── sync_buckets_from_config — snake_case only; retire on activation ───

CREATE OR REPLACE FUNCTION public.sync_buckets_from_config(
    p_config JSONB,
    p_config_version INTEGER DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_bucket_key TEXT;
    v_bucket_def JSONB;
    v_config_keys TEXT[];
    v_version INTEGER;
BEGIN
    v_version := COALESCE(
        p_config_version,
        (SELECT version FROM public.bursar_config WHERE active = true LIMIT 1),
        1
    );

    IF p_config #>> '{ledger,buckets}' IS NOT NULL
       AND jsonb_typeof(p_config #> '{ledger,buckets}') = 'object' THEN
        SELECT array_agg(k) INTO v_config_keys
        FROM jsonb_object_keys(p_config #> '{ledger,buckets}') k;

        IF v_config_keys IS NOT NULL THEN
            UPDATE public.credit_buckets
            SET status = 'retired', updated_at = now()
            WHERE bucket_key != ALL(v_config_keys)
              AND config_version = v_version
              AND status = 'active';
        END IF;

        FOR v_bucket_key, v_bucket_def IN
            SELECT * FROM jsonb_each(p_config #> '{ledger,buckets}')
        LOOP
            INSERT INTO public.credit_buckets (
                bucket_key, label, priority, expires, ttl_days,
                allow_overdraft, is_default, config_version, status
            )
            VALUES (
                v_bucket_key,
                COALESCE(v_bucket_def->>'label', v_bucket_key),
                COALESCE((v_bucket_def->>'priority')::INTEGER, 0),
                COALESCE(
                    (v_bucket_def->>'expires')::BOOLEAN,
                    (v_bucket_def->>'ttl_days')::INTEGER IS NOT NULL
                ),
                (v_bucket_def->>'ttl_days')::INTEGER,
                COALESCE((v_bucket_def->>'allow_overdraft')::BOOLEAN, false),
                COALESCE((v_bucket_def->>'default')::BOOLEAN, false),
                v_version,
                'active'
            )
            ON CONFLICT (bucket_key) DO UPDATE SET
                label = EXCLUDED.label,
                priority = EXCLUDED.priority,
                expires = EXCLUDED.expires,
                ttl_days = EXCLUDED.ttl_days,
                allow_overdraft = EXCLUDED.allow_overdraft,
                is_default = EXCLUDED.is_default,
                config_version = EXCLUDED.config_version,
                status = 'active',
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;

-- ── sync_plans_from_config — snake_case only ───────────────────────────

CREATE OR REPLACE FUNCTION public.sync_plans_from_config(
    p_config JSONB,
    p_config_version INTEGER
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_key TEXT;
    v_plan_def JSONB;
    v_config_keys TEXT[];
BEGIN
    IF p_config ? 'plans' AND jsonb_typeof(p_config->'plans') = 'object' THEN
        SELECT array_agg(k) INTO v_config_keys FROM jsonb_object_keys(p_config->'plans') k;

        IF v_config_keys IS NOT NULL THEN
            UPDATE public.credit_plans
            SET status = 'retired', updated_at = now()
            WHERE config_version = p_config_version
              AND plan_key IS NOT NULL
              AND plan_key != ALL(v_config_keys)
              AND status = 'active';
        END IF;

        FOR v_plan_key, v_plan_def IN SELECT * FROM jsonb_each(p_config->'plans')
        LOOP
            INSERT INTO public.credit_plans (
                plan_key, config_version, label, allowance_amount, rate_overrides,
                entitlements, billing_mode, per_operation, max_concurrent,
                overdraft_floor, allowance_period, status
            )
            VALUES (
                v_plan_key,
                p_config_version,
                v_plan_def->>'label',
                COALESCE((v_plan_def #>> '{allowance,amount}')::NUMERIC, 0),
                COALESCE(v_plan_def->'rate_overrides', '{}'::jsonb),
                COALESCE(v_plan_def->'entitlements', '{}'::jsonb),
                COALESCE(v_plan_def #>> '{safety,billing_mode}', 'strict'),
                v_plan_def #> '{safety,per_operation}',
                (v_plan_def #>> '{safety,max_concurrent}')::INTEGER,
                (v_plan_def #>> '{safety,overdraft_floor}')::NUMERIC,
                COALESCE(v_plan_def #>> '{allowance,period}', 'calendar_month'),
                'active'
            )
            ON CONFLICT (plan_key, config_version) WHERE plan_key IS NOT NULL
            DO UPDATE SET
                label = EXCLUDED.label,
                allowance_amount = EXCLUDED.allowance_amount,
                rate_overrides = EXCLUDED.rate_overrides,
                entitlements = EXCLUDED.entitlements,
                billing_mode = EXCLUDED.billing_mode,
                per_operation = EXCLUDED.per_operation,
                max_concurrent = EXCLUDED.max_concurrent,
                overdraft_floor = EXCLUDED.overdraft_floor,
                allowance_period = EXCLUDED.allowance_period,
                status = 'active',
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;

-- ── sync_billing_from_config — require explicit deposit_to / grant bucket ─

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
                v_item->>'deposit_to',
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

-- ── migrate_plan_users — capture source rows before update ───────────────

CREATE OR REPLACE FUNCTION public.migrate_plan_users(
    p_plan_key TEXT,
    p_target_config_version INTEGER DEFAULT NULL,
    p_from_plan_key TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_target_plan_id UUID;
    v_target_version INTEGER;
    v_count INTEGER;
    v_from_key TEXT;
BEGIN
    v_from_key := COALESCE(p_from_plan_key, p_plan_key);

    IF p_target_config_version IS NOT NULL THEN
        SELECT id, config_version INTO v_target_plan_id, v_target_version
        FROM public.credit_plans
        WHERE plan_key = p_plan_key AND config_version = p_target_config_version;
    ELSE
        SELECT id, config_version INTO v_target_plan_id, v_target_version
        FROM public.credit_plans
        WHERE plan_key = p_plan_key
        ORDER BY config_version DESC
        LIMIT 1;
    END IF;

    IF v_target_plan_id IS NULL THEN
        RETURN jsonb_build_object('error', 'plan_not_found');
    END IF;

    CREATE TEMP TABLE _migrate_sources ON COMMIT DROP AS
    SELECT uc.user_id, uc.plan_id AS from_plan_id, uc.catalog_version AS from_config_version
    FROM public.user_credits uc
    JOIN public.credit_plans cp ON cp.id = uc.plan_id
    WHERE cp.plan_key = v_from_key AND cp.id != v_target_plan_id;

    INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
    SELECT uw.user_id, v_target_plan_id, uw.billing_period, uw.usage
    FROM public.credit_usage_window uw
    JOIN _migrate_sources src ON src.user_id = uw.user_id
    JOIN public.credit_plans cp ON cp.id = uw.plan_id
    WHERE cp.plan_key = v_from_key
    ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
        usage = GREATEST(public.credit_usage_window.usage, EXCLUDED.usage),
        updated_at = now();

    UPDATE public.user_credits uc
    SET plan_id = v_target_plan_id,
        catalog_version = v_target_version,
        updated_at = now()
    FROM _migrate_sources src
    WHERE uc.user_id = src.user_id;

    GET DIAGNOSTICS v_count = ROW_COUNT;

    INSERT INTO public.credit_plan_migrations (
        user_id, from_plan_id, to_plan_id, from_config_version, to_config_version, reason
    )
    SELECT src.user_id, src.from_plan_id, v_target_plan_id, src.from_config_version,
           v_target_version, 'migrate_plan_users'
    FROM _migrate_sources src;

    RETURN jsonb_build_object(
        'plan_key', p_plan_key,
        'from_plan_key', v_from_key,
        'target_plan_id', v_target_plan_id,
        'target_config_version', v_target_version,
        'migrated_count', v_count
    );
END;
$$;

-- ── Defer 025 performance changes until benchmark evidence exists ────────

DROP INDEX IF EXISTS idx_credit_transactions_usage_feature_created;
DROP INDEX IF EXISTS idx_credit_transactions_team_usage_created;

CREATE OR REPLACE FUNCTION public.list_user_transactions(
  p_user_id UUID,
  p_types TEXT[] DEFAULT NULL,
  p_from_date TIMESTAMPTZ DEFAULT NULL,
  p_to_date TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 50,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE (
  id UUID,
  user_id UUID,
  amount NUMERIC,
  type TEXT,
  reference_type TEXT,
  reference_id UUID,
  metadata JSONB,
  created_at TIMESTAMPTZ,
  total_count BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_total BIGINT;
BEGIN
  SELECT COUNT(*) INTO v_total
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND (p_types IS NULL OR ct.type::TEXT = ANY(p_types))
    AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
    AND (p_to_date IS NULL OR ct.created_at < p_to_date);

  RETURN QUERY
  SELECT
    ct.id,
    ct.user_id,
    ct.amount,
    ct.type::TEXT,
    ct.reference_type,
    ct.reference_id,
    ct.metadata,
    ct.created_at,
    v_total AS total_count
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND (p_types IS NULL OR ct.type::TEXT = ANY(p_types))
    AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
    AND (p_to_date IS NULL OR ct.created_at < p_to_date)
  ORDER BY ct.created_at DESC
  LIMIT p_limit
  OFFSET p_offset;
END;
$$;

NOTIFY pgrst, 'reload schema';
