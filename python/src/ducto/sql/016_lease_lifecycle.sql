-- ducto: 016 — lease lifecycle (atomic admission) for the financial-safety API.
--
-- Implements the interface-plan §3/§4 primitives as server-side RPCs that mirror
-- the MemoryStore lease lifecycle exactly (parity is a project invariant):
--   create_lease  — the ONLY admission control (D4): one lock, counts active
--                   leases for max_concurrent, deny-cap gate, floor check.
--   settle_lease  — de-clamped charge of the ACTUAL cost (D5); never blocks on
--                   floor/cap (advisory at settle); balance may go negative.
--   release_lease — idempotent release without charge (H1).
--   renew_lease   — extend TTL for long jobs (B4).
--   get_available_credits — advisory available = balance − Σ active holds.
--   expire_due_leases — reaper that marks crashed/abandoned holds expired.
--
-- Leases reuse credit_reservations, extended with a status (active → settled |
-- released | expired), a billing mode, the resolved overdraft floor, and the
-- settling transaction id. Money is NUMERIC(18,4); windows pinned to UTC (M16).

-- ── Schema: lease columns on credit_reservations ───────────────────────────
ALTER TABLE public.credit_reservations
    ADD COLUMN IF NOT EXISTS status         TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS billing_mode   TEXT NOT NULL DEFAULT 'strict',
    ADD COLUMN IF NOT EXISTS overdraft_floor NUMERIC(18,4),
    ADD COLUMN IF NOT EXISTS settle_tx_id   UUID;

-- Active-hold lookups (available sum + concurrency count) hit this index.
CREATE INDEX IF NOT EXISTS idx_credit_reservations_active
    ON public.credit_reservations (user_id, operation_type, status, expires_at);

-- ── Enable overdraft: drop the hard balance >= 0 floor ─────────────────────
-- Overdraft (D3/D5) requires the balance to go negative down to a per-policy
-- floor enforced in RPC logic, not by a blanket table CHECK. Drop the inline
-- CHECK (balance >= 0) from 001 (default name) plus any other balance check.
ALTER TABLE public.user_credits DROP CONSTRAINT IF EXISTS user_credits_balance_check;
DO $$
DECLARE
    v_con TEXT;
BEGIN
    FOR v_con IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'public.user_credits'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%balance%>=%0%'
    LOOP
        EXECUTE format('ALTER TABLE public.user_credits DROP CONSTRAINT %I', v_con);
    END LOOP;
END $$;

-- ── Schema: policy columns on credit_plans ─────────────────────────────────
ALTER TABLE public.credit_plans
    ADD COLUMN IF NOT EXISTS default_billing_mode TEXT NOT NULL DEFAULT 'strict',
    ADD COLUMN IF NOT EXISTS per_operation        JSONB,
    ADD COLUMN IF NOT EXISTS max_concurrent       INTEGER,
    ADD COLUMN IF NOT EXISTS overdraft_floor      NUMERIC(18,4);

-- ── sync_plans_from_config: also sync the policy columns (interface plan §1) ─
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
                default_billing_mode, per_operation, max_concurrent, overdraft_floor
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
                COALESCE((v_plan_def->>'overdraft_floor')::NUMERIC, (v_plan_def->>'overdraftFloor')::NUMERIC)
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
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sync_plans_from_config(JSONB) FROM anon, authenticated;

-- ── get_user_plan: include the policy fields ───────────────────────────────
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
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    SELECT uc.plan_id, cp.name, cp.free_allowance, cp.features,
           cp.default_billing_mode, cp.per_operation, cp.max_concurrent, cp.overdraft_floor
    INTO v_plan_id, v_plan_name, v_free_allowance, v_features,
         v_billing_mode, v_per_operation, v_max_concurrent, v_overdraft_floor
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
        'overdraft_floor', v_overdraft_floor
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_user_plan(UUID) FROM anon, authenticated;

