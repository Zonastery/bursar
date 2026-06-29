-- ducto: 015 — atomic "calculate-then-charge" deduction (contract §2).
--
-- deduct_with_allowance performs the entire deduct pipeline in ONE transaction:
-- lock balance -> user-scoped idempotency -> consume free allowance -> enforce
-- spend cap on the NET amount -> balance-floor check -> debit -> insert ledger
-- row. It is all-or-nothing: any failure (cap deny, insufficient credits, or a
-- racing duplicate) rolls back the allowance consumption and the balance change.
-- This replaces the legacy non-atomic check_allowance/check_spend_cap/
-- reserve_credits/deduct_credits orchestration in the manager (C1/H2/H16).
--
-- Money is NUMERIC(18,4). All windows pinned to UTC for determinism (M16).

CREATE OR REPLACE FUNCTION public.deduct_with_allowance(
    p_user_id         UUID,
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
    v_free_allowance NUMERIC;
    v_period_start   DATE;
    v_used           NUMERIC;
    v_remaining      NUMERIC;
    v_consume        NUMERIC := 0;
    v_net            NUMERIC;
    v_cap            RECORD;
    v_cap_spend      NUMERIC;
    v_cap_window     TIMESTAMPTZ;
    v_cap_warning    TEXT := NULL;
    v_new_balance    NUMERIC;
    v_transaction_id UUID;
    v_metadata       JSONB;
    v_existing_id    UUID;
    v_existing_amt   NUMERIC;
    v_existing_cons  NUMERIC;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Reject non-finite / negative amounts. Zero is a valid no-op charge.
    IF p_amount IS NULL
       OR NOT (p_amount = p_amount)            -- NaN
       OR p_amount = 'Infinity'::numeric
       OR p_amount = '-Infinity'::numeric
       OR p_amount < 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- (1) Lock the balance row, creating it if missing (existing convention).
    SELECT balance, plan_id INTO v_balance, v_plan_id
    FROM public.user_credits
    WHERE user_id = p_user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0)
        ON CONFLICT (user_id) DO NOTHING;

        SELECT balance, plan_id INTO v_balance, v_plan_id
        FROM public.user_credits
        WHERE user_id = p_user_id
        FOR UPDATE;
    END IF;

    -- (2) Idempotency (user-scoped): replay the original result if seen before.
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0)
        INTO v_existing_id, v_existing_amt, v_existing_cons
        FROM public.credit_transactions
        WHERE user_id = p_user_id
          AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;

        IF FOUND THEN
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id,
                'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons,
                'balance_after', v_balance,
                'idempotent', true,
                'cap_warning', NULL
            );
        END IF;
    END IF;

    -- (3) Allowance: consume as much of the cost as the plan's remaining free
    -- allowance covers. The window increment is wrapped in a subtransaction so
    -- a later RAISE (cap deny) rolls the consumption back (contract §2 step 4).
    IF v_plan_id IS NOT NULL THEN
        SELECT free_allowance INTO v_free_allowance
        FROM public.credit_plans WHERE id = v_plan_id;

        v_period_start := (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE;

        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM public.credit_usage_window
        WHERE user_id = p_user_id
          AND plan_id = v_plan_id
          AND billing_period = v_period_start;

        v_remaining := GREATEST(COALESCE(v_free_allowance, 0) - COALESCE(v_used, 0), 0);
        v_consume   := LEAST(v_remaining, p_amount);
    END IF;

    v_net := p_amount - v_consume;

    -- Build the merged metadata: caller metadata first, system fields last so
    -- the system-owned keys win (contract §5).
    v_metadata := COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_strip_nulls(jsonb_build_object(
               'idempotency_key', p_idempotency_key,
               'model', p_model
           ))
        || jsonb_build_object('allowance_consumed', v_consume);

    -- The mutating section is wrapped so that any RAISE inside it rolls back the
    -- usage-window increment as well as the balance change (all-or-nothing).
    BEGIN
        IF v_consume > 0 THEN
            INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
            VALUES (p_user_id, v_plan_id, v_period_start, v_consume)
            ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
                usage = public.credit_usage_window.usage + v_consume,
                updated_at = now();
        END IF;

        -- (4) Spend cap on the NET amount. Deny caps abort (RAISE) so allowance
        -- is NOT consumed on a denied deduction. Warn/notify set a flag only.
        FOR v_cap IN
            SELECT action, cap_type, model, cap_limit
            FROM public.credit_spend_caps
            WHERE user_id = p_user_id
              AND (model IS NULL OR model = p_model)
            ORDER BY (action = 'deny') DESC, cap_limit ASC
        LOOP
            v_cap_window := CASE v_cap.cap_type
                WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
                ELSE date_trunc('month', now() AT TIME ZONE 'UTC')
            END;

            SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_cap_spend
            FROM public.credit_transactions ct
            WHERE ct.user_id = p_user_id
              AND ct.type IN ('usage', 'team_usage')
              AND ct.amount < 0
              AND ct.created_at >= v_cap_window
              AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);

            IF v_cap_spend + v_net > v_cap.cap_limit THEN
                IF v_cap.action = 'deny' THEN
                    -- Abort: rolls back the usage-window increment above.
                    -- DU001 is a ducto-private SQLSTATE for "cap reached".
                    RAISE EXCEPTION 'ducto_cap_reached'
                        USING ERRCODE = 'DU001';
                ELSE
                    -- Soft cap: record the strongest warning seen and continue.
                    IF v_cap_warning IS NULL THEN
                        v_cap_warning := v_cap.action;
                    END IF;
                END IF;
            END IF;
        END LOOP;

        -- (5) Balance floor on the NET amount.
        -- DU002 is a ducto-private SQLSTATE for "insufficient credits".
        IF v_balance - v_net < p_min_balance THEN
            RAISE EXCEPTION 'ducto_insufficient_credits'
                USING ERRCODE = 'DU002';
        END IF;

        -- (6) Debit and insert the ledger row.
        UPDATE public.user_credits
        SET balance = balance - v_net,
            updated_at = now()
        WHERE user_id = p_user_id
        RETURNING balance INTO v_new_balance;

        INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
        VALUES (p_user_id, -v_net, 'usage', v_metadata)
        RETURNING id INTO v_transaction_id;

    EXCEPTION
        WHEN SQLSTATE 'DU001' THEN
            -- Spend cap reached (deny): allowance consumption rolled back.
            RETURN jsonb_build_object('error', 'cap_reached', 'action', 'deny');
        WHEN SQLSTATE 'DU002' THEN
            -- Balance floor breached: allowance consumption rolled back.
            RETURN jsonb_build_object('error', 'insufficient_credits');
        WHEN unique_violation THEN
            -- A concurrent caller with the same (user_id, idempotency_key) won.
            -- Roll back our window/balance changes and replay the original.
            SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0)
            INTO v_existing_id, v_existing_amt, v_existing_cons
            FROM public.credit_transactions
            WHERE user_id = p_user_id
              AND metadata->>'idempotency_key' = p_idempotency_key
            LIMIT 1;

            RETURN jsonb_build_object(
                'transaction_id', v_existing_id,
                'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons,
                'balance_after', v_balance,
                'idempotent', true,
                'cap_warning', NULL
            );
    END;

    -- (7) Structured result.
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

-- Defense-in-depth: backend-only RPC.
REVOKE EXECUTE ON FUNCTION public.deduct_with_allowance(UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB) FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
