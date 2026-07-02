-- ducto: 022 — configurable free-allowance reset window (WS9).
--
-- Adds support for PlanDefinition.allowance_period in ("calendar_month" [default,
-- unchanged behavior], "rolling_30d", "anniversary"). The manager (Python/JS)
-- resolves the actual [period_start, period_end) window using the shared
-- resolve_allowance_window() logic and passes just period_start down to SQL as a
-- new trailing p_period_start DATE parameter on the debit/lease RPCs.
--
-- SQL's job is narrow: key usage-window rows by whatever period_start it is
-- given (billing_period = p_period_start), falling back to the current UTC
-- calendar month when p_period_start IS NULL. This generalizes correctly to
-- any period length because the manager/store layer (not SQL) owns computing
-- period boundaries for rolling_30d/anniversary plans and is the sole
-- authority for period_end display purposes.
--
-- Summary of changes:
--   1. credit_plans.allowance_period TEXT NOT NULL DEFAULT 'calendar_month'.
--   2. user_credits.plan_assigned_at TIMESTAMPTZ (+ backfill from created_at).
--   3. sync_plans_from_config also persists allowance_period from the JSONB
--      plan definition (supports both snake_case and camelCase keys).
--   4. set_user_plan sets plan_assigned_at = now() on insert AND on
--      ON CONFLICT DO UPDATE (every (re-)assignment re-anchors the window).
--   5. get_user_plan additionally returns allowance_period + plan_assigned_at.
--   6. deduct_with_allowance / settle_lease / create_lease / check_plan_allowance
--      each gain a new trailing p_period_start DATE DEFAULT NULL parameter.
--      Everywhere v_period_start was computed via date_trunc('month', ...), it
--      now COALESCEs to p_period_start first.

-- ── 1. credit_plans.allowance_period ────────────────────────────────────────
ALTER TABLE public.credit_plans
    ADD COLUMN IF NOT EXISTS allowance_period TEXT NOT NULL DEFAULT 'calendar_month';

-- ── 2. user_credits.plan_assigned_at (+ backfill) ───────────────────────────
ALTER TABLE public.user_credits ADD COLUMN IF NOT EXISTS plan_assigned_at TIMESTAMPTZ;

UPDATE public.user_credits
SET plan_assigned_at = created_at
WHERE plan_id IS NOT NULL AND plan_assigned_at IS NULL;

