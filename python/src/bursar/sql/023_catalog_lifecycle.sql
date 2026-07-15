-- bursar: catalog lifecycle — plan status, migration audit, bucket archival,
-- draft publish, cross-plan migration, subscription catalog linkage.

-- ── Plan lifecycle status ────────────────────────────────────────────────

ALTER TABLE public.credit_plans ADD COLUMN IF NOT EXISTS status TEXT
    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired'));

-- ── Bucket lifecycle status ──────────────────────────────────────────────

ALTER TABLE public.credit_buckets ADD COLUMN IF NOT EXISTS status TEXT
    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired'));

ALTER TABLE public.credit_buckets ADD COLUMN IF NOT EXISTS config_version INTEGER;

-- ── User catalog pin ─────────────────────────────────────────────────────

ALTER TABLE public.user_credits ADD COLUMN IF NOT EXISTS catalog_version INTEGER;

UPDATE public.user_credits uc
SET catalog_version = cp.config_version
FROM public.credit_plans cp
WHERE uc.plan_id = cp.id AND uc.catalog_version IS NULL;

-- ── Plan assignment / migration audit log ────────────────────────────────

CREATE TABLE IF NOT EXISTS public.credit_plan_migrations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.user_credits(user_id) ON DELETE CASCADE,
    from_plan_id UUID REFERENCES public.credit_plans(id),
    to_plan_id UUID NOT NULL REFERENCES public.credit_plans(id),
    from_config_version INTEGER,
    to_config_version INTEGER,
    effective_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credit_plan_migrations_user
    ON public.credit_plan_migrations (user_id, created_at DESC);

ALTER TABLE public.credit_plan_migrations ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE policyname = 'Server-only credit_plan_migrations'
        AND tablename = 'credit_plan_migrations'
    ) THEN
        CREATE POLICY "Server-only credit_plan_migrations"
            ON public.credit_plan_migrations USING (false);
    END IF;
END;
$$;

-- ── Billing subscription catalog linkage ─────────────────────────────────

ALTER TABLE public.billing_subscriptions ADD COLUMN IF NOT EXISTS catalog_version INTEGER;
ALTER TABLE public.billing_subscriptions ADD COLUMN IF NOT EXISTS plan_version_id UUID
    REFERENCES public.credit_plans(id);

-- ── Pricing config: allow inactive drafts ────────────────────────────────
-- (active flag already exists; publish_bursar_config inserts inactive rows)

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

    PERFORM public.sync_plans_from_config(p_config, v_next_version);
    PERFORM public.sync_buckets_from_config(p_config, v_next_version);
    PERFORM public.sync_billing_from_config(p_config->'billing');

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', false
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.publish_bursar_config(JSONB, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_bursar_config(JSONB, TEXT) TO service_role;

-- ── sync_buckets_from_config — version + retire absent buckets ───────────

DO $$ DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'sync_buckets_from_config' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

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
            WHERE bucket_key != ALL(v_config_keys) AND status = 'active';
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
                    COALESCE(
                        (v_bucket_def->>'ttlDays')::INTEGER,
                        (v_bucket_def->>'ttl_days')::INTEGER
                    ) IS NOT NULL
                ),
                COALESCE(
                    (v_bucket_def->>'ttlDays')::INTEGER,
                    (v_bucket_def->>'ttl_days')::INTEGER
                ),
                COALESCE(
                    (v_bucket_def->>'allowOverdraft')::BOOLEAN,
                    (v_bucket_def->>'allow_overdraft')::BOOLEAN,
                    false
                ),
                COALESCE(
                    (v_bucket_def->>'isDefaultBucket')::BOOLEAN,
                    (v_bucket_def->>'is_default_bucket')::BOOLEAN,
                    (v_bucket_def->>'default')::BOOLEAN,
                    false
                ),
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

REVOKE EXECUTE ON FUNCTION public.sync_buckets_from_config(JSONB, INTEGER) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sync_buckets_from_config(JSONB, INTEGER) TO service_role;

