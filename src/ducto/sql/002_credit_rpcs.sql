-- ducto: credit management RPCs.
-- All functions use OR REPLACE for idempotent setup.
-- All mutation functions require service_role (backend-only).

-- credits_add: Atomically add credits to user's balance and log transaction.
CREATE OR REPLACE FUNCTION public.credits_add(
    p_user_id UUID,
    p_amount INTEGER,
    p_type public.credit_tx_type DEFAULT 'adjustment',
    p_metadata JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_new_balance INTEGER;
    v_lifetime INTEGER;
    v_transaction_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
    VALUES (p_user_id, p_amount, CASE WHEN p_type = 'purchase' THEN p_amount ELSE 0 END)
    ON CONFLICT (user_id) DO UPDATE SET
        balance = public.user_credits.balance + p_amount,
        lifetime_purchased = CASE WHEN p_type = 'purchase'
            THEN public.user_credits.lifetime_purchased + p_amount
            ELSE public.user_credits.lifetime_purchased
        END,
        updated_at = now()
    RETURNING balance, lifetime_purchased INTO v_new_balance, v_lifetime;

    INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
    VALUES (p_user_id, p_amount, p_type, p_metadata)
    RETURNING id INTO v_transaction_id;

    RETURN jsonb_build_object(
        'id', v_transaction_id,
        'user_id', p_user_id,
        'amount', p_amount,
        'new_balance', v_new_balance,
        'lifetime_purchased', v_lifetime
    );
END;
$$;


-- reserve_credits: Optimistic concurrency guard.
-- Locks user row, checks available balance (including min_balance floor),
-- creates a time-bounded reservation. Prevents double-spending.
CREATE OR REPLACE FUNCTION public.reserve_credits(
    p_user_id UUID,
    p_amount INTEGER,
    p_operation_type TEXT,
    p_metadata JSONB DEFAULT NULL,
    p_min_balance INTEGER DEFAULT 5
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance INTEGER;
    v_reserved INTEGER;
    v_available INTEGER;
    v_reservation_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Lock row so concurrent calls see an accurate (not stale) balance
    SELECT COALESCE(balance, 0) INTO v_balance
    FROM public.user_credits
    WHERE user_id = p_user_id
    FOR UPDATE;

    IF v_balance IS NULL THEN
        RETURN jsonb_build_object('error', 'no_balance_record');
    END IF;

    -- Clean up expired reservations (keeps the table bounded)
    DELETE FROM public.credit_reservations
    WHERE user_id = p_user_id AND expires_at <= now();

    -- Sum currently active reservations
    SELECT COALESCE(SUM(amount), 0) INTO v_reserved
    FROM public.credit_reservations
    WHERE user_id = p_user_id AND expires_at > now();

    v_available := v_balance - v_reserved;

    IF v_available < p_min_balance THEN
        RETURN jsonb_build_object(
            'error', 'insufficient_credits',
            'available', v_available,
            'balance', v_balance,
            'reserved', v_reserved,
            'min_balance', p_min_balance
        );
    END IF;

    -- Cap requested amount to what's actually available
    p_amount := LEAST(p_amount, v_available);

    INSERT INTO public.credit_reservations (user_id, amount, operation_type, metadata)
    VALUES (p_user_id, p_amount, p_operation_type, COALESCE(p_metadata, '{}'::jsonb))
    RETURNING id INTO v_reservation_id;

    RETURN jsonb_build_object(
        'reservation_id', v_reservation_id,
        'user_id', p_user_id,
        'amount', p_amount,
        'balance', v_balance,
        'reserved', v_reserved + p_amount
    );
END;
$$;


-- deduct_credits: Finalize a deduction, release the reservation.
-- Idempotency supported via metadata->>'idempotency_key'.
CREATE OR REPLACE FUNCTION public.deduct_credits(
    p_user_id UUID,
    p_reservation_id UUID,
    p_amount INTEGER,
    p_metadata JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_current_balance INTEGER;
    v_new_balance INTEGER;
    v_transaction_id UUID;
    v_ref_id UUID;
    v_idempotency_key TEXT;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    v_idempotency_key := COALESCE(p_metadata->>'idempotency_key', NULL);

    -- Idempotency check: return existing transaction if this key was already processed
    IF v_idempotency_key IS NOT NULL THEN
        SELECT id INTO v_transaction_id
        FROM public.credit_transactions
        WHERE metadata->>'idempotency_key' = v_idempotency_key;

        IF FOUND THEN
            RETURN jsonb_build_object(
                'id', v_transaction_id,
                'user_id', p_user_id,
                'amount', -p_amount,
                'new_balance', (SELECT balance FROM public.user_credits WHERE user_id = p_user_id),
                'idempotent', true
            );
        END IF;
    END IF;

    -- Lock row and check balance BEFORE any mutation
    SELECT balance INTO v_current_balance
    FROM public.user_credits
    WHERE user_id = p_user_id
    FOR UPDATE;

    IF v_current_balance IS NULL THEN
        RETURN jsonb_build_object('error', 'no_balance_record');
    END IF;

    IF v_current_balance < p_amount THEN
        RETURN jsonb_build_object('error', 'insufficient_credits', 'available', v_current_balance);
    END IF;

    -- Deduct atomically
    UPDATE public.user_credits
    SET balance = balance - p_amount,
        updated_at = now()
    WHERE user_id = p_user_id
    RETURNING balance INTO v_new_balance;

    -- Parse reference_id from metadata (gracefully handle bad UUIDs)
    BEGIN
        v_ref_id := (p_metadata->>'reference_id')::UUID;
    EXCEPTION WHEN OTHERS THEN
        v_ref_id := NULL;
    END;

    INSERT INTO public.credit_transactions
        (user_id, amount, type, reference_type, reference_id, metadata)
    VALUES
        (p_user_id, -p_amount, 'usage',
         p_metadata->>'reference_type',
         v_ref_id,
         p_metadata)
    RETURNING id INTO v_transaction_id;

    -- Release reservation
    DELETE FROM public.credit_reservations WHERE id = p_reservation_id AND user_id = p_user_id;

    RETURN jsonb_build_object(
        'id', v_transaction_id,
        'user_id', p_user_id,
        'amount', -p_amount,
        'new_balance', v_new_balance,
        'idempotent', false
    );
END;
$$;


-- get_credits_balance: Read current balance and lifetime purchased total.
CREATE OR REPLACE FUNCTION public.get_credits_balance(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance INTEGER;
    v_lifetime INTEGER;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT balance, lifetime_purchased INTO v_balance, v_lifetime
    FROM public.user_credits
    WHERE user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'balance', COALESCE(v_balance, 0),
        'lifetime_purchased', COALESCE(v_lifetime, 0)
    );
END;
$$;


-- Defense-in-depth: revoke direct execute from user roles.
-- Only service_role RPC calls (via Supabase client with service key) should succeed.
REVOKE EXECUTE ON FUNCTION public.credits_add FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.reserve_credits FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.deduct_credits FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_credits_balance FROM anon, authenticated;

-- Refresh PostgREST schema cache so REST API can resolve the new RPCs.
NOTIFY pgrst, 'reload schema';