-- ── create_lease: atomic admission (the only admission control, D4) ────────
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
    p_metadata        JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance     NUMERIC;
    v_active_cnt  INTEGER;
    v_reserved    NUMERIC;
    v_available   NUMERIC;
    v_cap         RECORD;
    v_cap_window  TIMESTAMPTZ;
    v_cap_spend   NUMERIC;
    v_lease_id    UUID;
    v_expires_at  TIMESTAMPTZ;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF p_amount IS NULL OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric OR p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- Lock the balance row, creating it if missing (overdraft admits new users).
    SELECT balance INTO v_balance FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0) ON CONFLICT (user_id) DO NOTHING;
        SELECT balance INTO v_balance FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
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

    -- (3) Deny spend cap at admission (a blocked user can't even start).
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

    -- (4) available = balance − Σ active holds; reject if floor breached.
    SELECT COALESCE(SUM(amount), 0) INTO v_reserved
    FROM public.credit_reservations
    WHERE user_id = p_user_id AND status = 'active' AND expires_at > now();

    v_available := v_balance - v_reserved;
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

-- ── settle_lease: de-clamped charge of the ACTUAL cost (D5) ─────────────────
CREATE OR REPLACE FUNCTION public.settle_lease(
    p_user_id         UUID,
    p_lease_id        UUID,
    p_amount          NUMERIC,
    p_idempotency_key TEXT DEFAULT NULL,
    p_min_balance     NUMERIC DEFAULT 0,
    p_model           TEXT DEFAULT NULL,
    p_metadata        JSONB DEFAULT '{}'::jsonb
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

    -- Lock + validate the lease state.
    SELECT status, settle_tx_id, expires_at INTO v_status, v_settle_tx, v_lease_expires
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

    -- Allowance consume on the actual cost (mirrors 015).
    IF v_plan_id IS NOT NULL THEN
        SELECT free_allowance INTO v_free_allowance FROM public.credit_plans WHERE id = v_plan_id;
        v_period_start := (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE;
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM public.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_consume := LEAST(GREATEST(COALESCE(v_free_allowance, 0) - COALESCE(v_used, 0), 0), p_amount);
    END IF;
    v_net := p_amount - v_consume;

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

    -- Debit the actual net (no floor block — de-clamped; balance may go negative).
    v_metadata := COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model))
        || jsonb_build_object('allowance_consumed', v_consume);

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

-- ── release_lease: idempotent release without charge (H1) ───────────────────
CREATE OR REPLACE FUNCTION public.release_lease(p_user_id UUID, p_lease_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_status TEXT;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT status INTO v_status FROM public.credit_reservations
    WHERE id = p_lease_id AND user_id = p_user_id FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('released', false, 'reason', 'not_found');
    END IF;
    IF v_status = 'settled' THEN
        RETURN jsonb_build_object('released', false, 'reason', 'already_settled');
    END IF;
    IF v_status = 'released' THEN
        RETURN jsonb_build_object('released', false, 'reason', 'already_released');
    END IF;

    UPDATE public.credit_reservations SET status = 'released' WHERE id = p_lease_id;
    RETURN jsonb_build_object('released', true, 'reason', 'released');
END;
$$;

-- ── renew_lease: extend an active lease's TTL (B4) ──────────────────────────
CREATE OR REPLACE FUNCTION public.renew_lease(p_user_id UUID, p_lease_id UUID, p_ttl_seconds INTEGER)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_status      TEXT;
    v_amount      NUMERIC;
    v_billing     TEXT;
    v_expires_at  TIMESTAMPTZ;
    v_lease_exp   TIMESTAMPTZ;
    v_balance     NUMERIC;
    v_reserved    NUMERIC;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT status, amount, billing_mode, expires_at
    INTO v_status, v_amount, v_billing, v_lease_exp
    FROM public.credit_reservations
    WHERE id = p_lease_id AND user_id = p_user_id FOR UPDATE;

    IF NOT FOUND OR v_status IN ('released', 'settled') THEN
        RETURN jsonb_build_object('error', 'lease_not_found');
    END IF;
    IF v_status = 'expired' OR v_lease_exp <= now() THEN
        UPDATE public.credit_reservations SET status = 'expired' WHERE id = p_lease_id;
        RETURN jsonb_build_object('error', 'lease_expired');
    END IF;

    v_expires_at := now() + make_interval(secs => p_ttl_seconds);
    UPDATE public.credit_reservations SET expires_at = v_expires_at WHERE id = p_lease_id;

    SELECT balance INTO v_balance FROM public.user_credits WHERE user_id = p_user_id;
    SELECT COALESCE(SUM(amount), 0) INTO v_reserved
    FROM public.credit_reservations
    WHERE user_id = p_user_id AND status = 'active' AND expires_at > now();

    RETURN jsonb_build_object(
        'lease_id', p_lease_id, 'user_id', p_user_id, 'amount', v_amount,
        'available', COALESCE(v_balance, 0) - v_reserved, 'reserved', v_reserved,
        'billing_mode', v_billing, 'expires_at', v_expires_at
    );
END;
$$;

-- ── get_available_credits: advisory available = balance − Σ active holds ────
CREATE OR REPLACE FUNCTION public.get_available_credits(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance  NUMERIC;
    v_reserved NUMERIC;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT COALESCE(balance, 0) INTO v_balance FROM public.user_credits WHERE user_id = p_user_id;
    v_balance := COALESCE(v_balance, 0);
    SELECT COALESCE(SUM(amount), 0) INTO v_reserved
    FROM public.credit_reservations
    WHERE user_id = p_user_id AND status = 'active' AND expires_at > now();

    RETURN jsonb_build_object(
        'user_id', p_user_id, 'balance', v_balance,
        'reserved', v_reserved, 'available', v_balance - v_reserved
    );
END;
$$;

-- ── expire_due_leases: reaper for crashed/abandoned holds (B4/H1) ───────────
CREATE OR REPLACE FUNCTION public.expire_due_leases()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_count INTEGER;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;
    UPDATE public.credit_reservations SET status = 'expired'
    WHERE status = 'active' AND expires_at <= now();
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN jsonb_build_object('expired_count', v_count);
END;
$$;

-- Defense-in-depth: all lease RPCs are backend-only.
REVOKE EXECUTE ON FUNCTION public.create_lease(UUID, NUMERIC, TEXT, TEXT, NUMERIC, INTEGER, INTEGER, TEXT, NUMERIC, JSONB) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.settle_lease(UUID, UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.release_lease(UUID, UUID) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.renew_lease(UUID, UUID, INTEGER) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_available_credits(UUID) FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.expire_due_leases() FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
