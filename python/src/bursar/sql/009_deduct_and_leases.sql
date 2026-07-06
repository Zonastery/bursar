-- bursar: atomic deduction and lease lifecycle — the financial-safety API.
--
-- Two admission/charge primitives:
--   deduct_with_allowance — atomic "calculate-then-charge": lock balance ->
--                   user-scoped idempotency -> consume free allowance ->
--                   enforce spend cap on the NET amount -> balance-floor
--                   check -> tier-aware debit -> insert ledger row.
--   create_lease  — the ONLY admission control: one lock, allowance headroom,
--                   counts active leases for max_concurrent, deny-cap gate,
--                   floor check.
--   settle_lease  — de-clamped charge of the ACTUAL cost, floor-clamped per
--                   billing mode; never blocks on floor/cap (advisory at
--                   settle); balance may go negative under overdraft.
--   release_lease — idempotent release without charge.
--   renew_lease   — extend TTL for long jobs.
--   get_available_credits — advisory available = balance − Σ active holds.
--   expire_due_leases — reaper that marks crashed/abandoned holds expired.
--
-- Leases reuse credit_reservations, extended with a status (active → settled |
-- released | expired), a billing mode, the resolved overdraft floor, and the
-- settling transaction id. Money is NUMERIC(18,4); windows pinned to UTC.
--
-- Both deduct_with_allowance and settle_lease additionally walk configured
-- credit tiers (see 010_credit_tiers.sql) priority-ascending after the
-- (unchanged) floor/clamp logic, to decide which per-tier balance(s) actually
-- fund a given debit. Idempotent replay always echoes the ORIGINAL row's
-- stored tier_breakdown — never recomputes.

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
-- Overdraft requires the balance to go negative down to a per-policy floor
-- enforced in RPC logic, not by a blanket table CHECK. Drop any inline CHECK
-- matching the balance >= 0 pattern, by name-pattern rather than a fixed
-- constraint name, so this is robust regardless of how it was originally named.
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

-- deduct_with_allowance gained BOOLEAN/DATE trailing params across its
-- history. Drop every overload by name first so the current signature is
-- unambiguous (no-op on fresh installs).
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'deduct_with_allowance' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

