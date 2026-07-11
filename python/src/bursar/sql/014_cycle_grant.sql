-- bursar: cycle grant revocation.
--
-- Allows service_role to reverse previously granted cycle credits (e.g.
-- subscription cycle grants) by transaction type, recording the reversal
-- as a negative credit_transactions entry.

-- ── 1. Add cycle_grant + cycle_grant_revoke to the transaction type enum ───

DO $$ BEGIN
    ALTER TYPE public.credit_tx_type ADD VALUE 'cycle_grant';
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
    v_current_balance NUMERIC;
    v_remaining NUMERIC;
    v_to_deduct NUMERIC;
    v_bucket_row RECORD;
    v_first_bucket TEXT;
    v_transaction_id UUID;
    v_new_balance NUMERIC;
BEGIN
    -- Total credits granted of the given type
    SELECT COALESCE(SUM(amount), 0) INTO v_total_granted
    FROM public.credit_transactions
    WHERE user_id = p_user_id
      AND type::text = p_tx_type
      AND amount > 0;

    -- Total already revoked for this tx_type
    SELECT COALESCE(SUM(ABS(amount)), 0) INTO v_total_revoked
    FROM public.credit_transactions
    WHERE user_id = p_user_id
      AND type = 'cycle_grant_revoke'::public.credit_tx_type
      AND metadata->>'revoked_tx_type' = p_tx_type;

    v_revocable := v_total_granted - v_total_revoked;

    -- Cap at the user's current balance (parity with MemoryStore).
    SELECT COALESCE(balance, 0) INTO v_current_balance
    FROM public.user_credits
    WHERE user_id = p_user_id;

    v_revocable := LEAST(v_revocable, v_current_balance);

    IF v_revocable <= 0 THEN
        RETURN jsonb_build_object(
            'user_id', p_user_id,
            'amount', 0,
            'new_balance', v_current_balance
        );
    END IF;

    -- Priority-walk across buckets (parity with MemoryStore's _walk_tiers):
    -- drain configured buckets in ascending priority order, then any bucket keys
    -- the user holds a nonzero balance in that are no longer configured
    -- ("config drift" safety net).
    v_remaining := v_revocable;
    FOR v_bucket_row IN
        SELECT uct.bucket_key, uct.balance
        FROM public.user_credit_buckets uct
        LEFT JOIN public.credit_buckets ct ON ct.bucket_key = uct.bucket_key
        WHERE uct.user_id = p_user_id AND uct.balance > 0
        ORDER BY COALESCE(ct.priority, 999999) ASC, uct.bucket_key ASC
    LOOP
        v_to_deduct := LEAST(v_bucket_row.balance, v_remaining);
        UPDATE public.user_credit_buckets
        SET balance = balance - v_to_deduct, updated_at = now()
        WHERE user_id = p_user_id AND bucket_key = v_bucket_row.bucket_key;
        v_remaining := v_remaining - v_to_deduct;

        IF v_first_bucket IS NULL THEN
            v_first_bucket := v_bucket_row.bucket_key;
        END IF;

        EXIT WHEN v_remaining <= 0;
    END LOOP;

    -- If the user has no bucket rows (edge case), create one in the default bucket
    -- so the aggregate/per-tier invariant stays intact.
    IF v_first_bucket IS NULL THEN
        v_first_bucket := 'default';
        INSERT INTO public.user_credit_buckets (user_id, bucket_key, balance)
        VALUES (p_user_id, v_first_bucket, -v_revocable)
        ON CONFLICT (user_id, bucket_key) DO UPDATE SET
            balance = public.user_credit_buckets.balance - v_revocable,
            updated_at = now();
    END IF;

    -- Insert reversal transaction
    INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
    VALUES (
        p_user_id,
        -v_revocable,
        'cycle_grant_revoke'::public.credit_tx_type,
        jsonb_build_object(
            'revoked_tx_type', p_tx_type,
            'revoked_amount', v_revocable,
            'bucket', v_first_bucket
        )
    )
    RETURNING id INTO v_transaction_id;

    -- Deduct from aggregate balance
    UPDATE public.user_credits
    SET balance = balance - v_revocable, updated_at = now()
    WHERE user_id = p_user_id
    RETURNING balance INTO v_new_balance;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'amount', v_revocable,
        'new_balance', COALESCE(v_new_balance, 0),
        'transaction_id', v_transaction_id,
        'bucket', v_first_bucket
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.revoke_credits_by_tx_type(UUID, TEXT) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;

NOTIFY pgrst, 'reload schema';
