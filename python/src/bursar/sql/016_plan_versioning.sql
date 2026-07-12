-- bursar: plan versioning — immutable plan rows per config version.
--
-- Each call to set_active_pricing_config (or activate_pricing_config) creates
-- new credit_plans rows keyed by (plan_key, config_version) instead of mutating
-- rows in place. Existing users stay on their assigned plan version until
-- explicitly migrated via migrate_plan_users().
--
-- This is safe to apply on a fresh database or on any existing database; the
-- migration backfills existing rows and the NOT NULL constraint is added only
-- after the backfill completes.

-- ── Schema: config_version on credit_plans ─────────────────────────────

ALTER TABLE public.credit_plans ADD COLUMN IF NOT EXISTS config_version INTEGER;

UPDATE public.credit_plans
SET config_version = COALESCE(
    (SELECT version FROM public.credit_pricing_config WHERE active = true LIMIT 1),
    1
)
WHERE config_version IS NULL;

ALTER TABLE public.credit_plans ALTER COLUMN config_version SET NOT NULL;

-- Replace the (plan_key) unique index with a composite (plan_key, config_version)
-- so the same plan_key can appear in multiple config versions.
-- Recreate the old index as non-unique so that IF NOT EXISTS checks in earlier
-- migrations won't fail when re-applied (test/reapply environments).
DROP INDEX IF EXISTS idx_credit_plans_plan_key;
CREATE INDEX IF NOT EXISTS idx_credit_plans_plan_key
    ON public.credit_plans (plan_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_plans_plan_key_version
    ON public.credit_plans (plan_key, config_version) WHERE plan_key IS NOT NULL;


-- ── Drop old sync_plans_from_config (signature changes) ─────────────────

DO $$ DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'sync_plans_from_config' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;


-- ── sync_plans_from_config — INSERT new versioned rows ─────────────────
--
-- Instead of ON CONFLICT (plan_key) DO UPDATE (which mutated the row in
-- place and silently changed allowances for every user on that plan), this
-- version creates new rows keyed by (plan_key, config_version). Existing
-- users' credit_plans rows are never mutated.
--
-- Idempotent: re-running with the same config_version refreshes the rows
-- via ON CONFLICT DO UPDATE.

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
BEGIN
    IF p_config ? 'plans' AND jsonb_typeof(p_config->'plans') = 'object' THEN
        FOR v_plan_key, v_plan_def IN SELECT * FROM jsonb_each(p_config->'plans')
        LOOP
            INSERT INTO public.credit_plans (
                plan_key, config_version, label, allowance_amount, rate_overrides,
                entitlements, billing_mode, per_operation, max_concurrent,
                overdraft_floor, allowance_period
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
                COALESCE(v_plan_def #>> '{allowance,period}', 'calendar_month')
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
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sync_plans_from_config(JSONB, INTEGER) FROM PUBLIC, anon, authenticated;


-- ── Redefine set_active_pricing_config — pass version to sync_plans ─────
--
-- The function is already defined in 013_billing.sql with the billing sync
-- included. This version just changes the sync_plans_from_config call to
-- pass the new version number. Everything else is identical.

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
    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM public.credit_pricing_config;

    UPDATE public.credit_pricing_config SET active = false WHERE active = true;

    INSERT INTO public.credit_pricing_config (config, active, version, label)
    VALUES (p_config, true, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    PERFORM public.sync_plans_from_config(p_config, v_next_version);
    PERFORM public.sync_buckets_from_config(p_config);
    PERFORM public.sync_billing_from_config(p_config->'billing');

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true
    );
END;
$$;


-- ── Redefine set_user_plan — resolve to active config version ───────────
--
-- set_user_plan currently resolves plan_key → plan_id by taking the first
-- match (004_plans.sql:209-211). With versioned plan rows, we must resolve
-- to the currently-active config's version of the plan_key. If the plan_key
-- isn't in the active config we fall back to the latest version so that
-- grandfathered plan keys can still be assigned (e.g. reactivation of a
-- cancelled sub whose plan was removed from the config).

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
    -- Resolve to the active config's version of this plan_key
    SELECT cp.id INTO v_plan_id
    FROM public.credit_plans cp
    WHERE cp.plan_key = p_plan_key
      AND cp.config_version = (
          SELECT version FROM public.credit_pricing_config WHERE active = true LIMIT 1
      );

    -- Fallback: if plan_key not in active config, use the latest version
    -- (This handles grandfathered users whose plan was removed from config.)
    IF v_plan_id IS NULL THEN
        SELECT id INTO v_plan_id
        FROM public.credit_plans
        WHERE plan_key = p_plan_key
        ORDER BY config_version DESC
        LIMIT 1;
    END IF;

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

-- Also grant the new signature to service_role (existing GRANT is for the
-- old overload; after DROP+CREATE we re-grant).
GRANT EXECUTE ON FUNCTION public.set_user_plan(UUID, TEXT, TIMESTAMPTZ) TO service_role;


-- ── migrate_plan_users — bulk-migrate users to a specific plan version ──

CREATE OR REPLACE FUNCTION public.migrate_plan_users(
    p_plan_key TEXT,
    p_target_config_version INTEGER DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_target_plan_id UUID;
    v_count INTEGER;
BEGIN
    -- Resolve target plan_id: specific version if given, otherwise latest
    IF p_target_config_version IS NOT NULL THEN
        SELECT id INTO v_target_plan_id
        FROM public.credit_plans
        WHERE plan_key = p_plan_key AND config_version = p_target_config_version;
    ELSE
        SELECT id INTO v_target_plan_id
        FROM public.credit_plans
        WHERE plan_key = p_plan_key
        ORDER BY config_version DESC
        LIMIT 1;
    END IF;

    IF v_target_plan_id IS NULL THEN
        RETURN jsonb_build_object('error', 'plan_not_found');
    END IF;

    -- Bulk-migrate all users on older versions of this plan_key to the target
    -- version. plan_assigned_at is NOT reset so allowance windows keep their
    -- current anchor.
    UPDATE public.user_credits
    SET plan_id = v_target_plan_id,
        updated_at = now()
    WHERE plan_id IN (
        SELECT id FROM public.credit_plans
        WHERE plan_key = p_plan_key AND id != v_target_plan_id
    );

    GET DIAGNOSTICS v_count = ROW_COUNT;

    RETURN jsonb_build_object(
        'plan_key', p_plan_key,
        'target_plan_id', v_target_plan_id,
        'target_config_version', COALESCE(p_target_config_version,
            (SELECT config_version FROM public.credit_plans WHERE id = v_target_plan_id)),
        'migrated_count', v_count
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.migrate_plan_users(TEXT, INTEGER) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.migrate_plan_users(TEXT, INTEGER) TO service_role;


-- ── Fix activate_pricing_config — re-run syncs with target config ───────
--
-- Previously, activate_pricing_config only flipped the active flag without
-- re-syncing the derived tables (plans, buckets, billing). This meant a
-- rollback to an old config version left the derived tables in the "new"
-- state — inconsistent.
--
-- Now it loads the target version's config and re-runs all three syncs,
-- ensuring plans/buckets/billing are consistent with the activated config.

CREATE OR REPLACE FUNCTION public.activate_pricing_config(p_version INTEGER)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_target_version INTEGER;
    v_target_id UUID;
    v_config JSONB;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('bursar_pricing_version'));

    -- Verify the target version exists and fetch its config
    SELECT id, config INTO v_target_id, v_config
    FROM public.credit_pricing_config
    WHERE version = p_version;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'version_not_found');
    END IF;

    -- Deactivate all configs
    UPDATE public.credit_pricing_config SET active = false WHERE active = true;

    -- Activate the target version
    UPDATE public.credit_pricing_config SET active = true
    WHERE version = p_version
    RETURNING id INTO v_target_id;

    -- Re-sync derived tables from the target config
    PERFORM public.sync_plans_from_config(v_config, p_version);
    PERFORM public.sync_buckets_from_config(v_config);
    PERFORM public.sync_billing_from_config(v_config->'billing');

    RETURN jsonb_build_object(
        'id', v_target_id,
        'version', p_version,
        'active', true
    );
END;
$$;


-- ── Update get_user_plan — include config_version in the result ─────────

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
    v_billing_mode TEXT;
    v_per_operation JSONB;
    v_max_concurrent INTEGER;
    v_overdraft_floor NUMERIC;
    v_allowance_period TEXT;
    v_plan_assigned_at TIMESTAMPTZ;
    v_config_version INTEGER;
BEGIN
    SELECT uc.plan_id, cp.label, cp.allowance_amount, cp.entitlements,
           cp.billing_mode, cp.per_operation, cp.max_concurrent, cp.overdraft_floor,
           cp.allowance_period, uc.plan_assigned_at, cp.config_version
    INTO v_plan_id, v_plan_label, v_allowance_amount, v_entitlements,
         v_billing_mode, v_per_operation, v_max_concurrent, v_overdraft_floor,
         v_allowance_period, v_plan_assigned_at, v_config_version
    FROM public.user_credits uc
    LEFT JOIN public.credit_plans cp ON cp.id = uc.plan_id
    WHERE uc.user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'plan_id', v_plan_id,
        'plan_label', v_plan_label,
        'allowance_amount', COALESCE(v_allowance_amount, 0),
        'entitlements', COALESCE(v_entitlements, '{}'::jsonb),
        'billing_mode', COALESCE(v_billing_mode, 'strict'),
        'per_operation', COALESCE(v_per_operation, '{}'::jsonb),
        'max_concurrent', v_max_concurrent,
        'overdraft_floor', v_overdraft_floor,
        'allowance_period', COALESCE(v_allowance_period, 'calendar_month'),
        'plan_assigned_at', v_plan_assigned_at,
        'config_version', v_config_version
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_user_plan(UUID) FROM PUBLIC, anon, authenticated;

NOTIFY pgrst, 'reload schema';