-- deduct_with_allowance: the entire deduct pipeline in ONE transaction.
-- All-or-nothing: any failure (cap deny, insufficient credits, or a racing
-- duplicate) rolls back the allowance consumption and the balance change.
CREATE OR REPLACE FUNCTION public.deduct_with_allowance(
    p_user_id          UUID,
    p_amount           NUMERIC,
    p_idempotency_key  TEXT DEFAULT NULL,
    p_min_balance      NUMERIC DEFAULT 0,
    p_model            TEXT DEFAULT NULL,
    p_metadata         JSONB DEFAULT '{}'::jsonb,
    p_skip_allowance   BOOLEAN DEFAULT FALSE,
    p_period_start     DATE DEFAULT NULL,
    p_feature               TEXT DEFAULT NULL,
    p_feature_max_calls     INT DEFAULT NULL,
    p_feature_action        TEXT DEFAULT NULL,
    p_feature_period_start  DATE DEFAULT NULL,
    p_feature_period_end    DATE DEFAULT NULL
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
    v_feature_count        INT;
    v_feature_limit_warning TEXT := NULL;
    v_new_balance          NUMERIC;
    v_transaction_id       UUID;
    v_metadata             JSONB;
    v_existing_id          UUID;
    v_existing_amt         NUMERIC;
    v_existing_cons        NUMERIC;
    v_existing_bal_after   NUMERIC;
    v_existing_tier_bd     JSONB;
    -- Tier walk
    v_tier_breakdown       JSONB := '{}'::jsonb;
    v_tier_remaining       NUMERIC;
    v_walk                 RECORD;
    v_tier_balance         NUMERIC;
    v_take                 NUMERIC;
    v_sink_tier            TEXT;
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

    -- (2) Idempotency replay: return the original balance_after/tier_breakdown
    --     from tx metadata rather than the (wrong) current balance.
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id,
               ABS(amount),
               COALESCE((metadata->>'allowance_consumed')::numeric, 0),
               COALESCE((metadata->>'balance_after')::numeric, v_balance),
               COALESCE(metadata->'tier_breakdown', '{}'::jsonb)
        INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after, v_existing_tier_bd
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
                'cap_warning', NULL,
                'feature_limit_warning', NULL,
                'tier_breakdown', v_existing_tier_bd
            );
        END IF;
    END IF;

    -- (3) Allowance: skipped for fixed-cost jobs (p_skip_allowance = TRUE).
    -- v_period_start: explicit p_period_start (rolling_30d/anniversary,
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
                    RAISE EXCEPTION 'bursa_cap_reached' USING ERRCODE = 'DU001';
                ELSE
                    IF v_cap_warning IS NULL THEN v_cap_warning := v_cap.action; END IF;
                END IF;
            END IF;
        END LOOP;

        -- (4b) Feature limit: ledger-derived count of prior committed `usage`
        -- transactions tagged metadata.feature = p_feature within
        -- [p_feature_period_start, p_feature_period_end). Skipped entirely
        -- when no feature/limit was resolved by the caller (manager).
        IF p_feature IS NOT NULL AND p_feature_max_calls IS NOT NULL THEN
            -- Deliberately no `amount < 0` filter (unlike the spend-cap window
            -- query above, which only cares about actual dollars spent): a
            -- call fully covered by free allowance nets to amount = 0 but is
            -- still one invocation and must still count.
            SELECT COUNT(*) INTO v_feature_count
            FROM public.credit_transactions ct
            WHERE ct.user_id = p_user_id
              AND ct.type = 'usage'
              AND ct.metadata->>'feature' = p_feature
              AND ct.created_at >= (p_feature_period_start::timestamp AT TIME ZONE 'UTC')
              AND ct.created_at < (p_feature_period_end::timestamp AT TIME ZONE 'UTC');

            IF v_feature_count >= p_feature_max_calls THEN
                IF p_feature_action = 'deny' THEN
                    RAISE EXCEPTION 'bursa_feature_limit_reached' USING ERRCODE = 'DU003';
                ELSE
                    v_feature_limit_warning := p_feature_action;
                END IF;
            END IF;
        END IF;

        IF v_balance - v_net < p_min_balance THEN
            RAISE EXCEPTION 'bursa_insufficient_credits' USING ERRCODE = 'DU002';
        END IF;

        -- ── Tier walk: decide WHICH tier balance(s) fund this debit. The
        -- aggregate UPDATE below is unchanged and remains authoritative; this
        -- only decides how user_credit_tiers is split. Walk order: configured
        -- tiers by priority ASC, then any tier_keys this user holds balance
        -- under that are no longer in credit_tiers (config drift safety net),
        -- appended last.
        v_tier_remaining := v_net;

        FOR v_walk IN
            SELECT tier_key, priority, 0 AS grp FROM public.credit_tiers
            UNION ALL
            SELECT uct.tier_key, 0, 1 AS grp
            FROM public.user_credit_tiers uct
            WHERE uct.user_id = p_user_id
              AND NOT EXISTS (SELECT 1 FROM public.credit_tiers ct WHERE ct.tier_key = uct.tier_key)
            ORDER BY grp ASC, priority ASC, tier_key ASC
        LOOP
            EXIT WHEN v_tier_remaining <= 0;

            SELECT balance INTO v_tier_balance
            FROM public.user_credit_tiers
            WHERE user_id = p_user_id AND tier_key = v_walk.tier_key
            FOR UPDATE;
            v_tier_balance := COALESCE(v_tier_balance, 0);

            v_take := LEAST(v_tier_balance, v_tier_remaining);
            IF v_take > 0 THEN
                UPDATE public.user_credit_tiers
                SET balance = balance - v_take, updated_at = now()
                WHERE user_id = p_user_id AND tier_key = v_walk.tier_key;

                v_tier_breakdown := v_tier_breakdown || jsonb_build_object(v_walk.tier_key, v_take);
                v_tier_remaining := v_tier_remaining - v_take;
            END IF;
        END LOOP;

        -- Overdraft sink: only reachable when a negative floor sanctioned
        -- going negative (the floor check above already guarantees
        -- v_balance - v_net >= p_min_balance, so configured-tier balances,
        -- which sum to v_balance, always fully cover v_net in strict mode).
        IF v_tier_remaining > 0 THEN
            SELECT tier_key INTO v_sink_tier FROM public.credit_tiers WHERE allow_overdraft = true ORDER BY priority DESC, tier_key DESC LIMIT 1;
            IF v_sink_tier IS NULL THEN
                SELECT tier_key INTO v_sink_tier FROM public.credit_tiers ORDER BY priority DESC, tier_key DESC LIMIT 1;
            END IF;
            IF v_sink_tier IS NULL THEN
                v_sink_tier := 'default';
            END IF;

            INSERT INTO public.user_credit_tiers (user_id, tier_key, balance)
            VALUES (p_user_id, v_sink_tier, -v_tier_remaining)
            ON CONFLICT (user_id, tier_key) DO UPDATE SET
                balance = public.user_credit_tiers.balance - v_tier_remaining,
                updated_at = now();

            v_tier_breakdown := v_tier_breakdown || jsonb_build_object(
                v_sink_tier, COALESCE((v_tier_breakdown->>v_sink_tier)::numeric, 0) + v_tier_remaining
            );
            v_tier_remaining := 0;
        END IF;

        UPDATE public.user_credits
        SET balance = balance - v_net, updated_at = now()
        WHERE user_id = p_user_id
        RETURNING balance INTO v_new_balance;

        -- Store balance_after/tier_breakdown in metadata for correct
        -- idempotent replay.
        -- Tag metadata.feature whenever p_feature is given, regardless of
        -- whether a limit is currently configured (p_feature_max_calls may be
        -- NULL) — this is what makes the ledger-derived count accurate once a
        -- limit is enabled later, and is what future check_feature_limit /
        -- enforcement queries count against.
        v_metadata := COALESCE(p_metadata, '{}'::jsonb)
            || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model, 'feature', p_feature))
            || jsonb_build_object('allowance_consumed', v_consume, 'balance_after', v_new_balance, 'tier_breakdown', v_tier_breakdown);

        INSERT INTO public.credit_transactions (user_id, amount, type, reference_type, metadata)
        VALUES (p_user_id, -v_net, 'usage', p_metadata->>'reference_type', v_metadata)
        RETURNING id INTO v_transaction_id;

    EXCEPTION
        WHEN SQLSTATE 'DU001' THEN
            RETURN jsonb_build_object('error', 'cap_reached', 'action', 'deny');
        WHEN SQLSTATE 'DU002' THEN
            RETURN jsonb_build_object('error', 'insufficient_credits');
        WHEN SQLSTATE 'DU003' THEN
            RETURN jsonb_build_object('error', 'feature_limit_reached', 'action', 'deny');
        WHEN unique_violation THEN
            SELECT id,
                   ABS(amount),
                   COALESCE((metadata->>'allowance_consumed')::numeric, 0),
                   COALESCE((metadata->>'balance_after')::numeric, v_balance),
                   COALESCE(metadata->'tier_breakdown', '{}'::jsonb)
            INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after, v_existing_tier_bd
            FROM public.credit_transactions
            WHERE user_id = p_user_id AND metadata->>'idempotency_key' = p_idempotency_key
            LIMIT 1;
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id, 'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons, 'balance_after', v_existing_bal_after,
                'idempotent', true, 'cap_warning', NULL, 'feature_limit_warning', NULL, 'tier_breakdown', v_existing_tier_bd
            );
    END;

    RETURN jsonb_build_object(
        'transaction_id', v_transaction_id,
        'amount', v_net,
        'allowance_consumed', v_consume,
        'balance_after', v_new_balance,
        'idempotent', false,
        'cap_warning', v_cap_warning,
        'feature_limit_warning', v_feature_limit_warning,
        'tier_breakdown', v_tier_breakdown
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.deduct_with_allowance(UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN, DATE, TEXT, INT, TEXT, DATE, DATE) FROM PUBLIC, anon, authenticated;

-- create_lease gained trailing params across its history. Drop every overload
-- by name first so the current signature is unambiguous.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'create_lease' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

-- create_lease: atomic admission (the only admission control). Allowance
-- headroom (remaining free allowance) counts toward available funds so a
-- free-tier user can hold a worst-case amount even when their cash balance is
-- below the hold.
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
    p_period_start    DATE DEFAULT NULL,
    p_feature               TEXT DEFAULT NULL,
    p_feature_max_calls     INT DEFAULT NULL,
    p_feature_action        TEXT DEFAULT NULL,
    p_feature_period_start  DATE DEFAULT NULL,
    p_feature_period_end    DATE DEFAULT NULL
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
    v_feature_count   INT;
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

    -- (1A) Allowance headroom: remaining free allowance counts toward available
    --      funds at admission. v_period_start: explicit p_period_start else
    --      the current UTC calendar month (unchanged).
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

    -- (3b) Deny-only feature limit at admission — same ledger-derived count as
    -- deduct_with_allowance/settle_lease, but only ever enforces 'deny'
    -- (warn/notify are not checked here: nothing has been charged yet, so
    -- there is nothing to warn about). Skipped when no feature/limit was
    -- resolved by the caller (manager).
    IF p_feature IS NOT NULL AND p_feature_max_calls IS NOT NULL AND p_feature_action = 'deny' THEN
        -- Deliberately no `amount < 0` filter — see deduct_with_allowance's 4b
        -- block for why (zero-net calls still count as invocations).
        SELECT COUNT(*) INTO v_feature_count
        FROM public.credit_transactions ct
        WHERE ct.user_id = p_user_id
          AND ct.type = 'usage'
          AND ct.metadata->>'feature' = p_feature
          AND ct.created_at >= (p_feature_period_start::timestamp AT TIME ZONE 'UTC')
          AND ct.created_at < (p_feature_period_end::timestamp AT TIME ZONE 'UTC');
        IF v_feature_count >= p_feature_max_calls THEN
            RETURN jsonb_build_object('error', 'feature_limit_reached', 'billing_mode', p_billing_mode);
        END IF;
    END IF;

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

REVOKE EXECUTE ON FUNCTION public.create_lease(UUID, NUMERIC, TEXT, TEXT, NUMERIC, INTEGER, INTEGER, TEXT, NUMERIC, JSONB, DATE, TEXT, INT, TEXT, DATE, DATE) FROM PUBLIC, anon, authenticated;

-- settle_lease gained trailing params across its history. Drop every overload
-- by name first so the current signature is unambiguous — this specifically
-- guards against a documented historical bug where two overloads (one with
-- the floor-clamp fix, one with skip_allowance) coexisted and callers always
-- resolved to the wrong one.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'settle_lease' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

-- settle_lease: de-clamped charge of the ACTUAL cost, floor-clamped per
-- billing mode (strict/strict_prepaid -> p_min_balance; overdraft -> the
-- lease's own overdraft_floor, which can be negative). Applies the identical
-- tier walk to the already-floor-clamped v_net; no special-casing.
CREATE OR REPLACE FUNCTION public.settle_lease(
    p_user_id         UUID,
    p_lease_id        UUID,
    p_amount          NUMERIC,
    p_idempotency_key TEXT DEFAULT NULL,
    p_min_balance     NUMERIC DEFAULT 0,
    p_model           TEXT DEFAULT NULL,
    p_metadata        JSONB DEFAULT '{}'::jsonb,
    p_skip_allowance  BOOLEAN DEFAULT FALSE,
    p_period_start    DATE DEFAULT NULL,
    p_feature               TEXT DEFAULT NULL,
    p_feature_max_calls     INT DEFAULT NULL,
    p_feature_action        TEXT DEFAULT NULL,
    p_feature_period_start  DATE DEFAULT NULL,
    p_feature_period_end    DATE DEFAULT NULL
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
    v_feature_count  INT;
    v_feature_limit_warning TEXT := NULL;
    v_new_balance    NUMERIC;
    v_tx_id          UUID;
    v_metadata       JSONB;
    v_existing_id    UUID;
    v_existing_amt   NUMERIC;
    v_existing_cons  NUMERIC;
    v_existing_tier_bd JSONB;
    -- Tier walk
    v_tier_breakdown JSONB := '{}'::jsonb;
    v_tier_remaining NUMERIC;
    v_walk           RECORD;
    v_tier_balance   NUMERIC;
    v_take           NUMERIC;
    v_sink_tier      TEXT;
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
        SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0),
               COALESCE(metadata->'tier_breakdown', '{}'::jsonb)
        INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_tier_bd
        FROM public.credit_transactions
        WHERE user_id = p_user_id AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id, 'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons, 'balance_after', v_balance,
                'idempotent', true, 'cap_warning', NULL, 'feature_limit_warning', NULL, 'tier_breakdown', v_existing_tier_bd
            );
        END IF;
    END IF;

    -- Lock + validate the lease state; also read billing policy columns.
    SELECT status, settle_tx_id, expires_at, billing_mode, overdraft_floor
    INTO v_status, v_settle_tx, v_lease_expires, v_billing_mode, v_overdraft_floor
    FROM public.credit_reservations
    WHERE id = p_lease_id AND user_id = p_user_id FOR UPDATE;

    IF NOT FOUND OR v_status = 'released' THEN
        RETURN jsonb_build_object('error', 'lease_not_found', 'balance_after', v_balance);
    END IF;
    IF v_status = 'settled' THEN
        IF v_settle_tx IS NOT NULL THEN
            SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0),
                   COALESCE(metadata->'tier_breakdown', '{}'::jsonb)
            INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_tier_bd
            FROM public.credit_transactions WHERE id = v_settle_tx;
            IF FOUND THEN
                RETURN jsonb_build_object(
                    'transaction_id', v_existing_id, 'amount', v_existing_amt,
                    'allowance_consumed', v_existing_cons, 'balance_after', v_balance,
                    'idempotent', true, 'cap_warning', NULL, 'feature_limit_warning', NULL, 'tier_breakdown', v_existing_tier_bd
                );
            END IF;
        END IF;
        RETURN jsonb_build_object('amount', 0, 'balance_after', v_balance, 'idempotent', true, 'tier_breakdown', '{}'::jsonb);
    END IF;
    IF v_status = 'expired' OR v_lease_expires <= now() THEN
        UPDATE public.credit_reservations SET status = 'expired' WHERE id = p_lease_id;
        RETURN jsonb_build_object('error', 'lease_expired', 'balance_after', v_balance);
    END IF;

    -- Zero-cost settle releases the lease without charging (and does not
    -- tag/count anything toward a feature limit — no work happened).
    IF p_amount = 0 THEN
        UPDATE public.credit_reservations SET status = 'settled' WHERE id = p_lease_id;
        RETURN jsonb_build_object('transaction_id', NULL, 'amount', 0, 'balance_after', v_balance, 'idempotent', false, 'tier_breakdown', '{}'::jsonb);
    END IF;

    -- Allowance consume on the actual cost (mirrors deduct_with_allowance).
    -- Skipped when p_skip_allowance = TRUE: fixed-cost batch jobs reserved via
    -- the lease path must not deplete the free inference allowance.
    -- v_period_start: explicit p_period_start else the current UTC calendar
    -- month (unchanged).
    IF NOT p_skip_allowance AND v_plan_id IS NOT NULL THEN
        SELECT free_allowance INTO v_free_allowance FROM public.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM public.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_consume := LEAST(GREATEST(COALESCE(v_free_allowance, 0) - COALESCE(v_used, 0), 0), p_amount);
    END IF;
    v_net := p_amount - v_consume;

    -- Floor enforcement: clamp v_net so the post-settle balance stays ≥ floor.
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

    -- Feature limit is ADVISORY at settle (never blocks — the work already
    -- happened): a breach only sets v_feature_limit_warning, using the
    -- configured action even when it is 'deny' (this is the "prefer deny"
    -- signal — it means the call would have been denied had it gone through
    -- deduct/create_lease). Skipped when no feature/limit was resolved.
    IF p_feature IS NOT NULL AND p_feature_max_calls IS NOT NULL THEN
        -- Deliberately no `amount < 0` filter — see deduct_with_allowance's 4b
        -- block for why (zero-net calls still count as invocations).
        SELECT COUNT(*) INTO v_feature_count
        FROM public.credit_transactions ct
        WHERE ct.user_id = p_user_id
          AND ct.type = 'usage'
          AND ct.metadata->>'feature' = p_feature
          AND ct.created_at >= (p_feature_period_start::timestamp AT TIME ZONE 'UTC')
          AND ct.created_at < (p_feature_period_end::timestamp AT TIME ZONE 'UTC');
        IF v_feature_count >= p_feature_max_calls THEN
            v_feature_limit_warning := p_feature_action;
        END IF;
    END IF;

    IF v_consume > 0 THEN
        INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
        VALUES (p_user_id, v_plan_id, v_period_start, v_consume)
        ON CONFLICT (user_id, plan_id, billing_period)
        DO UPDATE SET usage = public.credit_usage_window.usage + v_consume, updated_at = now();
    END IF;

    -- ── Tier walk: identical algorithm to deduct_with_allowance, applied to
    -- this already-floor-clamped v_net. No special-casing.
    v_tier_remaining := v_net;

    FOR v_walk IN
        SELECT tier_key, priority, 0 AS grp FROM public.credit_tiers
        UNION ALL
        SELECT uct.tier_key, 0, 1 AS grp
        FROM public.user_credit_tiers uct
        WHERE uct.user_id = p_user_id
          AND NOT EXISTS (SELECT 1 FROM public.credit_tiers ct WHERE ct.tier_key = uct.tier_key)
        ORDER BY grp ASC, priority ASC, tier_key ASC
    LOOP
        EXIT WHEN v_tier_remaining <= 0;

        SELECT balance INTO v_tier_balance
        FROM public.user_credit_tiers
        WHERE user_id = p_user_id AND tier_key = v_walk.tier_key
        FOR UPDATE;
        v_tier_balance := COALESCE(v_tier_balance, 0);

        v_take := LEAST(v_tier_balance, v_tier_remaining);
        IF v_take > 0 THEN
            UPDATE public.user_credit_tiers
            SET balance = balance - v_take, updated_at = now()
            WHERE user_id = p_user_id AND tier_key = v_walk.tier_key;

            v_tier_breakdown := v_tier_breakdown || jsonb_build_object(v_walk.tier_key, v_take);
            v_tier_remaining := v_tier_remaining - v_take;
        END IF;
    END LOOP;

    IF v_tier_remaining > 0 THEN
        SELECT tier_key INTO v_sink_tier FROM public.credit_tiers WHERE allow_overdraft = true ORDER BY priority DESC, tier_key DESC LIMIT 1;
        IF v_sink_tier IS NULL THEN
            SELECT tier_key INTO v_sink_tier FROM public.credit_tiers ORDER BY priority DESC, tier_key DESC LIMIT 1;
        END IF;
        IF v_sink_tier IS NULL THEN
            v_sink_tier := 'default';
        END IF;

        INSERT INTO public.user_credit_tiers (user_id, tier_key, balance)
        VALUES (p_user_id, v_sink_tier, -v_tier_remaining)
        ON CONFLICT (user_id, tier_key) DO UPDATE SET
            balance = public.user_credit_tiers.balance - v_tier_remaining,
            updated_at = now();

        v_tier_breakdown := v_tier_breakdown || jsonb_build_object(
            v_sink_tier, COALESCE((v_tier_breakdown->>v_sink_tier)::numeric, 0) + v_tier_remaining
        );
        v_tier_remaining := 0;
    END IF;

    -- Tag metadata.feature whenever p_feature is given (mirrors
    -- deduct_with_allowance) — this is what makes the call countable for
    -- future feature-limit checks, regardless of whether a limit is
    -- currently configured.
    v_metadata := COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model, 'feature', p_feature))
        || jsonb_build_object('allowance_consumed', v_consume, 'balance_after', v_balance - v_net, 'tier_breakdown', v_tier_breakdown);

    UPDATE public.user_credits SET balance = balance - v_net, updated_at = now()
    WHERE user_id = p_user_id RETURNING balance INTO v_new_balance;

    INSERT INTO public.credit_transactions (user_id, amount, type, reference_type, metadata)
    VALUES (p_user_id, -v_net, 'usage', p_metadata->>'reference_type', v_metadata) RETURNING id INTO v_tx_id;

    UPDATE public.credit_reservations SET status = 'settled', settle_tx_id = v_tx_id WHERE id = p_lease_id;

    RETURN jsonb_build_object(
        'transaction_id', v_tx_id, 'amount', v_net, 'allowance_consumed', v_consume,
        'balance_after', v_new_balance, 'idempotent', false, 'cap_warning', v_cap_warning,
        'feature_limit_warning', v_feature_limit_warning,
        'tier_breakdown', v_tier_breakdown
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.settle_lease(UUID, UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN, DATE, TEXT, INT, TEXT, DATE, DATE) FROM PUBLIC, anon, authenticated;

-- release_lease: idempotent release without charge.
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

-- renew_lease: extend an active lease's TTL.
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

-- get_available_credits: advisory available = balance − Σ active holds.
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

-- expire_due_leases: reaper for crashed/abandoned holds.
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
REVOKE EXECUTE ON FUNCTION public.release_lease(UUID, UUID) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.renew_lease(UUID, UUID, INTEGER) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_available_credits(UUID) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.expire_due_leases() FROM PUBLIC, anon, authenticated;

NOTIFY pgrst, 'reload schema';
