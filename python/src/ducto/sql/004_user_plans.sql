-- ducto: subscription plan support.
-- credit_plans table, plan_id on user_credits, usage window for allowance tracking.

CREATE TABLE IF NOT EXISTS public.credit_plans (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    free_allowance INTEGER NOT NULL DEFAULT 0,
    rate_overrides JSONB DEFAULT '{}'::jsonb,
    features JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.credit_usage_window (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.user_credits(user_id),
    plan_id UUID NOT NULL REFERENCES public.credit_plans(id),
    billing_period DATE NOT NULL,
    usage INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credit_usage_window_plan_id ON public.credit_usage_window (plan_id);

-- One usage window per user/plan/period
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_usage_window_unique
    ON public.credit_usage_window (user_id, plan_id, billing_period);

-- Add plan_id to user_credits
ALTER TABLE public.user_credits ADD COLUMN IF NOT EXISTS plan_id UUID REFERENCES public.credit_plans(id);

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

-- get_user_plan: Fetch user's current plan (if any).
CREATE OR REPLACE FUNCTION public.get_user_plan(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_id UUID;
    v_plan_name TEXT;
    v_free_allowance INTEGER;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    SELECT uc.plan_id, cp.name, cp.free_allowance
    INTO v_plan_id, v_plan_name, v_free_allowance
    FROM public.user_credits uc
    LEFT JOIN public.credit_plans cp ON cp.id = uc.plan_id
    WHERE uc.user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_name', v_plan_name,
        'free_allowance', COALESCE(v_free_allowance, 0)
    );
END;
$$;

-- set_user_plan: Assign a plan to a user (upsert).
CREATE OR REPLACE FUNCTION public.set_user_plan(
    p_user_id UUID,
    p_plan_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    INSERT INTO public.user_credits (user_id, plan_id)
    VALUES (p_user_id, p_plan_id)
    ON CONFLICT (user_id) DO UPDATE SET
        plan_id = p_plan_id,
        updated_at = now();

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', p_plan_id
    );
END;
$$;

-- check_plan_allowance: Get remaining free allowance for current billing period.
CREATE OR REPLACE FUNCTION public.check_plan_allowance(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_id UUID;
    v_free_allowance INTEGER;
    v_current_usage INTEGER;
    v_period_start DATE;
    v_period_end DATE;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    -- Get user's plan
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

    -- Calculate current billing period (monthly)
    v_period_start := date_trunc('month', now())::DATE;
    v_period_end := (date_trunc('month', now()) + interval '1 month' - interval '1 day')::DATE;

    -- Get current usage this period
    SELECT COALESCE(usage, 0) INTO v_current_usage
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

-- increment_usage_window: Record allowance consumption for current billing period.
CREATE OR REPLACE FUNCTION public.increment_usage_window(
    p_user_id UUID,
    p_plan_id UUID,
    p_amount INTEGER
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_period_start DATE;
    v_new_usage INTEGER;
BEGIN
    IF p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    v_period_start := date_trunc('month', now())::DATE;

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

REVOKE EXECUTE ON FUNCTION public.get_user_plan FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.set_user_plan FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.check_plan_allowance FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.increment_usage_window FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
