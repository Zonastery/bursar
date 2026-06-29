-- ducto: credit refund support.
-- Idempotent — safe to run multiple times (CREATE OR REPLACE).

-- Money moved INTEGER -> NUMERIC(18,4) (M11). Drop the old INTEGER-amount
-- overload so the NUMERIC definition fully replaces it (no-op on fresh installs).
DROP FUNCTION IF EXISTS public.refund_credits(UUID, INTEGER, TEXT, JSONB);

-- refund_credits: reverse a credit deduction (full or partial), atomically.
--
-- Everything below happens in ONE transaction. The original ledger row and the
-- balance row are taken FOR UPDATE so concurrent refunds against the same
-- original transaction serialize and cannot race the over-refund check
-- (contract §4). All money is NUMERIC(18,4).
--
-- Business outcomes (structured `{"error": code}` envelope; the manager maps
-- codes to typed exceptions per §4):
--   * not_found       — no such original transaction.
--   * over_refund     — refunding would exceed the original debit, OR the
--                       referenced transaction is NOT a debit (a credit /
--                       purchase / refund / adjustment has zero refundable
--                       amount, so any refund over-refunds). See note below on
--                       why over_refund (not not_found) is used for non-debits.
--   * already_refunded — an exact duplicate of a prior full refund (back-compat).
--
-- On success returns: refund_transaction_id, user_id, amount, new_balance.
-- All error envelopes also carry user_id + new_balance so the store/manager can
-- surface the current balance uniformly.
CREATE OR REPLACE FUNCTION public.refund_credits(
    p_transaction_id UUID,
    p_amount NUMERIC DEFAULT NULL,
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
    v_original_debit NUMERIC;      -- positive magnitude of the original debit
    v_prior_refunded NUMERIC;      -- sum of all prior refunds for this original
    v_remaining NUMERIC;           -- still-refundable amount
    v_refund_amount NUMERIC;
    v_new_balance NUMERIC;
    v_refund_tx_id UUID;
BEGIN
    -- Prevent concurrent refund on same transaction (advisory + row locks below).
    PERFORM pg_advisory_xact_lock(hashtext('refund_' || p_transaction_id));

    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Fetch + lock the original transaction row so its refund total cannot move
    -- under us while we compute the over-refund check.
    SELECT id, user_id, amount, type INTO v_tx
    FROM public.credit_transactions
    WHERE id = p_transaction_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'error', 'not_found',
            'user_id', '',
            'new_balance', 0
        );
    END IF;

    -- Lock the balance row up front. Same lock the debit took, so a refund and a
    -- concurrent deduct on the same user serialize. Created if missing (the row
    -- should already exist for any user with a prior debit, but be defensive).
    SELECT balance INTO v_new_balance
    FROM public.user_credits
    WHERE user_id = v_tx.user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (v_tx.user_id, 0, 0)
        ON CONFLICT (user_id) DO NOTHING;

        SELECT balance INTO v_new_balance
        FROM public.user_credits
        WHERE user_id = v_tx.user_id
        FOR UPDATE;
    END IF;

    -- (2) Reject refunding a non-debit. Only a `usage`/`team_usage` deduction
    -- (negative amount) is refundable. A purchase / refund / adjustment / bonus
    -- has nothing to give back, so its refundable amount is 0 and ANY refund
    -- over-refunds. We return `over_refund` (not `not_found`) because the row
    -- DOES exist — `not_found` would be misleading; `over_refund` precisely says
    -- "more than is refundable" (which for a non-debit is anything > 0).
    IF v_tx.type NOT IN ('usage', 'team_usage') OR v_tx.amount >= 0 THEN
        RETURN jsonb_build_object(
            'error', 'over_refund',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- Positive magnitude of the original debit (amount is negative for a debit).
    v_original_debit := ABS(v_tx.amount);

    -- (3a) Back-compat duplicate detection: a prior FULL refund of this exact
    -- transaction (one refund row whose amount equals the full original debit)
    -- replays as `already_refunded`. Cumulative partials are NOT treated as
    -- duplicates here — they fall through to the over-refund cap in (1)/(3b).
    SELECT EXISTS (
        SELECT 1 FROM public.credit_transactions
        WHERE reference_id = p_transaction_id
          AND type = 'refund'
          AND amount = v_original_debit
    ) INTO v_already_refunded;

    IF v_already_refunded THEN
        RETURN jsonb_build_object(
            'error', 'already_refunded',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- Determine the requested refund amount (NULL ⇒ full remaining).
    -- Sum of all prior refunds for this original (refund rows store a positive
    -- amount). Read under the FOR UPDATE lock taken above.
    SELECT COALESCE(SUM(amount), 0) INTO v_prior_refunded
    FROM public.credit_transactions
    WHERE reference_id = p_transaction_id
      AND type = 'refund';

    v_remaining := v_original_debit - v_prior_refunded;

    -- Requested amount: explicit value, else the full remaining refundable.
    v_refund_amount := COALESCE(p_amount, v_remaining);

    -- (1) Over-refund rejection: prior refunds + this refund must not exceed the
    -- original debit. Equivalently: this refund must not exceed what remains.
    -- A non-positive request (<= 0), or one that exceeds the remaining balance
    -- (including the case where the original is already fully refunded so
    -- v_remaining = 0), is rejected WITHOUT refunding.
    IF v_refund_amount <= 0 OR v_refund_amount > v_remaining THEN
        RETURN jsonb_build_object(
            'error', 'over_refund',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- (3b) Apply: restore balance and append the refund ledger row. Cumulative
    -- partials accumulate via successive refund rows; the cap above guarantees
    -- the running total never exceeds v_original_debit.
    UPDATE public.user_credits
    SET balance = balance + v_refund_amount,
        updated_at = now()
    WHERE user_id = v_tx.user_id
    RETURNING balance INTO v_new_balance;

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