-- Update set_active_bursar_config to pass version to sync_buckets
CREATE OR REPLACE FUNCTION public.set_active_bursar_config(
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

    UPDATE public.bursar_config SET active = false WHERE active = true;

    INSERT INTO public.bursar_config (config, active, version, label)
    VALUES (p_config, true, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    PERFORM public.sync_plans_from_config(p_config, v_next_version);
    PERFORM public.sync_buckets_from_config(p_config, v_next_version);
    PERFORM public.sync_billing_from_config(p_config->'billing');

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true
    );
END;
$$;

-- Update activate_bursar_config bucket sync call
CREATE OR REPLACE FUNCTION public.activate_bursar_config(p_version INTEGER)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_target_id UUID;
    v_config JSONB;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));

    SELECT id, config INTO v_target_id, v_config
    FROM public.bursar_config
    WHERE version = p_version;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'version_not_found');
    END IF;

    UPDATE public.bursar_config SET active = false WHERE active = true;

    UPDATE public.bursar_config SET active = true
    WHERE version = p_version
    RETURNING id INTO v_target_id;

    PERFORM public.sync_plans_from_config(v_config, p_version);
    PERFORM public.sync_buckets_from_config(v_config, p_version);
    PERFORM public.sync_billing_from_config(v_config->'billing');

    RETURN jsonb_build_object(
        'id', v_target_id,
        'version', p_version,
        'active', true
    );
END;
$$;

-- ── sync_plans_from_config — mark retired plans absent from config ───────

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
                COALESCE(v_plan_def->'rate_overrides', v_plan_def->'rateOverrides', '{}'::jsonb),
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

-- ── set_user_plan — active catalog only unless grandfathered ───────────