-- ── 3. sync_plans_from_config: also persist allowance_period ────────────────
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
                plan_key, name, free_allowance, rate_overrides, features,
                default_billing_mode, per_operation, max_concurrent, overdraft_floor,
                allowance_period
            )
            VALUES (
                v_plan_key,
                v_plan_def->>'name',
                COALESCE((v_plan_def->>'free_allowance')::NUMERIC, (v_plan_def->>'freeAllowance')::NUMERIC, 0),
                COALESCE(v_plan_def->'rate_overrides', v_plan_def->'rateOverrides', '{}'::jsonb),
                COALESCE(v_plan_def->'features', '{}'::jsonb),
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

REVOKE EXECUTE ON FUNCTION public.sync_plans_from_config(JSONB) FROM anon, authenticated;

-- ── 4. set_user_plan: re-anchor plan_assigned_at on every (re-)assignment ───
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

REVOKE EXECUTE ON FUNCTION public.set_user_plan(UUID, TEXT) FROM anon, authenticated;

-- ── 5. get_user_plan: also return allowance_period + plan_assigned_at ───────
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

    SELECT uc.plan_id, cp.name, cp.free_allowance, cp.features,
           cp.default_billing_mode, cp.per_operation, cp.max_concurrent, cp.overdraft_floor,
           cp.allowance_period, uc.plan_assigned_at
    INTO v_plan_id, v_plan_name, v_free_allowance, v_features,
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
        'default_billing_mode', COALESCE(v_billing_mode, 'strict'),
        'per_operation', COALESCE(v_per_operation, '{}'::jsonb),
        'max_concurrent', v_max_concurrent,
        'overdraft_floor', v_overdraft_floor,
        'allowance_period', COALESCE(v_allowance_period, 'calendar_month'),
        'plan_assigned_at', v_plan_assigned_at
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_user_plan(UUID) FROM anon, authenticated;

-- ── 6a. check_plan_allowance: p_period_start override ───────────────────────
-- The added parameter changes the overload signature Postgres sees, so the
-- old 1-arg form must be dropped first or both coexist ambiguously.
DROP FUNCTION IF EXISTS public.check_plan_allowance(UUID);

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

    -- Calendar-month fallback when p_period_start is NULL (pinned to UTC, M16):
    -- this is the regression-safety baseline and must match pre-WS9 behavior
    -- exactly. When p_period_start IS supplied (rolling_30d/anniversary,
    -- resolved by the manager/store layer), usage is keyed on it directly;
    -- period_end here is generic "not this period" display only — the
    -- manager/store layer owns the authoritative period_end for display.
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

REVOKE EXECUTE ON FUNCTION public.check_plan_allowance(UUID, DATE) FROM anon, authenticated;

-- ── 6b. increment_usage_window: p_period_start override ─────────────────────
-- NOTE: this RPC is NOT called from any of the atomic debit/lease RPCs below
-- (deduct_with_allowance / settle_lease / create_lease all consume allowance
-- inline via a direct INSERT ... ON CONFLICT into credit_usage_window). It is
-- effectively dead on the hot paths -- kept for API/signature consistency with
-- postgres.py / supabase.py's public increment_usage_window() store method.
DROP FUNCTION IF EXISTS public.increment_usage_window(UUID, UUID, NUMERIC);

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
    IF p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
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

REVOKE EXECUTE ON FUNCTION public.increment_usage_window(UUID, UUID, NUMERIC, DATE) FROM anon, authenticated;

-- ── 6c. deduct_with_allowance: p_period_start override (live sig from 018) ──
DROP FUNCTION IF EXISTS public.deduct_with_allowance(UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN);

CREATE OR REPLACE FUNCTION public.deduct_with_allowance(
    p_user_id          UUID,
    p_amount           NUMERIC,
    p_idempotency_key  TEXT DEFAULT NULL,
    p_min_balance      NUMERIC DEFAULT 0,
    p_model            TEXT DEFAULT NULL,
    p_metadata         JSONB DEFAULT '{}'::jsonb,
    p_skip_allowance   BOOLEAN DEFAULT FALSE,
    p_period_start     DATE DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance              NUMERIC;
    v_plan_id              UUID;
    v_free_allowance       NUMERIC;
    v_period_start         DATE;
    v_used                 NUMERIC;
    v_remaining            NUMERIC;
    v_consume              NUMERIC := 0;
    v_net                  NUMERIC;
    v_cap                  RECORD;
    v_cap_spend            NUMERIC;
    v_cap_window           TIMESTAMPTZ;
    v_cap_warning          TEXT := NULL;
    v_new_balance          NUMERIC;
    v_transaction_id       UUID;
    v_metadata             JSONB;
    v_existing_id          UUID;
    v_existing_amt         NUMERIC;
    v_existing_cons        NUMERIC;
    v_existing_bal_after   NUMERIC;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF p_amount IS NULL
       OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric
       OR p_amount = '-Infinity'::numeric
       OR p_amount < 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    SELECT balance, plan_id INTO v_balance, v_plan_id
    FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0) ON CONFLICT (user_id) DO NOTHING;
        SELECT balance, plan_id INTO v_balance, v_plan_id
        FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    END IF;

    -- (2) Idempotency replay: return the original balance_after from tx metadata
    --     rather than the (wrong) current balance (Fix 8).
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id,
               ABS(amount),
               COALESCE((metadata->>'allowance_consumed')::numeric, 0),
               COALESCE((metadata->>'balance_after')::numeric, v_balance)
        INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after
        FROM public.credit_transactions
        WHERE user_id = p_user_id
          AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id,
                'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons,
                'balance_after', v_existing_bal_after,
                'idempotent', true,
                'cap_warning', NULL
            );
        END IF;
    END IF;

    -- (3) Allowance: skipped for fixed-cost jobs (p_skip_allowance = TRUE, Fix 7).
    -- v_period_start (WS9): explicit p_period_start (rolling_30d/anniversary,
    -- resolved by the manager) else the current UTC calendar month (unchanged).
    IF NOT p_skip_allowance AND v_plan_id IS NOT NULL THEN
        SELECT free_allowance INTO v_free_allowance
        FROM public.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM public.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_remaining := GREATEST(COALESCE(v_free_allowance, 0) - COALESCE(v_used, 0), 0);
        v_consume   := LEAST(v_remaining, p_amount);
    END IF;

    v_net := p_amount - v_consume;

    BEGIN
        IF v_consume > 0 THEN
            INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
            VALUES (p_user_id, v_plan_id, v_period_start, v_consume)
            ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
                usage = public.credit_usage_window.usage + v_consume,
                updated_at = now();
        END IF;

        FOR v_cap IN
            SELECT action, cap_type, model, cap_limit
            FROM public.credit_spend_caps
            WHERE user_id = p_user_id AND (model IS NULL OR model = p_model)
            ORDER BY (action = 'deny') DESC, cap_limit ASC
        LOOP
            v_cap_window := CASE v_cap.cap_type
                WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
                ELSE date_trunc('month', now() AT TIME ZONE 'UTC')
            END;
            SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_cap_spend
            FROM public.credit_transactions ct
            WHERE ct.user_id = p_user_id AND ct.type IN ('usage', 'team_usage') AND ct.amount < 0
              AND ct.created_at >= v_cap_window
              AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);
            IF v_cap_spend + v_net > v_cap.cap_limit THEN
                IF v_cap.action = 'deny' THEN
                    RAISE EXCEPTION 'ducto_cap_reached' USING ERRCODE = 'DU001';
                ELSE
                    IF v_cap_warning IS NULL THEN v_cap_warning := v_cap.action; END IF;
                END IF;
            END IF;
        END LOOP;

        IF v_balance - v_net < p_min_balance THEN
            RAISE EXCEPTION 'ducto_insufficient_credits' USING ERRCODE = 'DU002';
        END IF;

        UPDATE public.user_credits
        SET balance = balance - v_net, updated_at = now()
        WHERE user_id = p_user_id
        RETURNING balance INTO v_new_balance;

        -- Store balance_after in metadata for correct idempotent replay (Fix 8).
        v_metadata := COALESCE(p_metadata, '{}'::jsonb)
            || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model))
            || jsonb_build_object('allowance_consumed', v_consume, 'balance_after', v_new_balance);

        INSERT INTO public.credit_transactions (user_id, amount, type, reference_type, metadata)
        VALUES (p_user_id, -v_net, 'usage', p_metadata->>'reference_type', v_metadata)
        RETURNING id INTO v_transaction_id;

    EXCEPTION
        WHEN SQLSTATE 'DU001' THEN
            RETURN jsonb_build_object('error', 'cap_reached', 'action', 'deny');
        WHEN SQLSTATE 'DU002' THEN
            RETURN jsonb_build_object('error', 'insufficient_credits');
        WHEN unique_violation THEN
            SELECT id,
                   ABS(amount),
                   COALESCE((metadata->>'allowance_consumed')::numeric, 0),
                   COALESCE((metadata->>'balance_after')::numeric, v_balance)
            INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after
            FROM public.credit_transactions
            WHERE user_id = p_user_id AND metadata->>'idempotency_key' = p_idempotency_key
            LIMIT 1;
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id, 'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons, 'balance_after', v_existing_bal_after,
                'idempotent', true, 'cap_warning', NULL
            );
    END;

    RETURN jsonb_build_object(
        'transaction_id', v_transaction_id,
        'amount', v_net,
        'allowance_consumed', v_consume,
        'balance_after', v_new_balance,
        'idempotent', false,
        'cap_warning', v_cap_warning
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.deduct_with_allowance(UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN, DATE) FROM anon, authenticated;

-- ── 6d. create_lease: p_period_start override (live sig from 018) ──────────
DROP FUNCTION IF EXISTS public.create_lease(UUID, NUMERIC, TEXT, TEXT, NUMERIC, INTEGER, INTEGER, TEXT, NUMERIC, JSONB);

CREATE OR REPLACE FUNCTION public.create_lease(
    p_user_id         UUID,
    p_amount          NUMERIC,
    p_operation_type  TEXT,
    p_billing_mode    TEXT DEFAULT 'strict',
    p_floor           NUMERIC DEFAULT 0,
    p_max_concurrent  INTEGER DEFAULT NULL,
    p_ttl_seconds     INTEGER DEFAULT 600,
    p_model           TEXT DEFAULT NULL,
    p_overdraft_floor NUMERIC DEFAULT NULL,
    p_metadata        JSONB DEFAULT '{}'::jsonb,
    p_period_start    DATE DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance         NUMERIC;
    v_plan_id         UUID;
    v_free_allowance  NUMERIC;
    v_period_start    DATE;
    v_used            NUMERIC;
    v_allowance_avail NUMERIC := 0;
    v_active_cnt      INTEGER;
    v_reserved        NUMERIC;
    v_available       NUMERIC;
    v_cap             RECORD;
    v_cap_window      TIMESTAMPTZ;
    v_cap_spend       NUMERIC;
    v_lease_id        UUID;
    v_expires_at      TIMESTAMPTZ;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF p_amount IS NULL OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric OR p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- Lock the balance row (and capture plan_id), creating it if missing.
    SELECT balance, plan_id INTO v_balance, v_plan_id
    FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0) ON CONFLICT (user_id) DO NOTHING;
        SELECT balance, plan_id INTO v_balance, v_plan_id
        FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    END IF;

    -- (1A) Allowance headroom: remaining free allowance counts toward available funds
    --      at admission so a free-tier user can hold a worst-case amount even when
    --      their cash balance is below the hold (Fix 1 / D4). v_period_start (WS9):
    --      explicit p_period_start else the current UTC calendar month (unchanged).
    IF v_plan_id IS NOT NULL THEN
        SELECT free_allowance INTO v_free_allowance
        FROM public.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM public.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_allowance_avail := GREATEST(COALESCE(v_free_allowance, 0) - COALESCE(v_used, 0), 0);
    END IF;

    -- (2) Concurrency: count active, unexpired leases for this operation type.
    IF p_max_concurrent IS NOT NULL THEN
        SELECT COUNT(*) INTO v_active_cnt
        FROM public.credit_reservations
        WHERE user_id = p_user_id AND operation_type = p_operation_type
          AND status = 'active' AND expires_at > now();
        IF v_active_cnt >= p_max_concurrent THEN
            RETURN jsonb_build_object('error', 'concurrency_limit', 'billing_mode', p_billing_mode);
        END IF;
    END IF;

    -- (3) Deny spend cap at admission.
    FOR v_cap IN
        SELECT cap_type, model, cap_limit FROM public.credit_spend_caps
        WHERE user_id = p_user_id AND action = 'deny' AND (model IS NULL OR model = p_model)
    LOOP
        v_cap_window := CASE v_cap.cap_type
            WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
            ELSE date_trunc('month', now() AT TIME ZONE 'UTC')
        END;
        SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_cap_spend
        FROM public.credit_transactions ct
        WHERE ct.user_id = p_user_id AND ct.type IN ('usage', 'team_usage') AND ct.amount < 0
          AND ct.created_at >= v_cap_window
          AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);
        IF v_cap_spend + p_amount > v_cap.cap_limit THEN
            RETURN jsonb_build_object('error', 'cap_reached', 'billing_mode', p_billing_mode);
        END IF;
    END LOOP;

    -- (4) effective_available = balance − Σ active holds + allowance headroom.
    --     Allowance covers the gap so free-tier users aren't falsely rejected.
    SELECT COALESCE(SUM(amount), 0) INTO v_reserved
    FROM public.credit_reservations
    WHERE user_id = p_user_id AND status = 'active' AND expires_at > now();

    v_available := v_balance - v_reserved + v_allowance_avail;
    IF v_available - p_amount < p_floor THEN
        RETURN jsonb_build_object(
            'error', 'insufficient_credits',
            'available', v_available, 'reserved', v_reserved, 'billing_mode', p_billing_mode
        );
    END IF;

    -- (5) Insert the active lease.
    v_expires_at := now() + make_interval(secs => p_ttl_seconds);
    INSERT INTO public.credit_reservations
        (user_id, amount, operation_type, metadata, expires_at, status, billing_mode, overdraft_floor)
    VALUES
        (p_user_id, p_amount, p_operation_type, COALESCE(p_metadata, '{}'::jsonb),
         v_expires_at, 'active', p_billing_mode, p_overdraft_floor)
    RETURNING id INTO v_lease_id;

    RETURN jsonb_build_object(
        'lease_id', v_lease_id,
        'user_id', p_user_id,
        'amount', p_amount,
        'available', v_available - p_amount,
        'reserved', v_reserved + p_amount,
        'billing_mode', p_billing_mode,
        'expires_at', v_expires_at
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.create_lease(UUID, NUMERIC, TEXT, TEXT, NUMERIC, INTEGER, INTEGER, TEXT, NUMERIC, JSONB, DATE) FROM anon, authenticated;

-- ── 6e. settle_lease: consolidate + p_period_start override ─────────────────
-- Two overloads currently coexist on `main` (a pre-existing latent bug this
-- migration also fixes): 021_settle_lease_floor.sql's 7-arg version (C1 floor
-- clamp, but NO p_skip_allowance) and 019_settle_skip_allowance.sql's 8-arg
-- version (p_skip_allowance, but NOT floor-clamped). Every real caller
-- (postgres.py / supabase.py) passes 8 positional args including
-- skip_allowance, so it always resolved to the 019 overload -- meaning the
-- C1 floor-clamp fix from 021 was silently NEVER applied in production.
-- This migration drops BOTH old overloads and creates the single canonical
-- signature: floor-clamp (021) + p_skip_allowance (019) + p_period_start (WS9).
DROP FUNCTION IF EXISTS public.settle_lease(UUID, UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB);
DROP FUNCTION IF EXISTS public.settle_lease(UUID, UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN);

