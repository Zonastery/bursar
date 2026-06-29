-- ducto: feature entitlements for plan-based gating.
--
-- Adds a plan_key column so plans defined in pricing config can be
-- referenced by human-readable keys (e.g. "pro", "enterprise") instead
-- of opaque UUIDs.  set_active_pricing_config() now automatically syncs
-- plan definitions into credit_plans so that get_user_plan() returns
-- features and check_feature() works out of the box.

-- ── Schema migration ───────────────────────────────────────────────

ALTER TABLE public.credit_plans ADD COLUMN IF NOT EXISTS plan_key TEXT;

-- Drop any old constraint that might exist from a prior version
ALTER TABLE public.credit_plans DROP CONSTRAINT IF EXISTS uq_credit_plans_plan_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_plans_plan_key
    ON public.credit_plans (plan_key)
    WHERE plan_key IS NOT NULL;


-- ── sync_plans_from_config: upsert plan definitions into credit_plans ─

CREATE OR REPLACE FUNCTION public.sync_plans_from_config(p_config JSONB)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_plan_key TEXT;
    v_plan_def JSONB;
    v_plan_id UUID;
BEGIN
    IF p_config ? 'plans' AND jsonb_typeof(p_config->'plans') = 'object' THEN
        FOR v_plan_key, v_plan_def IN SELECT * FROM jsonb_each(p_config->'plans')
        LOOP
            -- Upsert: match on plan_key, generate UUID on first insert
            INSERT INTO public.credit_plans (plan_key, name, free_allowance, rate_overrides, features)
            VALUES (
                v_plan_key,
                v_plan_def->>'name',
                COALESCE(
                    (v_plan_def->>'free_allowance')::NUMERIC,
                    (v_plan_def->>'freeAllowance')::NUMERIC,
                    0
                ),
                COALESCE(
                    v_plan_def->'rate_overrides',
                    v_plan_def->'rateOverrides',
                    '{}'::jsonb
                ),
                COALESCE(v_plan_def->'features', '{}'::jsonb)
            )
            ON CONFLICT (plan_key) WHERE plan_key IS NOT NULL
            DO UPDATE SET
                name = EXCLUDED.name,
                free_allowance = EXCLUDED.free_allowance,
                rate_overrides = EXCLUDED.rate_overrides,
                features = EXCLUDED.features,
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sync_plans_from_config(JSONB) FROM anon, authenticated;


-- ── Patch set_active_pricing_config to also sync plans ─────────────

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

    -- Serialize concurrent publishers so version assignment can't race (M14).
    PERFORM pg_advisory_xact_lock(hashtext('ducto_pricing_version'));

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM public.credit_pricing_config;

    -- Deactivate all existing active configs
    UPDATE public.credit_pricing_config SET active = false WHERE active = true;

    -- Insert new active config
    INSERT INTO public.credit_pricing_config (config, active, version, label)
    VALUES (p_config, true, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    -- Sync plan definitions into credit_plans table
    PERFORM public.sync_plans_from_config(p_config);

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true
    );
END;
$$;


-- ── Patch set_user_plan to accept plan_key (TEXT) instead of UUID ──

-- Drop old overload that accepted UUID so the new TEXT-based one is unique
DROP FUNCTION IF EXISTS public.set_user_plan(UUID, UUID);

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

    INSERT INTO public.user_credits (user_id, plan_id)
    VALUES (p_user_id, v_plan_id)
    ON CONFLICT (user_id) DO UPDATE SET
        plan_id = v_plan_id,
        updated_at = now();

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.set_active_pricing_config(JSONB, TEXT) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.set_user_plan(UUID, TEXT) FROM anon, authenticated;


-- ── Patch get_user_plan to include features ──────────────────────
--     The original 004_user_plans.sql does not return features from
--     the credit_plans JSONB column.  This migration patches it to
--     include features so that check_feature() works out of the box.

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
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    SELECT uc.plan_id, cp.name, cp.free_allowance, cp.features
    INTO v_plan_id, v_plan_name, v_free_allowance, v_features
    FROM public.user_credits uc
    LEFT JOIN public.credit_plans cp ON cp.id = uc.plan_id
    WHERE uc.user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_name', v_plan_name,
        'free_allowance', COALESCE(v_free_allowance, 0),
        'features', COALESCE(v_features, '{}'::jsonb)
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_user_plan(UUID) FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
