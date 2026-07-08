-- bursar: cycle grant revocation.
--
-- Allows service_role to reverse previously granted cycle credits (e.g.
-- subscription cycle grants) by transaction type, recording the reversal
-- as a negative credit_transactions entry.

-- ── 1. Add cycle_grant_revoke to the transaction type enum ────────────────

DO $$ BEGIN
    ALTER TYPE public.credit_tx_type ADD VALUE 'cycle_grant_revoke';
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── 2. RPC: revoke_credits_by_tx_type ─────────────────────────────────────

CREATE OR REPLACE FUNCTION public.revoke_credits_by_tx_type(
    p_user_id UUID,
    p_tx_type TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_total_granted NUMERIC;
    v_total_revoked NUMERIC;
    v_revocable NUMERIC;
    v_new_balance NUMERIC;
    v_tier TEXT;
    v_transaction_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Total credits granted of the given type (LIFO — SUM is order-agnostic,
    -- but if the total is the same regardless of order, LIFO only matters for
    -- the tier resolution below, where we pick the most recent transaction's
    -- tier).
    SELECT COALESCE(SUM(amount), 0) INTO v_total_granted
    FROM public.credit_transactions
    WHERE user_id = p_user_id
      AND type::text = p_tx_type
      AND amount > 0;

    -- Total already revoked for this tx_type (absolute value of negative
    -- cycle_grant_revoke entries that target this tx_type).
    SELECT COALESCE(SUM(ABS(amount)), 0) INTO v_total_revoked
    FROM public.credit_transactions
    WHERE user_id = p_user_id
      AND type = 'cycle_grant_revoke'::public.credit_tx_type
      AND metadata->>'revoked_tx_type' = p_tx_type;

    v_revocable := v_total_granted - v_total_revoked;

    IF v_revocable <= 0 THEN
        SELECT COALESCE(SUM(amount), 0) INTO v_new_balance
        FROM public.credit_transactions
        WHERE user_id = p_user_id;

        RETURN jsonb_build_object(
            'user_id', p_user_id,
            'amount', 0,
            'new_balance', v_new_balance
        );
    END IF;

    -- Use the tier from the most recent grant transaction of this type
    -- (LIFO — the last in is the first out).
    SELECT COALESCE(metadata->>'tier', 'default') INTO v_tier
    FROM public.credit_transactions
    WHERE user_id = p_user_id
      AND type::text = p_tx_type
      AND amount > 0
    ORDER BY created_at DESC
    LIMIT 1;

    -- Insert reversal transaction
    INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
    VALUES (
        p_user_id,
        -v_revocable,
        'cycle_grant_revoke'::public.credit_tx_type,
        jsonb_build_object(
            'revoked_tx_type', p_tx_type,
            'revoked_amount', v_revocable,
            'tier', v_tier
        )
    )
    RETURNING id INTO v_transaction_id;

    -- Deduct from aggregate balance
    UPDATE public.user_credits
    SET balance = balance - v_revocable, updated_at = now()
    WHERE user_id = p_user_id
    RETURNING balance INTO v_new_balance;

    -- Deduct from per-tier balance
    UPDATE public.user_credit_tiers
    SET balance = balance - v_revocable, updated_at = now()
    WHERE user_id = p_user_id AND tier_key = v_tier;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'amount', v_revocable,
        'new_balance', COALESCE(v_new_balance, 0),
        'transaction_id', v_transaction_id,
        'tier', v_tier
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.revoke_credits_by_tx_type(UUID, TEXT) FROM PUBLIC, anon, authenticated;

-- GRANT EXECUTE on all functions to service_role is handled by the existing
-- schema-level grant in 012_feature_limits.sql:81.

NOTIFY pgrst, 'reload schema';