CREATE OR REPLACE FUNCTION public.settle_lease(
    p_user_id         UUID,
    p_lease_id        UUID,
    p_amount          NUMERIC,
    p_idempotency_key TEXT DEFAULT NULL,
    p_min_balance     NUMERIC DEFAULT 0,
    p_model           TEXT DEFAULT NULL,
    p_metadata        JSONB DEFAULT '{}'::jsonb,
    p_skip_allowance  BOOLEAN DEFAULT FALSE,
    p_period_start    DATE DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance        NUMERIC;
    v_plan_id        UUID;
    v_status         TEXT;
    v_settle_tx      UUID;
    v_lease_expires  TIMESTAMPTZ;
    v_billing_mode   TEXT;
    v_overdraft_floor NUMERIC;
    v_settle_floor   NUMERIC;
    v_max_debit      NUMERIC;
    v_free_allowance NUMERIC;
    v_period_start   DATE;
    v_used           NUMERIC;
    v_consume        NUMERIC := 0;
    v_net            NUMERIC;
    v_cap            RECORD;
    v_cap_window     TIMESTAMPTZ;
    v_cap_spend      NUMERIC;
    v_cap_warning    TEXT := NULL;
    v_new_balance    NUMERIC;
    v_tx_id          UUID;
    v_metadata       JSONB;
    v_existing_id    UUID;
    v_existing_amt   NUMERIC;
    v_existing_cons  NUMERIC;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF p_amount IS NULL OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric OR p_amount < 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    SELECT balance, plan_id INTO v_balance, v_plan_id
    FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0) ON CONFLICT (user_id) DO NOTHING;
        SELECT balance, plan_id INTO v_balance, v_plan_id
        FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    END IF;

    -- Idempotency replay (user-scoped).
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0)
        INTO v_existing_id, v_existing_amt, v_existing_cons
        FROM public.credit_transactions
        WHERE user_id = p_user_id AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id, 'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons, 'balance_after', v_balance,
                'idempotent', true, 'cap_warning', NULL
            );
        END IF;
    END IF;

    -- Lock + validate the lease state; also read billing policy columns (C1).
    SELECT status, settle_tx_id, expires_at, billing_mode, overdraft_floor
    INTO v_status, v_settle_tx, v_lease_expires, v_billing_mode, v_overdraft_floor
    FROM public.credit_reservations
    WHERE id = p_lease_id AND user_id = p_user_id FOR UPDATE;

    IF NOT FOUND OR v_status = 'released' THEN
        RETURN jsonb_build_object('error', 'lease_not_found', 'balance_after', v_balance);
    END IF;
    IF v_status = 'settled' THEN
        IF v_settle_tx IS NOT NULL THEN
            SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0)
            INTO v_existing_id, v_existing_amt, v_existing_cons
            FROM public.credit_transactions WHERE id = v_settle_tx;
            IF FOUND THEN
                RETURN jsonb_build_object(
                    'transaction_id', v_existing_id, 'amount', v_existing_amt,
                    'allowance_consumed', v_existing_cons, 'balance_after', v_balance,
                    'idempotent', true, 'cap_warning', NULL
                );
            END IF;
        END IF;
        RETURN jsonb_build_object('amount', 0, 'balance_after', v_balance, 'idempotent', true);
    END IF;
    IF v_status = 'expired' OR v_lease_expires <= now() THEN
        UPDATE public.credit_reservations SET status = 'expired' WHERE id = p_lease_id;
        RETURN jsonb_build_object('error', 'lease_expired', 'balance_after', v_balance);
    END IF;

    -- Zero-cost settle releases the lease without charging (M3).
    IF p_amount = 0 THEN
        UPDATE public.credit_reservations SET status = 'settled' WHERE id = p_lease_id;
        RETURN jsonb_build_object('transaction_id', NULL, 'amount', 0, 'balance_after', v_balance, 'idempotent', false);
    END IF;

    -- Allowance consume on the actual cost (mirrors deduct_with_allowance).
    -- Skipped when p_skip_allowance = TRUE (Fix 7 / #4): fixed-cost batch jobs
    -- reserved via the lease path must not deplete the free inference allowance.
    -- v_period_start (WS9): explicit p_period_start else the current UTC
    -- calendar month (unchanged).
    IF NOT p_skip_allowance AND v_plan_id IS NOT NULL THEN
        SELECT free_allowance INTO v_free_allowance FROM public.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM public.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_consume := LEAST(GREATEST(COALESCE(v_free_allowance, 0) - COALESCE(v_used, 0), 0), p_amount);
    END IF;
    v_net := p_amount - v_consume;

    -- Floor enforcement (C1): clamp v_net so the post-settle balance stays ≥ floor.
    -- strict / strict_prepaid → floor is p_min_balance (engine's min_balance).
    -- overdraft → floor is the overdraft_floor stored on the lease (can be negative).
    IF v_billing_mode IN ('strict', 'strict_prepaid') THEN
        v_settle_floor := COALESCE(p_min_balance, 0);
    ELSE
        v_settle_floor := COALESCE(v_overdraft_floor, 0);
    END IF;
    v_max_debit := GREATEST(0, v_balance - v_settle_floor);
    IF v_net > v_max_debit THEN
        v_net := v_max_debit;
        -- Re-clamp allowance consume so it doesn't exceed amount - net.
        IF v_net < p_amount THEN
            v_consume := LEAST(v_consume, p_amount - v_net);
        END IF;
    END IF;

    -- Spend cap is ADVISORY at settle (never blocks): record the strongest breach.
    FOR v_cap IN
        SELECT action, cap_type, model, cap_limit FROM public.credit_spend_caps
        WHERE user_id = p_user_id AND (model IS NULL OR model = p_model)
        ORDER BY (action = 'deny') DESC, cap_limit ASC
    LOOP
        v_cap_window := CASE v_cap.cap_type
            WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
            ELSE date_trunc('month', now() AT TIME ZONE 'UTC')
        END;
        SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_cap_spend
        FROM public.credit_transactions ct
        WHERE ct.user_id = p_user_id AND ct.type IN ('usage', 'team_usage') AND ct.amount < 0
          AND ct.created_at >= v_cap_window
          AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);
        IF v_cap_spend + v_net > v_cap.cap_limit AND (v_cap_warning IS NULL OR (v_cap_warning <> 'deny' AND v_cap.action = 'deny')) THEN
            v_cap_warning := v_cap.action;
        END IF;
    END LOOP;

    IF v_consume > 0 THEN
        INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
        VALUES (p_user_id, v_plan_id, v_period_start, v_consume)
        ON CONFLICT (user_id, plan_id, billing_period)
        DO UPDATE SET usage = public.credit_usage_window.usage + v_consume, updated_at = now();
    END IF;

    v_metadata := COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model))
        || jsonb_build_object('allowance_consumed', v_consume, 'balance_after', v_balance - v_net);

    UPDATE public.user_credits SET balance = balance - v_net, updated_at = now()
    WHERE user_id = p_user_id RETURNING balance INTO v_new_balance;

    INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
    VALUES (p_user_id, -v_net, 'usage', v_metadata) RETURNING id INTO v_tx_id;

    UPDATE public.credit_reservations SET status = 'settled', settle_tx_id = v_tx_id WHERE id = p_lease_id;

    RETURN jsonb_build_object(
        'transaction_id', v_tx_id, 'amount', v_net, 'allowance_consumed', v_consume,
        'balance_after', v_new_balance, 'idempotent', false, 'cap_warning', v_cap_warning
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.settle_lease(UUID, UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN, DATE) FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