DO $$ DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'set_user_plan' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.set_user_plan(
    p_user_id UUID,
    p_plan_key TEXT,
    p_plan_assigned_at TIMESTAMPTZ DEFAULT NULL,
    p_config_version INTEGER DEFAULT NULL,
    p_allow_grandfathered BOOLEAN DEFAULT false
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_id UUID;
    v_assigned_at TIMESTAMPTZ;
    v_catalog_version INTEGER;
    v_old_plan_id UUID;
    v_old_catalog_version INTEGER;
BEGIN
    IF p_config_version IS NOT NULL THEN
        SELECT id INTO v_plan_id
        FROM public.credit_plans
        WHERE plan_key = p_plan_key AND config_version = p_config_version;
    ELSE
        SELECT cp.id, cp.config_version INTO v_plan_id, v_catalog_version
        FROM public.credit_plans cp
        WHERE cp.plan_key = p_plan_key
          AND cp.config_version = (
              SELECT version FROM public.bursar_config WHERE active = true LIMIT 1
          )
          AND cp.status = 'active';

        IF v_plan_id IS NULL AND p_allow_grandfathered THEN
            SELECT id, config_version INTO v_plan_id, v_catalog_version
            FROM public.credit_plans
            WHERE plan_key = p_plan_key AND status = 'active'
            ORDER BY config_version DESC
            LIMIT 1;
        END IF;
    END IF;

    IF v_plan_id IS NULL THEN
        RETURN jsonb_build_object('error', 'plan_not_found');
    END IF;

    SELECT config_version INTO v_catalog_version
    FROM public.credit_plans WHERE id = v_plan_id;

    v_assigned_at := COALESCE(p_plan_assigned_at, now());

    SELECT plan_id, catalog_version
    INTO v_old_plan_id, v_old_catalog_version
    FROM public.user_credits
    WHERE user_id = p_user_id;

    INSERT INTO public.user_credits (user_id, plan_id, plan_assigned_at, catalog_version)
    VALUES (p_user_id, v_plan_id, v_assigned_at, v_catalog_version)
    ON CONFLICT (user_id) DO UPDATE SET
        plan_id = v_plan_id,
        plan_assigned_at = v_assigned_at,
        catalog_version = v_catalog_version,
        updated_at = now();

    IF v_old_plan_id IS DISTINCT FROM v_plan_id THEN
        INSERT INTO public.credit_plan_migrations (
            user_id, from_plan_id, to_plan_id, from_config_version, to_config_version, reason
        ) VALUES (
            p_user_id, v_old_plan_id, v_plan_id, v_old_catalog_version, v_catalog_version, 'set_user_plan'
        );
    END IF;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_assigned_at', v_assigned_at,
        'catalog_version', v_catalog_version
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.set_user_plan(UUID, TEXT, TIMESTAMPTZ, INTEGER, BOOLEAN)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.set_user_plan(UUID, TEXT, TIMESTAMPTZ, INTEGER, BOOLEAN) TO service_role;

-- ── migrate_plan_users — preserve usage within period ────────────────────

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

    -- Carry forward in-period usage to the target plan row.
    INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
    SELECT uw.user_id, v_target_plan_id, uw.billing_period, uw.usage
    FROM public.credit_usage_window uw
    JOIN public.user_credits uc ON uc.user_id = uw.user_id
    JOIN public.credit_plans cp ON cp.id = uc.plan_id
    WHERE cp.plan_key = v_from_key AND cp.id != v_target_plan_id
    ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
        usage = GREATEST(public.credit_usage_window.usage, EXCLUDED.usage),
        updated_at = now();

    UPDATE public.user_credits uc
    SET plan_id = v_target_plan_id,
        catalog_version = v_target_version,
        updated_at = now()
    WHERE plan_id IN (
        SELECT id FROM public.credit_plans
        WHERE plan_key = v_from_key AND id != v_target_plan_id
    );

    GET DIAGNOSTICS v_count = ROW_COUNT;

    INSERT INTO public.credit_plan_migrations (
        user_id, from_plan_id, to_plan_id, to_config_version, reason
    )
    SELECT uc.user_id, cp_old.id, v_target_plan_id, v_target_version, 'migrate_plan_users'
    FROM public.user_credits uc
    JOIN public.credit_plans cp_old ON cp_old.plan_key = v_from_key AND cp_old.id != v_target_plan_id
    WHERE uc.plan_id = v_target_plan_id;

    RETURN jsonb_build_object(
        'plan_key', p_plan_key,
        'from_plan_key', v_from_key,
        'target_plan_id', v_target_plan_id,
        'target_config_version', v_target_version,
        'migrated_count', v_count
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.migrate_plan_users(TEXT, INTEGER, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.migrate_plan_users(TEXT, INTEGER, TEXT) TO service_role;

-- ── get_user_plan — include rate_overrides + catalog_version ─────────────

CREATE OR REPLACE FUNCTION public.get_user_plan(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_id UUID;
    v_plan_label TEXT;
    v_allowance_amount NUMERIC;
    v_entitlements JSONB;
    v_rate_overrides JSONB;
    v_billing_mode TEXT;
    v_per_operation JSONB;
    v_max_concurrent INTEGER;
    v_overdraft_floor NUMERIC;
    v_allowance_period TEXT;
    v_plan_assigned_at TIMESTAMPTZ;
    v_config_version INTEGER;
    v_catalog_version INTEGER;
BEGIN
    SELECT uc.plan_id, cp.label, cp.allowance_amount, cp.entitlements, cp.rate_overrides,
           cp.billing_mode, cp.per_operation, cp.max_concurrent, cp.overdraft_floor,
           cp.allowance_period, uc.plan_assigned_at, cp.config_version, uc.catalog_version
    INTO v_plan_id, v_plan_label, v_allowance_amount, v_entitlements, v_rate_overrides,
         v_billing_mode, v_per_operation, v_max_concurrent, v_overdraft_floor,
         v_allowance_period, v_plan_assigned_at, v_config_version, v_catalog_version
    FROM public.user_credits uc
    LEFT JOIN public.credit_plans cp ON cp.id = uc.plan_id
    WHERE uc.user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_label', v_plan_label,
        'allowance_amount', COALESCE(v_allowance_amount, 0),
        'entitlements', COALESCE(v_entitlements, '{}'::jsonb),
        'rate_overrides', COALESCE(v_rate_overrides, '{}'::jsonb),
        'billing_mode', COALESCE(v_billing_mode, 'strict'),
        'per_operation', COALESCE(v_per_operation, '{}'::jsonb),
        'max_concurrent', v_max_concurrent,
        'overdraft_floor', v_overdraft_floor,
        'allowance_period', COALESCE(v_allowance_period, 'calendar_month'),
        'plan_assigned_at', v_plan_assigned_at,
        'config_version', v_config_version,
        'catalog_version', COALESCE(v_catalog_version, v_config_version)
    );
END;
$$;

NOTIFY pgrst, 'reload schema';
