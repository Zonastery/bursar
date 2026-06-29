-- ducto: credit management RPCs.
-- All functions use OR REPLACE for idempotent setup.
-- All mutation functions require service_role (backend-only).

-- Money columns moved from INTEGER to NUMERIC(18,4) (M11). Because CREATE OR
-- REPLACE FUNCTION cannot change a parameter's type (it would create a second
-- overload instead), drop any pre-existing INTEGER-signature versions so the
-- NUMERIC definitions below fully replace them. Safe no-ops on a fresh install.
DROP FUNCTION IF EXISTS public.credits_add(UUID, INTEGER, public.credit_tx_type, JSONB);
DROP FUNCTION IF EXISTS public.reserve_credits(UUID, INTEGER, TEXT, JSONB, INTEGER);
DROP FUNCTION IF EXISTS public.deduct_credits(UUID, UUID, INTEGER, JSONB);

-- credits_add: Atomically add credits to user's balance and log transaction.
-- Money is NUMERIC(18,4). Purchases must be a positive, finite amount;
-- only the explicit 'adjustment' type may carry a negative/zero amount.
CREATE OR REPLACE FUNCTION public.credits_add(
    p_user_id UUID,
    p_amount NUMERIC,
    p_type public.credit_tx_type DEFAULT 'adjustment',
    p_metadata JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_new_balance NUMERIC;
    v_lifetime NUMERIC;
    v_transaction_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Reject non-finite amounts (NaN / +-Infinity) outright.
    IF p_amount IS NULL OR NOT (p_amount = p_amount) OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- Purchases (and other credit grants) must be strictly positive.
    -- Negative/zero amounts are only allowed via an explicit 'adjustment'.
    IF p_type <> 'adjustment' AND p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
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
--
-- Canonical semantics (contract §3): the reservation is REJECTED if reserving
-- the full requested amount would push available balance below p_min_balance,
-- i.e. (available - p_amount) < p_min_balance. The amount is NEVER silently
-- capped — both SQL and MemoryStore reject identically. Money is NUMERIC(18,4).
CREATE OR REPLACE FUNCTION public.reserve_credits(
    p_user_id UUID,
    p_amount NUMERIC,
    p_operation_type TEXT,
    p_metadata JSONB DEFAULT NULL,
    p_min_balance NUMERIC DEFAULT 5
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance NUMERIC;
    v_reserved NUMERIC;
    v_available NUMERIC;
    v_reservation_id UUID;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF p_amount IS NULL OR p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
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

    -- Reject (do NOT cap) if reserving the full amount would breach the floor.
    IF v_available - p_amount < p_min_balance THEN
        RETURN jsonb_build_object(
            'error', 'insufficient_credits',
            'available', v_available,
            'balance', v_balance,
            'reserved', v_reserved,
            'min_balance', p_min_balance
        );
    END IF;

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


-- deduct_credits: Finalize a deduction against an existing reservation,
-- then release the reservation. Money is NUMERIC(18,4).
--
-- The reservation is the authority on the maximum deductible amount (C3):
--   * lock the reservation row FOR UPDATE,
--   * require it to exist, belong to this user, and be unexpired,
--   * clamp p_amount <= reservation.amount.
-- Idempotency (H16) is user-scoped: the lookup AND the unique index are keyed
-- on (user_id, idempotency_key); the insert is wrapped so a concurrent
-- duplicate (unique_violation) re-selects and returns the original result.
CREATE OR REPLACE FUNCTION public.deduct_credits(
    p_user_id UUID,
    p_reservation_id UUID,
    p_amount NUMERIC,
    p_metadata JSONB DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_current_balance NUMERIC;
    v_new_balance NUMERIC;
    v_transaction_id UUID;
    v_ref_id UUID;
    v_idempotency_key TEXT;
    v_operation_type TEXT;
    v_reservation_amount NUMERIC;
    v_amount NUMERIC := p_amount;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF v_amount IS NULL OR v_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    v_idempotency_key := p_metadata->>'idempotency_key';

    -- Idempotency check (user-scoped): return existing tx if key already used.
    IF v_idempotency_key IS NOT NULL THEN
        SELECT id INTO v_transaction_id
        FROM public.credit_transactions
        WHERE user_id = p_user_id
          AND metadata->>'idempotency_key' = v_idempotency_key;

        IF FOUND THEN
            RETURN jsonb_build_object(
                'id', v_transaction_id,
                'user_id', p_user_id,
                'amount', -v_amount,
                'new_balance', (SELECT balance FROM public.user_credits WHERE user_id = p_user_id),
                'idempotent', true
            );
        END IF;
    END IF;

    -- Lock the balance row BEFORE any mutation
    SELECT balance INTO v_current_balance
    FROM public.user_credits
    WHERE user_id = p_user_id
    FOR UPDATE;

    IF v_current_balance IS NULL THEN
        RETURN jsonb_build_object('error', 'no_balance_record');
    END IF;

    -- Lock and validate the reservation: it is the spend ceiling (C3).
    SELECT amount, operation_type
    INTO v_reservation_amount, v_operation_type
    FROM public.credit_reservations
    WHERE id = p_reservation_id
      AND user_id = p_user_id
      AND expires_at > now()
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'not_found', 'reservation_id', p_reservation_id);
    END IF;

    -- Clamp the deducted amount to the reserved ceiling.
    v_amount := LEAST(v_amount, v_reservation_amount);

    IF v_current_balance < v_amount THEN
        RETURN jsonb_build_object('error', 'insufficient_credits', 'available', v_current_balance);
    END IF;

    -- Deduct atomically
    UPDATE public.user_credits
    SET balance = balance - v_amount,
        updated_at = now()
    WHERE user_id = p_user_id
    RETURNING balance INTO v_new_balance;

    -- Parse reference_id from metadata (gracefully handle bad UUIDs)
    BEGIN
        v_ref_id := (p_metadata->>'reference_id')::UUID;
    EXCEPTION WHEN OTHERS THEN
        v_ref_id := NULL;
    END;

    -- Insert ledger row; concurrent duplicate idempotency key -> re-select original.
    BEGIN
        INSERT INTO public.credit_transactions
            (user_id, amount, type, reference_type, reference_id, metadata)
        VALUES
            (p_user_id, -v_amount, 'usage',
             COALESCE(p_metadata->>'reference_type', v_operation_type),
             v_ref_id,
             p_metadata)
        RETURNING id INTO v_transaction_id;
    EXCEPTION WHEN unique_violation THEN
        -- A concurrent call with the same (user_id, idempotency_key) won the race.
        SELECT id INTO v_transaction_id
        FROM public.credit_transactions
        WHERE user_id = p_user_id
          AND metadata->>'idempotency_key' = v_idempotency_key;
        RETURN jsonb_build_object(
            'id', v_transaction_id,
            'user_id', p_user_id,
            'amount', -v_amount,
            'new_balance', (SELECT balance FROM public.user_credits WHERE user_id = p_user_id),
            'idempotent', true
        );
    END;

    -- Release reservation
    DELETE FROM public.credit_reservations WHERE id = p_reservation_id AND user_id = p_user_id;

    RETURN jsonb_build_object(
        'id', v_transaction_id,
        'user_id', p_user_id,
        'amount', -v_amount,
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
    v_balance NUMERIC;
    v_lifetime NUMERIC;
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
