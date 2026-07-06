-- bursar: subscription plan support.
-- credit_plans table, plan_id/plan_assigned_at on user_credits, usage window
-- for allowance tracking, and financial-safety policy columns
-- (default_billing_mode / per_operation / max_concurrent / overdraft_floor)
-- consumed by the atomic deduct/lease RPCs (009_deduct_and_leases.sql).

CREATE TABLE IF NOT EXISTS public.credit_plans (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    free_allowance NUMERIC(18,4) NOT NULL DEFAULT 0,
    rate_overrides JSONB DEFAULT '{}'::jsonb,
    features JSONB DEFAULT '{}'::jsonb,
    feature_limits JSONB DEFAULT '{}'::jsonb,
    plan_key TEXT,
    default_billing_mode TEXT NOT NULL DEFAULT 'strict',
    per_operation JSONB,
    max_concurrent INTEGER,
    overdraft_floor NUMERIC(18,4),
    allowance_period TEXT NOT NULL DEFAULT 'calendar_month',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- plan_key lets plans defined in pricing config be referenced by
-- human-readable keys (e.g. "pro", "enterprise") instead of opaque UUIDs.
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_plans_plan_key
    ON public.credit_plans (plan_key)
    WHERE plan_key IS NOT NULL;

-- feature_limits (per-feature invocation-count limits, 012_feature_limits.sql)
-- added after credit_plans first shipped — backfill via ALTER for existing
-- installs, since CREATE TABLE IF NOT EXISTS is a no-op once the table exists.
ALTER TABLE public.credit_plans ADD COLUMN IF NOT EXISTS feature_limits JSONB DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS public.credit_usage_window (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.user_credits(user_id),
    plan_id UUID NOT NULL REFERENCES public.credit_plans(id),
    billing_period DATE NOT NULL,
    usage NUMERIC(18,4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credit_usage_window_plan_id ON public.credit_usage_window (plan_id);

-- One usage window per user/plan/period
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_usage_window_unique
    ON public.credit_usage_window (user_id, plan_id, billing_period);

-- Add plan_id / plan_assigned_at to user_credits.
ALTER TABLE public.user_credits ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES public.credit_plans(id);
ALTER TABLE public.user_credits ADD COLUMN IF NOT EXISTS plan_assigned_at TIMESTAMPTZ;

UPDATE public.user_credits
SET plan_assigned_at = created_at
WHERE plan_id IS NOT NULL AND plan_assigned_at IS NULL;

-- RLS: server-only access (managed through RPCs)
ALTER TABLE public.credit_plans ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only credit_plans' AND tablename = 'credit_plans') THEN
        CREATE POLICY "Server-only credit_plans" ON public.credit_plans USING (false);
    END IF;
END;
$$;

ALTER TABLE public.credit_usage_window ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only credit_usage_window' AND tablename = 'credit_usage_window') THEN
        CREATE POLICY "Server-only credit_usage_window" ON public.credit_usage_window USING (false);
    END IF;
END;
$$;

-- sync_plans_from_config: upsert plan definitions (incl. financial-safety
-- policy and allowance_period) into credit_plans from the pricing config JSONB.
CREATE OR REPLACE FUNCTION public.sync_plans_from_config(p_config JSONB)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_key TEXT;
    v_plan_def JSONB;
BEGIN
    IF p_config ? 'plans' AND jsonb_typeof(p_config->'plans') = 'object' THEN
        FOR v_plan_key, v_plan_def IN SELECT * FROM jsonb_each(p_config->'plans')
        LOOP
            INSERT INTO public.credit_plans (
                plan_key, name, free_allowance, rate_overrides, features, feature_limits,
                default_billing_mode, per_operation, max_concurrent, overdraft_floor,
                allowance_period
            )
            VALUES (
                v_plan_key,
                v_plan_def->>'name',
                COALESCE((v_plan_def->>'free_allowance')::NUMERIC, (v_plan_def->>'freeAllowance')::NUMERIC, 0),
                COALESCE(v_plan_def->'rate_overrides', v_plan_def->'rateOverrides', '{}'::jsonb),
                COALESCE(v_plan_def->'features', '{}'::jsonb),
                COALESCE(v_plan_def->'feature_limits', v_plan_def->'featureLimits', '{}'::jsonb),
                COALESCE(v_plan_def->>'default_billing_mode', v_plan_def->>'defaultBillingMode', 'strict'),
                COALESCE(v_plan_def->'per_operation', v_plan_def->'perOperation'),
                COALESCE((v_plan_def->>'max_concurrent')::INTEGER, (v_plan_def->>'maxConcurrent')::INTEGER),
                COALESCE((v_plan_def->>'overdraft_floor')::NUMERIC, (v_plan_def->>'overdraftFloor')::NUMERIC),
                COALESCE(v_plan_def->>'allowance_period', v_plan_def->>'allowancePeriod', 'calendar_month')
            )
            ON CONFLICT (plan_key) WHERE plan_key IS NOT NULL
            DO UPDATE SET
                name = EXCLUDED.name,
                free_allowance = EXCLUDED.free_allowance,
                rate_overrides = EXCLUDED.rate_overrides,
                features = EXCLUDED.features,
                feature_limits = EXCLUDED.feature_limits,
                default_billing_mode = EXCLUDED.default_billing_mode,
                per_operation = EXCLUDED.per_operation,
                max_concurrent = EXCLUDED.max_concurrent,
                overdraft_floor = EXCLUDED.overdraft_floor,
                allowance_period = EXCLUDED.allowance_period,
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sync_plans_from_config(JSONB) FROM PUBLIC, anon, authenticated;

-- get_user_plan: Fetch user's current plan, including policy + allowance-window fields.
CREATE OR REPLACE FUNCTION public.get_user_plan(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_id UUID;
    v_plan_name TEXT;
    v_free_allowance NUMERIC;
    v_features JSONB;
    v_feature_limits JSONB;
    v_billing_mode TEXT;
    v_per_operation JSONB;
    v_max_concurrent INTEGER;
    v_overdraft_floor NUMERIC;
    v_allowance_period TEXT;
    v_plan_assigned_at TIMESTAMPTZ;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    SELECT uc.plan_id, cp.name, cp.free_allowance, cp.features, cp.feature_limits,
           cp.default_billing_mode, cp.per_operation, cp.max_concurrent, cp.overdraft_floor,
           cp.allowance_period, uc.plan_assigned_at
    INTO v_plan_id, v_plan_name, v_free_allowance, v_features, v_feature_limits,
         v_billing_mode, v_per_operation, v_max_concurrent, v_overdraft_floor,
         v_allowance_period, v_plan_assigned_at
    FROM public.user_credits uc
    LEFT JOIN public.credit_plans cp ON cp.id = uc.plan_id
    WHERE uc.user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_name', v_plan_name,
        'free_allowance', COALESCE(v_free_allowance, 0),
        'features', COALESCE(v_features, '{}'::jsonb),
        'feature_limits', COALESCE(v_feature_limits, '{}'::jsonb),
        'default_billing_mode', COALESCE(v_billing_mode, 'strict'),
        'per_operation', COALESCE(v_per_operation, '{}'::jsonb),
        'max_concurrent', v_max_concurrent,
        'overdraft_floor', v_overdraft_floor,
        'allowance_period', COALESCE(v_allowance_period, 'calendar_month'),
        'plan_assigned_at', v_plan_assigned_at
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_user_plan(UUID) FROM PUBLIC, anon, authenticated;

-- set_user_plan gained a plan_key (TEXT) parameter in place of the original
-- UUID param. Drop every overload by name first so the current signature is
-- unambiguous (no-op on fresh installs).
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'set_user_plan' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

-- set_user_plan: Assign a plan to a user by plan_key (upsert). Every
-- (re-)assignment re-anchors plan_assigned_at, which the allowance-window
-- resolution logic (rolling_30d / anniversary) uses as its epoch.
CREATE OR REPLACE FUNCTION public.set_user_plan(
    p_user_id UUID,
    p_plan_key TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Resolve plan_key to credit_plans UUID
    SELECT id INTO v_plan_id
    FROM public.credit_plans
    WHERE plan_key = p_plan_key;

    IF v_plan_id IS NULL THEN
        RETURN jsonb_build_object('error', 'plan_not_found');
    END IF;

    INSERT INTO public.user_credits (user_id, plan_id, plan_assigned_at)
    VALUES (p_user_id, v_plan_id, now())
    ON CONFLICT (user_id) DO UPDATE SET
        plan_id = v_plan_id,
        plan_assigned_at = now(),
        updated_at = now();

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id
    );
END;
$$;

-- unset_user_plan: Clear a user's plan (pauses the allowance period).
-- plan_assigned_at is cleared so rolling_30d / anniversary windows don't advance.
-- Call set_user_plan again to re-assign and re-anchor.
CREATE OR REPLACE FUNCTION public.unset_user_plan(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    UPDATE public.user_credits
    SET plan_id = NULL,
        plan_assigned_at = NULL,
        updated_at = now()
    WHERE user_id = p_user_id;

    RETURN jsonb_build_object('user_id', p_user_id);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.set_user_plan(UUID, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.unset_user_plan(UUID) FROM PUBLIC, anon, authenticated;

-- check_plan_allowance gained a trailing p_period_start DATE parameter. Drop
-- every overload by name first so the current signature is unambiguous.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'check_plan_allowance' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

-- check_plan_allowance: Get remaining free allowance for the given period
-- (falls back to the current UTC calendar month when p_period_start is NULL —
-- the regression-safety baseline for plans still on allowance_period =
-- "calendar_month"). When supplied (rolling_30d/anniversary, resolved by the
-- manager/store layer), usage is keyed on it directly; period_end here is
-- generic "not this period" display only.
CREATE OR REPLACE FUNCTION public.check_plan_allowance(
    p_user_id UUID,
    p_period_start DATE DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_id UUID;
    v_free_allowance NUMERIC;
    v_current_usage NUMERIC;
    v_period_start DATE;
    v_period_end DATE;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    SELECT uc.plan_id, cp.free_allowance
    INTO v_plan_id, v_free_allowance
    FROM public.user_credits uc
    LEFT JOIN public.credit_plans cp ON cp.id = uc.plan_id
    WHERE uc.user_id = p_user_id;

    IF v_plan_id IS NULL THEN
        RETURN jsonb_build_object(
            'plan_id', NULL::UUID,
            'allowance_remaining', 0,
            'period_start', NULL::TEXT,
            'period_end', NULL::TEXT
        );
    END IF;

    v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
    v_period_end := (date_trunc('month', now() AT TIME ZONE 'UTC') + interval '1 month' - interval '1 day')::DATE;

    SELECT COALESCE(SUM(usage), 0) INTO v_current_usage
    FROM public.credit_usage_window
    WHERE user_id = p_user_id
      AND plan_id = v_plan_id
      AND billing_period = v_period_start;

    RETURN jsonb_build_object(
        'plan_id', v_plan_id,
        'allowance_remaining', GREATEST(v_free_allowance - v_current_usage, 0),
        'period_start', v_period_start::TEXT,
        'period_end', v_period_end::TEXT
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.check_plan_allowance(UUID, DATE) FROM PUBLIC, anon, authenticated;

-- increment_usage_window gained a trailing p_period_start DATE parameter.
-- Drop every overload by name first so the current signature is unambiguous.
-- NOTE: this RPC is NOT called from any of the atomic debit/lease RPCs
-- (deduct_with_allowance / settle_lease / create_lease all consume allowance
-- inline via a direct INSERT ... ON CONFLICT into credit_usage_window). It is
-- effectively dead on the hot paths — kept for API/signature consistency with
-- postgres.py / supabase.py's public increment_usage_window() store method.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'increment_usage_window' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

-- increment_usage_window: Record allowance consumption for the given period.
CREATE OR REPLACE FUNCTION public.increment_usage_window(
    p_user_id UUID,
    p_plan_id UUID,
    p_amount NUMERIC,
    p_period_start DATE DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_period_start DATE;
    v_new_usage NUMERIC;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);

    INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
    VALUES (p_user_id, p_plan_id, v_period_start, p_amount)
    ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
        usage = public.credit_usage_window.usage + p_amount,
        updated_at = now()
    RETURNING usage INTO v_new_usage;

    RETURN jsonb_build_object(
        'usage', v_new_usage,
        'period_start', v_period_start::TEXT
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.increment_usage_window(UUID, UUID, NUMERIC, DATE) FROM PUBLIC, anon, authenticated;

NOTIFY pgrst, 'reload schema';
