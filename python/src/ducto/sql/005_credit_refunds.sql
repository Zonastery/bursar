-- ducto: credit refund support.
-- Idempotent — safe to run multiple times (CREATE OR REPLACE).

-- refund_credits: reverse a credit deduction (full or partial).
-- Returns error if transaction not found or already refunded.
CREATE OR REPLACE FUNCTION public.refund_credits(
    p_transaction_id UUID,
    p_amount INTEGER DEFAULT NULL,
    p_reason TEXT DEFAULT NULL,
    p_metadata JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_tx RECORD;
    v_already_refunded BOOLEAN;
    v_refund_amount INTEGER;
    v_refund_tx_id UUID;
    v_new_balance INTEGER;
BEGIN
    -- Prevent concurrent refund on same transaction
    PERFORM pg_advisory_xact_lock(hashtext('refund_' || p_transaction_id));

    IF auth.role() DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Fetch original transaction
    SELECT id, user_id, amount, type INTO v_tx
    FROM public.credit_transactions
    WHERE id = p_transaction_id;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'error', 'transaction_not_found',
            'user_id', '',
            'new_balance', 0
        );
    END IF;

    -- Check for duplicate refund
    SELECT EXISTS (
        SELECT 1 FROM public.credit_transactions
        WHERE reference_id = p_transaction_id AND type = 'refund'
    ) INTO v_already_refunded;

    IF v_already_refunded THEN
        SELECT balance INTO v_new_balance
        FROM public.user_credits
        WHERE user_id = v_tx.user_id;

        RETURN jsonb_build_object(
            'error', 'already_refunded',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- Determine refund amount
    v_refund_amount := COALESCE(p_amount, ABS(v_tx.amount));
    v_refund_amount := LEAST(v_refund_amount, ABS(v_tx.amount));

    -- Restore balance
    UPDATE public.user_credits
    SET balance = balance + v_refund_amount,
        updated_at = now()
    WHERE user_id = v_tx.user_id
    RETURNING balance INTO v_new_balance;

    -- Log refund transaction
    INSERT INTO public.credit_transactions (user_id, amount, type, reference_type, reference_id, metadata)
    VALUES (v_tx.user_id, v_refund_amount, 'refund', p_reason, p_transaction_id,
            p_metadata || jsonb_build_object('reason', p_reason))
    RETURNING id INTO v_refund_tx_id;

    RETURN jsonb_build_object(
        'refund_transaction_id', v_refund_tx_id,
        'user_id', v_tx.user_id,
        'amount', v_refund_amount,
        'new_balance', v_new_balance
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.refund_credits FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
