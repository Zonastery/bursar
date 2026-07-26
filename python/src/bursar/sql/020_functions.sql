-- Canonical account and ledger functions.

CREATE FUNCTION bursar.ensure_personal_account(p_user_id uuid)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account_id uuid;
BEGIN
    INSERT INTO bursar.credit_accounts(account_type, user_id)
    VALUES ('personal', p_user_id)
    ON CONFLICT (user_id) WHERE user_id IS NOT NULL DO NOTHING;

    SELECT id INTO v_account_id
    FROM bursar.credit_accounts
    WHERE user_id = p_user_id;

    RETURN v_account_id;
END;
$$;

CREATE FUNCTION bursar.post_ledger_entry(
    p_account_id uuid,
    p_actor_user_id uuid,
    p_amount numeric,
    p_entry_type text,
    p_reference_entry_id uuid DEFAULT NULL,
    p_reversal_entry_id uuid DEFAULT NULL,
    p_idempotency_key text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_expires_at timestamptz DEFAULT NULL,
    p_bucket text DEFAULT 'default',
    p_min_balance numeric DEFAULT NULL
)
RETURNS TABLE(entry_id uuid, balance_after numeric, idempotent boolean)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance numeric(18,4);
    v_new_balance numeric(18,4);
    v_entry_id uuid;
    v_remaining numeric(18,4);
    v_take numeric(18,4);
    v_lot record;
BEGIN
    IF p_amount = 0 OR NOT bursar.is_finite_numeric(p_amount) THEN
        RAISE EXCEPTION 'ledger amount must be finite and non-zero';
    END IF;

    SELECT balance INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = p_account_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'credit account not found';
    END IF;

    IF p_idempotency_key IS NOT NULL THEN
        SELECT id INTO v_entry_id
        FROM bursar.credit_ledger_entries
        WHERE account_id = p_account_id
          AND idempotency_key = p_idempotency_key;
        IF FOUND THEN
            RETURN QUERY SELECT v_entry_id, v_balance, true;
            RETURN;
        END IF;
    END IF;

    v_new_balance := v_balance + p_amount;
    IF p_min_balance IS NOT NULL AND v_new_balance < p_min_balance THEN
        RAISE EXCEPTION 'insufficient_credits';
    END IF;

    INSERT INTO bursar.credit_ledger_entries(
        account_id,
        actor_user_id,
        amount,
        entry_type,
        reference_entry_id,
        reversal_entry_id,
        idempotency_key,
        metadata
    )
    VALUES (
        p_account_id,
        p_actor_user_id,
        p_amount,
        p_entry_type,
        p_reference_entry_id,
        p_reversal_entry_id,
        p_idempotency_key,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    RETURNING id INTO v_entry_id;

    UPDATE bursar.credit_accounts
    SET balance = v_new_balance,
        updated_at = now()
    WHERE id = p_account_id;

    IF p_amount > 0 AND p_entry_type NOT IN ('refund', 'reversal') THEN
        INSERT INTO bursar.credit_lots(
            account_id,
            source_entry_id,
            granted,
            expires_at,
            bucket
        )
        VALUES (
            p_account_id,
            v_entry_id,
            p_amount,
            p_expires_at,
            COALESCE(NULLIF(p_bucket, ''), 'default')
        );
    ELSIF p_amount < 0 THEN
        v_remaining := -p_amount;
        FOR v_lot IN
            SELECT l.id, l.granted - l.consumed AS available
            FROM bursar.credit_lots l
            LEFT JOIN bursar.credit_buckets b ON b.bucket_key = l.bucket
            WHERE l.account_id = p_account_id
              AND l.consumed < l.granted
              AND (l.expires_at IS NULL OR l.expires_at > now())
            ORDER BY COALESCE(b.priority, 2147483647),
                     l.expires_at NULLS LAST,
                     l.created_at,
                     l.id
            FOR UPDATE OF l
        LOOP
            EXIT WHEN v_remaining <= 0;
            v_take := LEAST(v_remaining, v_lot.available);
            UPDATE bursar.credit_lots
            SET consumed = consumed + v_take
            WHERE id = v_lot.id;
            INSERT INTO bursar.credit_lot_allocations(debit_entry_id, lot_id, amount)
            VALUES (v_entry_id, v_lot.id, v_take);
            v_remaining := v_remaining - v_take;
        END LOOP;
    END IF;

    RETURN QUERY SELECT v_entry_id, v_new_balance, false;
END;
$$;

CREATE FUNCTION bursar.get_credits_balance(p_user_id uuid)
RETURNS TABLE(user_id uuid, balance numeric, lifetime_purchased numeric)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT p_user_id,
           a.balance,
           COALESCE((
               SELECT SUM(e.amount)
               FROM bursar.credit_ledger_entries e
               WHERE e.account_id = a.id
                 AND e.amount > 0
                 AND e.entry_type IN ('purchase', 'topup')
           ), 0)::numeric
    FROM bursar.credit_accounts a
    WHERE a.user_id = p_user_id
$$;

CREATE FUNCTION bursar.credits_add(
    p_user_id uuid,
    p_amount numeric,
    p_type text DEFAULT 'adjustment',
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_expires_at timestamptz DEFAULT NULL,
    p_bucket text DEFAULT NULL,
    p_idempotency_key text DEFAULT NULL
)
RETURNS TABLE(
    entry_id uuid,
    user_id uuid,
    amount numeric,
    new_balance numeric,
    lifetime_purchased numeric,
    bucket text,
    idempotent boolean,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account_id uuid;
    v_result record;
    v_bucket text;
BEGIN
    v_account_id := bursar.ensure_personal_account(p_user_id);
    v_bucket := COALESCE(
        p_bucket,
        (SELECT bucket_key FROM bursar.credit_buckets
         WHERE is_default AND status = 'active'
         ORDER BY priority, bucket_key LIMIT 1),
        'default'
    );

    SELECT * INTO v_result
    FROM bursar.post_ledger_entry(
        v_account_id,
        p_user_id,
        p_amount,
        p_type,
        NULL,
        NULL,
        p_idempotency_key,
        p_metadata,
        p_expires_at,
        v_bucket,
        NULL
    );

    RETURN QUERY
    SELECT v_result.entry_id,
           p_user_id,
           p_amount,
           v_result.balance_after,
           COALESCE((
               SELECT SUM(e.amount)
               FROM bursar.credit_ledger_entries e
               WHERE e.account_id = v_account_id
                 AND e.amount > 0
                 AND e.entry_type IN ('purchase', 'topup')
           ), 0),
           v_bucket,
           v_result.idempotent,
           NULL::text;
EXCEPTION
    WHEN OTHERS THEN
        RETURN QUERY
        SELECT NULL::uuid, p_user_id, p_amount, NULL::numeric, 0::numeric,
               COALESCE(p_bucket, 'default'), false, SQLERRM;
END;
$$;

CREATE FUNCTION bursar.get_available_credits(p_user_id uuid)
RETURNS TABLE(balance numeric, reserved numeric, available numeric)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT a.balance,
           COALESCE(SUM(l.amount) FILTER (
               WHERE l.status = 'active' AND l.expires_at > now()
           ), 0)::numeric AS reserved,
           a.balance - COALESCE(SUM(l.amount) FILTER (
               WHERE l.status = 'active' AND l.expires_at > now()
           ), 0)::numeric AS available
    FROM bursar.credit_accounts a
    LEFT JOIN bursar.credit_leases l ON l.account_id = a.id
    WHERE a.user_id = p_user_id
    GROUP BY a.id, a.balance
$$;

CREATE FUNCTION bursar.deduct_with_allowance(
    p_user_id uuid,
    p_amount numeric,
    p_idempotency_key text DEFAULT NULL,
    p_min_balance numeric DEFAULT 0,
    p_model text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_skip_allowance boolean DEFAULT false,
    p_period_start date DEFAULT NULL,
    p_feature text DEFAULT NULL,
    p_feature_max_calls integer DEFAULT NULL,
    p_feature_action text DEFAULT NULL,
    p_feature_period_start date DEFAULT NULL,
    p_feature_period_end date DEFAULT NULL
)
RETURNS TABLE(
    entry_id uuid,
    user_id uuid,
    amount numeric,
    balance_after numeric,
    allowance_consumed numeric,
    idempotent boolean,
    cap_warning text,
    feature_limit_warning text,
    bucket_breakdown jsonb,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account_id uuid;
    v_allowance numeric(18,4) := 0;
    v_used numeric(18,4) := 0;
    v_free numeric(18,4) := 0;
    v_billable numeric(18,4);
    v_result record;
    v_balance numeric(18,4);
    v_metadata jsonb;
    v_feature_limited boolean := false;
    v_feature_warning text;
BEGIN
    IF p_amount < 0 OR NOT bursar.is_finite_numeric(p_amount) THEN
        RAISE EXCEPTION 'amount must be finite and non-negative';
    END IF;

    v_account_id := bursar.ensure_personal_account(p_user_id);
    SELECT balance INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_account_id
    FOR UPDATE;

    IF p_feature IS NOT NULL AND p_feature_max_calls IS NOT NULL THEN
        SELECT limited
        INTO v_feature_limited
        FROM bursar.check_feature_limit(
            p_user_id,
            p_feature,
            p_feature_max_calls,
            COALESCE(p_feature_period_start, current_date),
            COALESCE(p_feature_period_end, current_date)
        );
        IF v_feature_limited AND COALESCE(p_feature_action, 'deny') = 'deny' THEN
            RETURN QUERY SELECT NULL::uuid, p_user_id, 0::numeric, v_balance, 0::numeric, false,
                NULL::text, NULL::text, '{}'::jsonb, 'feature_limit_reached'::text;
            RETURN;
        ELSIF v_feature_limited THEN
            v_feature_warning := 'feature_limit_reached';
        END IF;
    END IF;

    IF NOT p_skip_allowance AND p_period_start IS NOT NULL THEN
        SELECT COALESCE(cp.included_amount, 0)
        INTO v_allowance
        FROM bursar.account_plan_assignments apa
        JOIN bursar.credit_plans cp ON cp.id = apa.plan_id
        WHERE apa.account_id = v_account_id;

        SELECT COALESCE(consumed, 0) INTO v_used
        FROM bursar.account_allowance_usage
        WHERE account_id = v_account_id AND period_start = p_period_start
        FOR UPDATE;

        v_free := LEAST(p_amount, GREATEST(v_allowance - COALESCE(v_used, 0), 0));
        IF v_free > 0 THEN
            INSERT INTO bursar.account_allowance_usage(account_id, period_start, consumed)
            VALUES (v_account_id, p_period_start, v_free)
            ON CONFLICT (account_id, period_start)
            DO UPDATE SET consumed = bursar.account_allowance_usage.consumed + EXCLUDED.consumed;
        END IF;
    END IF;

    v_billable := p_amount - v_free;
    IF v_billable = 0 THEN
        RETURN QUERY
        SELECT NULL::uuid, p_user_id, 0::numeric, v_balance, v_free, false,
               NULL::text, NULL::text, '{}'::jsonb, NULL::text;
        RETURN;
    END IF;

    v_metadata := COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_build_object('model', p_model, 'requested_amount', p_amount);

    SELECT * INTO v_result
    FROM bursar.post_ledger_entry(
        v_account_id,
        p_user_id,
        -v_billable,
        'usage',
        NULL,
        NULL,
        p_idempotency_key,
        v_metadata,
        NULL,
        'default',
        p_min_balance
    );

        RETURN QUERY
        SELECT v_result.entry_id, p_user_id, v_billable, v_result.balance_after,
               v_free, v_result.idempotent, NULL::text, v_feature_warning,
           COALESCE((
               SELECT jsonb_object_agg(l.bucket, x.amount)
               FROM (
                   SELECT a.lot_id, SUM(a.amount) AS amount
                   FROM bursar.credit_lot_allocations a
                   WHERE a.debit_entry_id = v_result.entry_id
                   GROUP BY a.lot_id
               ) x
               JOIN bursar.credit_lots l ON l.id = x.lot_id
           ), '{}'::jsonb),
           NULL::text;
EXCEPTION
    WHEN OTHERS THEN
        RETURN QUERY
        SELECT NULL::uuid, p_user_id, 0::numeric, v_balance, 0::numeric, false,
               NULL::text, NULL::text, '{}'::jsonb,
               CASE WHEN SQLERRM LIKE '%insufficient_credits%' THEN 'insufficient_credits'
                    ELSE SQLERRM END;
END;
$$;

CREATE FUNCTION bursar.refund_credits(
    p_entry_id uuid,
    p_amount numeric DEFAULT NULL,
    p_reason text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
    refund_entry_id uuid,
    original_entry_id uuid,
    user_id uuid,
    amount numeric,
    new_balance numeric,
    bucket_breakdown jsonb,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_original record;
    v_refunded numeric(18,4);
    v_amount numeric(18,4);
    v_result record;
    v_allocation record;
    v_restored numeric(18,4);
    v_available numeric(18,4);
BEGIN
    SELECT e.*, a.user_id
    INTO v_original
    FROM bursar.credit_ledger_entries e
    JOIN bursar.credit_accounts a ON a.id = e.account_id
    WHERE e.id = p_entry_id
      AND e.amount < 0
    FOR UPDATE OF e;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT NULL::uuid, p_entry_id, NULL::uuid, 0::numeric, 0::numeric,
               '{}'::jsonb, 'not_found'::text;
        RETURN;
    END IF;

    SELECT COALESCE(SUM(e.amount), 0)
    INTO v_refunded
    FROM bursar.credit_ledger_entries e
    WHERE e.reference_entry_id = p_entry_id
      AND e.entry_type IN ('refund', 'reversal')
      AND e.amount > 0;

    v_amount := COALESCE(p_amount, -v_original.amount - v_refunded);
    IF v_amount <= 0 OR v_refunded + v_amount > -v_original.amount THEN
        RETURN QUERY
        SELECT NULL::uuid, p_entry_id, v_original.user_id, 0::numeric, 0::numeric,
               '{}'::jsonb, 'over_refund'::text;
        RETURN;
    END IF;

    SELECT * INTO v_result
    FROM bursar.post_ledger_entry(
        v_original.account_id,
        v_original.user_id,
        v_amount,
        'refund',
        p_entry_id,
        NULL,
        NULL,
        COALESCE(p_metadata, '{}'::jsonb) || jsonb_build_object('reason', p_reason),
        NULL,
        'default',
        NULL
    );

    v_restored := 0;
    FOR v_allocation IN
        SELECT a.id, a.lot_id, a.amount,
               COALESCE(SUM(r.amount), 0) AS reversed
        FROM bursar.credit_lot_allocations a
        LEFT JOIN bursar.credit_lot_reversals r
          ON r.original_allocation_id = a.id
        WHERE a.debit_entry_id = p_entry_id
        GROUP BY a.id, a.lot_id, a.amount
        ORDER BY a.created_at DESC, a.id DESC
    LOOP
        EXIT WHEN v_restored >= v_amount;
        v_available := v_allocation.amount - v_allocation.reversed;
        IF v_available > 0 THEN
            v_available := LEAST(v_available, v_amount - v_restored);
            UPDATE bursar.credit_lots
            SET consumed = consumed - v_available
            WHERE id = v_allocation.lot_id;
            INSERT INTO bursar.credit_lot_reversals(
                refund_entry_id,
                original_allocation_id,
                amount
            )
            VALUES (v_result.entry_id, v_allocation.id, v_available);
            v_restored := v_restored + v_available;
        END IF;
    END LOOP;

    RETURN QUERY
    SELECT v_result.entry_id, p_entry_id, v_original.user_id, v_amount,
           v_result.balance_after,
           COALESCE((
               SELECT jsonb_object_agg(l.bucket, x.amount)
               FROM (
                   SELECT lr.original_allocation_id, SUM(lr.amount) AS amount
                   FROM bursar.credit_lot_reversals lr
                   WHERE lr.refund_entry_id = v_result.entry_id
                   GROUP BY lr.original_allocation_id
               ) x
               JOIN bursar.credit_lot_allocations la ON la.id = x.original_allocation_id
               JOIN bursar.credit_lots l ON l.id = la.lot_id
           ), '{}'::jsonb),
           NULL::text;
END;
$$;

CREATE FUNCTION bursar.create_lease(
    p_user_id uuid,
    p_amount numeric,
    p_operation_type text,
    p_billing_mode text DEFAULT 'strict',
    p_floor numeric DEFAULT 0,
    p_max_concurrent integer DEFAULT NULL,
    p_ttl_seconds integer DEFAULT 600,
    p_model text DEFAULT NULL,
    p_overdraft_floor numeric DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_period_start date DEFAULT NULL,
    p_feature text DEFAULT NULL,
    p_feature_max_calls integer DEFAULT NULL,
    p_feature_action text DEFAULT NULL,
    p_feature_period_start date DEFAULT NULL,
    p_feature_period_end date DEFAULT NULL
)
RETURNS TABLE(
    lease_id uuid,
    user_id uuid,
    amount numeric,
    available numeric,
    reserved numeric,
    billing_mode text,
    expires_at timestamptz,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account_id uuid;
    v_balance numeric(18,4);
    v_reserved numeric(18,4);
    v_floor numeric(18,4);
    v_id uuid;
    v_expires timestamptz;
BEGIN
    v_account_id := bursar.ensure_personal_account(p_user_id);
    SELECT balance INTO v_balance
    FROM bursar.credit_accounts
    WHERE id = v_account_id
    FOR UPDATE;

    UPDATE bursar.credit_leases AS cl
    SET status = 'expired', updated_at = now()
    WHERE cl.account_id = v_account_id
      AND cl.status = 'active'
      AND cl.expires_at <= now();

    IF p_max_concurrent IS NOT NULL
       AND (SELECT COUNT(*) FROM bursar.credit_leases
            WHERE account_id = v_account_id AND status = 'active') >= p_max_concurrent THEN
        RETURN QUERY SELECT NULL::uuid, p_user_id, p_amount, 0::numeric, 0::numeric,
                            p_billing_mode, NULL::timestamptz, 'concurrency_limit'::text;
        RETURN;
    END IF;

    SELECT COALESCE(SUM(cl.amount), 0) INTO v_reserved
    FROM bursar.credit_leases AS cl
    WHERE cl.account_id = v_account_id AND cl.status = 'active';

    v_floor := COALESCE(
        CASE WHEN p_billing_mode = 'overdraft' THEN p_overdraft_floor END,
        p_floor,
        0
    );
    IF v_balance - v_reserved - p_amount < v_floor THEN
        RETURN QUERY SELECT NULL::uuid, p_user_id, p_amount,
                            v_balance - v_reserved, v_reserved,
                            p_billing_mode, NULL::timestamptz, 'insufficient_credits'::text;
        RETURN;
    END IF;

    v_expires := now() + make_interval(secs => p_ttl_seconds);
    INSERT INTO bursar.credit_leases(
        account_id, actor_user_id, amount, operation_type,
        expires_at, overdraft_floor
    )
    VALUES (
        v_account_id, p_user_id, p_amount, p_operation_type,
        v_expires, CASE WHEN p_billing_mode = 'overdraft' THEN v_floor END
    )
    RETURNING id INTO v_id;

    RETURN QUERY SELECT v_id, p_user_id, p_amount,
                        v_balance - v_reserved - p_amount,
                        v_reserved + p_amount,
                        p_billing_mode, v_expires, NULL::text;
END;
$$;

CREATE FUNCTION bursar.settle_lease(
    p_user_id uuid,
    p_lease_id uuid,
    p_amount numeric,
    p_idempotency_key text DEFAULT NULL,
    p_min_balance numeric DEFAULT 0,
    p_model text DEFAULT NULL,
    p_metadata jsonb DEFAULT '{}'::jsonb,
    p_skip_allowance boolean DEFAULT false,
    p_period_start date DEFAULT NULL,
    p_feature text DEFAULT NULL,
    p_feature_max_calls integer DEFAULT NULL,
    p_feature_action text DEFAULT NULL,
    p_feature_period_start date DEFAULT NULL,
    p_feature_period_end date DEFAULT NULL
)
RETURNS TABLE(
    entry_id uuid,
    user_id uuid,
    amount numeric,
    balance_after numeric,
    allowance_consumed numeric,
    idempotent boolean,
    cap_warning text,
    feature_limit_warning text,
    bucket_breakdown jsonb,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_lease record;
BEGIN
    SELECT l.* INTO v_lease
    FROM bursar.credit_leases l
    JOIN bursar.credit_accounts a ON a.id = l.account_id
    WHERE l.id = p_lease_id
      AND a.user_id = p_user_id
    FOR UPDATE OF l;

    IF NOT FOUND OR v_lease.status <> 'active' OR v_lease.expires_at <= now() THEN
        RETURN QUERY
        SELECT NULL::uuid, p_user_id, 0::numeric, 0::numeric, 0::numeric, false,
               NULL::text, NULL::text, '{}'::jsonb, 'lease_not_found'::text;
        RETURN;
    END IF;

    UPDATE bursar.credit_leases
    SET status = 'settled', updated_at = now()
    WHERE id = p_lease_id;

    RETURN QUERY
    SELECT * FROM bursar.deduct_with_allowance(
        p_user_id, p_amount, p_idempotency_key,
        COALESCE(v_lease.overdraft_floor, p_min_balance),
        p_model, p_metadata || jsonb_build_object('lease_id', p_lease_id),
        p_skip_allowance, p_period_start, p_feature,
        p_feature_max_calls, p_feature_action,
        p_feature_period_start, p_feature_period_end
    );
END;
$$;

CREATE FUNCTION bursar.release_lease(p_user_id uuid, p_lease_id uuid)
RETURNS TABLE(lease_id uuid, user_id uuid, released boolean, reason text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    UPDATE bursar.credit_leases l
    SET status = 'released', updated_at = now()
    FROM bursar.credit_accounts a
    WHERE l.id = p_lease_id
      AND l.account_id = a.id
      AND a.user_id = p_user_id
      AND l.status = 'active';

    IF FOUND THEN
        RETURN QUERY SELECT p_lease_id, p_user_id, true, 'released'::text;
    ELSE
        RETURN QUERY SELECT p_lease_id, p_user_id, false, 'lease_not_found'::text;
    END IF;
END;
$$;

CREATE FUNCTION bursar.renew_lease(
    p_user_id uuid,
    p_lease_id uuid,
    p_ttl_seconds integer
)
RETURNS TABLE(
    lease_id uuid,
    user_id uuid,
    amount numeric,
    available numeric,
    reserved numeric,
    billing_mode text,
    expires_at timestamptz,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_lease record;
    v_balance numeric;
    v_reserved numeric;
BEGIN
    UPDATE bursar.credit_leases l
    SET expires_at = now() + make_interval(secs => p_ttl_seconds),
        updated_at = now()
    FROM bursar.credit_accounts a
    WHERE l.id = p_lease_id
      AND l.account_id = a.id
      AND a.user_id = p_user_id
      AND l.status = 'active'
      AND l.expires_at > now()
    RETURNING l.* INTO v_lease;

    IF NOT FOUND THEN
        RETURN QUERY
        SELECT p_lease_id, p_user_id, 0::numeric, 0::numeric, 0::numeric,
               'strict'::text, NULL::timestamptz, 'lease_not_found'::text;
        RETURN;
    END IF;

    SELECT a.balance, COALESCE(SUM(l.amount), 0)
    INTO v_balance, v_reserved
    FROM bursar.credit_accounts a
    LEFT JOIN bursar.credit_leases l
      ON l.account_id = a.id AND l.status = 'active' AND l.expires_at > now()
    WHERE a.id = v_lease.account_id
    GROUP BY a.id, a.balance;

    RETURN QUERY
    SELECT p_lease_id, p_user_id, v_lease.amount,
           v_balance - v_reserved, v_reserved,
           CASE WHEN v_lease.overdraft_floor IS NULL THEN 'strict' ELSE 'overdraft' END,
           v_lease.expires_at, NULL::text;
END;
$$;

CREATE FUNCTION bursar.get_ledger_entry(p_user_id uuid, p_entry_id uuid)
RETURNS TABLE(
    entry_id uuid,
    account_id uuid,
    actor_user_id uuid,
    amount numeric,
    entry_type text,
    reference_entry_id uuid,
    idempotency_key text,
    metadata jsonb,
    created_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT e.id, e.account_id, e.actor_user_id, e.amount, e.entry_type,
           e.reference_entry_id, e.idempotency_key, e.metadata, e.created_at
    FROM bursar.credit_ledger_entries e
    JOIN bursar.credit_accounts a ON a.id = e.account_id
    WHERE e.id = p_entry_id
      AND a.user_id = p_user_id
$$;

CREATE FUNCTION bursar.list_ledger_entries(
    p_user_id uuid,
    p_entry_types text[] DEFAULT NULL,
    p_from_date timestamptz DEFAULT NULL,
    p_to_date timestamptz DEFAULT NULL,
    p_limit integer DEFAULT 50,
    p_cursor_created_at timestamptz DEFAULT NULL,
    p_cursor_entry_id uuid DEFAULT NULL
)
RETURNS TABLE(
    entry_id uuid,
    account_id uuid,
    actor_user_id uuid,
    amount numeric,
    entry_type text,
    reference_entry_id uuid,
    idempotency_key text,
    metadata jsonb,
    created_at timestamptz,
    next_cursor_created_at timestamptz,
    next_cursor_entry_id uuid
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT e.id, e.account_id, e.actor_user_id, e.amount, e.entry_type,
           e.reference_entry_id, e.idempotency_key, e.metadata, e.created_at,
           e.created_at, e.id
    FROM bursar.credit_ledger_entries e
    JOIN bursar.credit_accounts a ON a.id = e.account_id
    WHERE a.user_id = p_user_id
      AND (p_entry_types IS NULL OR e.entry_type = ANY(p_entry_types))
      AND (p_from_date IS NULL OR e.created_at >= p_from_date)
      AND (p_to_date IS NULL OR e.created_at <= p_to_date)
      AND (
          p_cursor_created_at IS NULL
          OR (e.created_at, e.id) < (p_cursor_created_at, p_cursor_entry_id)
      )
    ORDER BY e.created_at DESC, e.id DESC
    LIMIT LEAST(GREATEST(p_limit, 1), 201)
$$;

CREATE FUNCTION bursar.list_usage_entries(
    p_user_id uuid,
    p_entry_types text[] DEFAULT NULL,
    p_from_date timestamptz DEFAULT NULL,
    p_to_date timestamptz DEFAULT NULL,
    p_limit integer DEFAULT 50,
    p_cursor_created_at timestamptz DEFAULT NULL,
    p_cursor_entry_id uuid DEFAULT NULL
)
RETURNS TABLE(
    entry_id uuid,
    account_id uuid,
    actor_user_id uuid,
    amount numeric,
    entry_type text,
    reference_entry_id uuid,
    idempotency_key text,
    metadata jsonb,
    created_at timestamptz,
    next_cursor_created_at timestamptz,
    next_cursor_entry_id uuid
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT * FROM bursar.list_ledger_entries(
        p_user_id, ARRAY['usage']::text[], p_from_date, p_to_date,
        p_limit, p_cursor_created_at, p_cursor_entry_id
    )
$$;

CREATE FUNCTION bursar.get_credit_bucket_balances(p_user_id uuid)
RETURNS TABLE(user_id uuid, buckets jsonb, total_balance numeric)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH account AS (
        SELECT id, balance FROM bursar.credit_accounts WHERE user_id = p_user_id
    ),
    bucket_totals AS (
        SELECT l.bucket AS bucket_key,
               SUM(l.granted - l.consumed) AS balance
        FROM bursar.credit_lots l
        JOIN account a ON a.id = l.account_id
        WHERE l.expires_at IS NULL OR l.expires_at > now()
        GROUP BY l.bucket
    )
    SELECT p_user_id,
           COALESCE(jsonb_agg(jsonb_build_object(
               'bucket_key', COALESCE(b.bucket_key, t.bucket_key),
               'name', COALESCE(b.label, t.bucket_key),
               'priority', COALESCE(b.priority, 2147483647),
               'expires', COALESCE(b.expires, false),
               'balance', COALESCE(t.balance, 0)
           ) ORDER BY COALESCE(b.priority, 2147483647), COALESCE(b.bucket_key, t.bucket_key))
           FILTER (WHERE COALESCE(b.bucket_key, t.bucket_key) IS NOT NULL), '[]'::jsonb),
           a.balance
    FROM account a
    LEFT JOIN bucket_totals t ON true
    FULL JOIN bursar.credit_buckets b ON b.bucket_key = t.bucket_key AND b.status = 'active'
    GROUP BY a.id, a.balance
$$;

CREATE FUNCTION bursar.expire_credits(
    p_dry_run boolean DEFAULT false,
    p_user_id uuid DEFAULT NULL
)
RETURNS TABLE(expired_count integer, expired_amount numeric, dry_run boolean, expired_by_bucket jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_lot record;
    v_amount numeric;
    v_result record;
    v_count integer := 0;
    v_total numeric := 0;
    v_by_bucket jsonb := '{}'::jsonb;
BEGIN
    FOR v_lot IN
        SELECT l.*, a.user_id
        FROM bursar.credit_lots l
        JOIN bursar.credit_accounts a ON a.id = l.account_id
        WHERE l.expires_at IS NOT NULL
          AND l.expires_at <= now()
          AND l.consumed < l.granted
          AND (p_user_id IS NULL OR a.user_id = p_user_id)
        ORDER BY l.expires_at, l.id
        FOR UPDATE OF l
    LOOP
        v_amount := v_lot.granted - v_lot.consumed;
        v_count := v_count + 1;
        v_total := v_total + v_amount;
        v_by_bucket := jsonb_set(
            v_by_bucket,
            ARRAY[v_lot.bucket],
            to_jsonb(COALESCE((v_by_bucket ->> v_lot.bucket)::numeric, 0) + v_amount),
            true
        );
        IF NOT p_dry_run THEN
            SELECT * INTO v_result
            FROM bursar.post_ledger_entry(
                v_lot.account_id, v_lot.user_id, -v_amount, 'expiry',
                v_lot.source_entry_id, NULL,
                'expiry:' || v_lot.id::text, jsonb_build_object('lot_id', v_lot.id),
                NULL, v_lot.bucket, NULL
            );
        END IF;
    END LOOP;

    RETURN QUERY SELECT v_count, v_total, p_dry_run, v_by_bucket;
END;
$$;

CREATE FUNCTION bursar.sync_catalog_from_config(p_config jsonb, p_version integer)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_key text;
    v_value jsonb;
    v_provider text;
    v_provider_value jsonb;
    v_lookup_type text;
    v_lookup_value text;
BEGIN
    IF jsonb_typeof(p_config) <> 'object' THEN
        RAISE EXCEPTION 'Bursar config must be an object';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_object_keys(p_config) key
        WHERE key NOT IN ('version', 'usage', 'credits', 'plans', 'payments')
    ) THEN
        RAISE EXCEPTION 'unknown top-level Bursar config field';
    END IF;
    IF (p_config ->> 'version')::integer <> 1 THEN
        RAISE EXCEPTION 'unsupported Bursar config version';
    END IF;

    UPDATE bursar.credit_buckets SET status = 'retired' WHERE status = 'active';
    FOR v_key, v_value IN
        SELECT key, value
        FROM jsonb_each(COALESCE(p_config #> '{credits,buckets}', '{}'::jsonb))
    LOOP
        INSERT INTO bursar.credit_buckets(
            bucket_key, label, priority, expires, ttl_days,
            allow_overdraft, is_default, status, config_version
        )
        VALUES (
            v_key,
            COALESCE(v_value ->> 'label', initcap(replace(v_key, '_', ' '))),
            COALESCE((
                SELECT ordinality::integer
                FROM jsonb_array_elements_text(
                    COALESCE(p_config #> '{credits,spend_order}', '[]'::jsonb)
                ) WITH ORDINALITY AS x(bucket, ordinality)
                WHERE bucket = v_key
            ), 2147483647),
            v_value ? 'expires_after',
            CASE
                WHEN v_value #>> '{expires_after,unit}' = 'day'
                THEN COALESCE((v_value #>> '{expires_after,count}')::integer, 1)
                ELSE NULL
            END,
            COALESCE((p_config #>> '{credits,overdraft_bucket}') = v_key, false),
            COALESCE(p_config #>> '{credits,default_bucket}', '') = v_key,
            'active',
            p_version
        )
        ON CONFLICT (bucket_key) DO UPDATE
        SET label = EXCLUDED.label,
            priority = EXCLUDED.priority,
            expires = EXCLUDED.expires,
            ttl_days = EXCLUDED.ttl_days,
            allow_overdraft = EXCLUDED.allow_overdraft,
            is_default = EXCLUDED.is_default,
            status = 'active',
            config_version = EXCLUDED.config_version,
            updated_at = now();
    END LOOP;

    UPDATE bursar.credit_plans SET status = 'retired' WHERE status = 'active';
    FOR v_key, v_value IN
        SELECT key, value FROM jsonb_each(COALESCE(p_config -> 'plans', '{}'::jsonb))
    LOOP
        INSERT INTO bursar.credit_plans(
            plan_key, display_name, rate_card, included_amount, included_reset,
            features, limits, spending, config_version, status
        )
        VALUES (
            v_key,
            v_value ->> 'display_name',
            v_value ->> 'rate_card',
            COALESCE((v_value #>> '{included_credits,amount}')::numeric, 0),
            v_value #> '{included_credits,reset}',
            COALESCE(v_value -> 'features', '{}'::jsonb),
            COALESCE(v_value -> 'limits', '{}'::jsonb),
            COALESCE(v_value -> 'spending', '{}'::jsonb),
            p_version,
            'active'
        )
        ON CONFLICT (plan_key, config_version) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            rate_card = EXCLUDED.rate_card,
            included_amount = EXCLUDED.included_amount,
            included_reset = EXCLUDED.included_reset,
            features = EXCLUDED.features,
            limits = EXCLUDED.limits,
            spending = EXCLUDED.spending,
            status = 'active',
            valid_to = NULL;
    END LOOP;

    UPDATE bursar.billing_offers SET status = 'archived' WHERE status = 'active';
    UPDATE bursar.billing_credit_topups SET status = 'archived' WHERE status = 'active';
    DELETE FROM bursar.billing_provider_refs;

    FOR v_key, v_value IN
        SELECT key, value
        FROM jsonb_each(COALESCE(p_config #> '{payments,subscriptions}', '{}'::jsonb))
    LOOP
        INSERT INTO bursar.billing_offers(
            offer_key, plan, interval, interval_count, grant_mode,
            grant_credits, grant_bucket, grant_replace_prior, status
        )
        VALUES (
            v_key,
            v_value ->> 'plan',
            COALESCE(v_value #>> '{billing_period,unit}', 'month'),
            COALESCE((v_value #>> '{billing_period,count}')::integer, 1),
            CASE WHEN v_value ? 'renewal_credits' THEN 'cycle_grant' ELSE 'allowance' END,
            (v_value #>> '{renewal_credits,amount}')::numeric,
            v_value #>> '{renewal_credits,bucket}',
            COALESCE(v_value #>> '{renewal_credits,behavior}', 'replace') = 'replace',
            'active'
        )
        ON CONFLICT (offer_key) DO UPDATE
        SET plan = EXCLUDED.plan,
            interval = EXCLUDED.interval,
            interval_count = EXCLUDED.interval_count,
            grant_mode = EXCLUDED.grant_mode,
            grant_credits = EXCLUDED.grant_credits,
            grant_bucket = EXCLUDED.grant_bucket,
            grant_replace_prior = EXCLUDED.grant_replace_prior,
            status = 'active',
            updated_at = now();

        FOR v_provider, v_provider_value IN
            SELECT key, value FROM jsonb_each(COALESCE(v_value -> 'providers', '{}'::jsonb))
        LOOP
            v_lookup_type := v_provider_value #>> '{lookup,type}';
            v_lookup_value := v_provider_value #>> '{lookup,value}';
            INSERT INTO bursar.billing_provider_refs(
                provider, environment, product_id, price_id, variant_id, lookup_key,
                resource_type, resource_key, active
            )
            VALUES (
                v_provider,
                bursar.current_provider_environment(),
                CASE WHEN v_lookup_type = 'product_id' THEN v_lookup_value END,
                CASE WHEN v_lookup_type = 'price_id' THEN v_lookup_value END,
                CASE WHEN v_lookup_type = 'variant_id' THEN v_lookup_value END,
                CASE WHEN v_lookup_type = 'lookup_key' THEN v_lookup_value END,
                'offer', v_key, true
            );
        END LOOP;
    END LOOP;

    FOR v_key, v_value IN
        SELECT key, value
        FROM jsonb_each(COALESCE(p_config #> '{payments,topups}', '{}'::jsonb))
    LOOP
        INSERT INTO bursar.billing_credit_topups(
            topup_key, deposit_to, credits_per_unit,
            min_amount_minor, max_amount_minor, tax_behavior, status
        )
        VALUES (
            v_key,
            v_value ->> 'bucket',
            (v_value ->> 'credits')::numeric,
            COALESCE((v_value ->> 'min_amount_minor')::integer, 500),
            COALESCE((v_value ->> 'max_amount_minor')::integer, 500000),
            COALESCE(v_value ->> 'tax_behavior', 'exclude_tax'),
            'active'
        )
        ON CONFLICT (topup_key) DO UPDATE
        SET deposit_to = EXCLUDED.deposit_to,
            credits_per_unit = EXCLUDED.credits_per_unit,
            status = 'active',
            updated_at = now();

        FOR v_provider, v_provider_value IN
            SELECT key, value FROM jsonb_each(COALESCE(v_value -> 'providers', '{}'::jsonb))
        LOOP
            v_lookup_type := v_provider_value #>> '{lookup,type}';
            v_lookup_value := v_provider_value #>> '{lookup,value}';
            INSERT INTO bursar.billing_provider_refs(
                provider, environment, product_id, price_id, variant_id, lookup_key,
                resource_type, resource_key, active
            )
            VALUES (
                v_provider,
                bursar.current_provider_environment(),
                CASE WHEN v_lookup_type = 'product_id' THEN v_lookup_value END,
                CASE WHEN v_lookup_type = 'price_id' THEN v_lookup_value END,
                CASE WHEN v_lookup_type = 'variant_id' THEN v_lookup_value END,
                CASE WHEN v_lookup_type = 'lookup_key' THEN v_lookup_value END,
                'topup', v_key, true
            );
        END LOOP;
    END LOOP;
END;
$$;

CREATE FUNCTION bursar.publish_bursar_config(
    p_config jsonb,
    p_label text DEFAULT NULL
)
RETURNS TABLE(
    id uuid,
    config jsonb,
    version integer,
    label text,
    active boolean,
    created_at timestamptz,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_row bursar.bursar_config%ROWTYPE;
BEGIN
    IF jsonb_typeof(p_config) <> 'object' THEN
        RAISE EXCEPTION 'Bursar config must be an object';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM jsonb_object_keys(p_config) key
        WHERE key NOT IN ('version', 'usage', 'credits', 'plans', 'payments')
    ) THEN
        RAISE EXCEPTION 'unknown top-level Bursar config field';
    END IF;
    IF (p_config ->> 'version')::integer <> 1 THEN
        RAISE EXCEPTION 'unsupported Bursar config version';
    END IF;
    INSERT INTO bursar.bursar_config(config, version, label, active)
    VALUES (
        p_config,
        COALESCE((SELECT MAX(bc.version) + 1 FROM bursar.bursar_config bc), 1),
        p_label,
        false
    )
    RETURNING * INTO v_row;
    RETURN QUERY
    SELECT v_row.id, v_row.config, v_row.version, v_row.label,
           v_row.active, v_row.created_at, NULL::text;
END;
$$;

CREATE FUNCTION bursar.activate_bursar_config(p_version integer)
RETURNS TABLE(
    id uuid,
    config jsonb,
    version integer,
    label text,
    active boolean,
    created_at timestamptz,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_row bursar.bursar_config%ROWTYPE;
BEGIN
    SELECT * INTO v_row
    FROM bursar.bursar_config
    WHERE bursar_config.version = p_version
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY
        SELECT NULL::uuid, NULL::jsonb, p_version, NULL::text, false,
               NULL::timestamptz, 'not_found'::text;
        RETURN;
    END IF;

    PERFORM bursar.sync_catalog_from_config(v_row.config, v_row.version);
    UPDATE bursar.bursar_config AS bc SET active = false WHERE bc.active;
    UPDATE bursar.bursar_config AS bc SET active = true WHERE bc.version = p_version;
    v_row.active := true;
    RETURN QUERY
    SELECT v_row.id, v_row.config, v_row.version, v_row.label,
           true, v_row.created_at, NULL::text;
END;
$$;

CREATE FUNCTION bursar.set_active_bursar_config(
    p_config jsonb,
    p_label text DEFAULT NULL
)
RETURNS TABLE(
    id uuid,
    config jsonb,
    version integer,
    label text,
    active boolean,
    created_at timestamptz,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_published record;
BEGIN
    SELECT * INTO v_published FROM bursar.publish_bursar_config(p_config, p_label);
    RETURN QUERY SELECT * FROM bursar.activate_bursar_config(v_published.version);
END;
$$;

CREATE FUNCTION bursar.get_active_bursar_config()
RETURNS TABLE(id uuid, config jsonb, version integer, label text, active boolean, created_at timestamptz)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT bc.id, bc.config, bc.version, bc.label, bc.active, bc.created_at
    FROM bursar.bursar_config bc
    WHERE bc.active
    LIMIT 1
$$;

CREATE FUNCTION bursar.get_bursar_config(p_version integer)
RETURNS TABLE(id uuid, config jsonb, version integer, label text, active boolean, created_at timestamptz)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT bc.id, bc.config, bc.version, bc.label, bc.active, bc.created_at
    FROM bursar.bursar_config bc
    WHERE bc.version = p_version
$$;

CREATE FUNCTION bursar.get_bursar_configs(p_limit integer DEFAULT 50)
RETURNS TABLE(id uuid, version integer, label text, active boolean, created_at timestamptz)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT bc.id, bc.version, bc.label, bc.active, bc.created_at
    FROM bursar.bursar_config bc
    ORDER BY bc.version DESC
    LIMIT LEAST(GREATEST(p_limit, 1), 200)
$$;

CREATE FUNCTION bursar.set_user_plan(
    p_user_id uuid,
    p_plan_key text,
    p_assigned_at timestamptz DEFAULT NULL,
    p_config_version integer DEFAULT NULL,
    p_force boolean DEFAULT false
)
RETURNS TABLE(user_id uuid, plan_id uuid, plan_assigned_at timestamptz, error text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account_id uuid;
    v_plan_id uuid;
BEGIN
    v_account_id := bursar.ensure_personal_account(p_user_id);
    SELECT cp.id INTO v_plan_id
    FROM bursar.credit_plans cp
    WHERE cp.plan_key = p_plan_key
      AND cp.status = 'active'
      AND (p_config_version IS NULL OR cp.config_version = p_config_version)
    ORDER BY cp.config_version DESC
    LIMIT 1;

    IF v_plan_id IS NULL THEN
        RETURN QUERY SELECT p_user_id, NULL::uuid, NULL::timestamptz, 'plan_not_found'::text;
        RETURN;
    END IF;

    INSERT INTO bursar.account_plan_assignments(
        account_id, plan_id, plan_key, assigned_at
    )
    VALUES (v_account_id, v_plan_id, p_plan_key, COALESCE(p_assigned_at, now()))
    ON CONFLICT (account_id) DO UPDATE
    SET plan_id = EXCLUDED.plan_id,
        plan_key = EXCLUDED.plan_key,
        assigned_at = EXCLUDED.assigned_at,
        updated_at = now();

    RETURN QUERY
    SELECT p_user_id, v_plan_id, COALESCE(p_assigned_at, now()), NULL::text;
END;
$$;

CREATE FUNCTION bursar.unset_user_plan(p_user_id uuid)
RETURNS TABLE(user_id uuid, removed boolean)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH deleted AS (
        DELETE FROM bursar.account_plan_assignments apa
        USING bursar.credit_accounts a
        WHERE apa.account_id = a.id AND a.user_id = p_user_id
        RETURNING 1
    )
    SELECT p_user_id, EXISTS(SELECT 1 FROM deleted)
$$;

CREATE FUNCTION bursar.get_user_plan(p_user_id uuid)
RETURNS TABLE(
    user_id uuid,
    plan_id uuid,
    plan_key text,
    plan_label text,
    allowance_amount numeric,
    allowance_period text,
    entitlements jsonb,
    rate_overrides jsonb,
    billing_mode text,
    per_operation jsonb,
    max_concurrent integer,
    overdraft_floor numeric,
    plan_assigned_at timestamptz,
    config_version integer,
    catalog_version integer
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT p_user_id,
           cp.id,
           cp.plan_key,
           cp.display_name,
           cp.included_amount,
           CASE
               WHEN cp.included_reset ->> 'unit' = 'month'
                    AND COALESCE(cp.included_reset ->> 'anchor', 'calendar') = 'calendar'
               THEN 'calendar_month'
               ELSE 'rolling_30d'
           END,
    cp.features || cp.limits,
           NULL::jsonb,
           COALESCE(cp.spending ->> 'mode', 'strict'),
           cp.spending -> 'operations',
           (cp.spending ->> 'max_concurrent')::integer,
           -((cp.spending ->> 'overdraft_limit')::numeric),
           apa.assigned_at,
           cp.config_version,
           cp.config_version
    FROM bursar.credit_accounts a
    JOIN bursar.account_plan_assignments apa ON apa.account_id = a.id
    JOIN bursar.credit_plans cp ON cp.id = apa.plan_id
    WHERE a.user_id = p_user_id
$$;

CREATE FUNCTION bursar.check_plan_allowance(p_user_id uuid, p_period_start date)
RETURNS TABLE(
    plan_id uuid,
    allowance_amount numeric,
    allowance_used numeric,
    allowance_remaining numeric,
    period_start date
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT cp.id,
           cp.included_amount,
           COALESCE(au.consumed, 0),
           GREATEST(cp.included_amount - COALESCE(au.consumed, 0), 0),
           p_period_start
    FROM bursar.credit_accounts a
    JOIN bursar.account_plan_assignments apa ON apa.account_id = a.id
    JOIN bursar.credit_plans cp ON cp.id = apa.plan_id
    LEFT JOIN bursar.account_allowance_usage au
      ON au.account_id = a.id AND au.period_start = p_period_start
    WHERE a.user_id = p_user_id
$$;

CREATE FUNCTION bursar.migrate_plan_users(
    p_plan_key text,
    p_target_config_version integer DEFAULT NULL,
    p_batch_size integer DEFAULT NULL
)
RETURNS TABLE(
    plan_key text,
    target_plan_id uuid,
    target_config_version integer,
    migrated_count integer,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_target bursar.credit_plans%ROWTYPE;
    v_count integer;
BEGIN
    SELECT * INTO v_target
    FROM bursar.credit_plans cp
    WHERE cp.plan_key = p_plan_key
      AND (p_target_config_version IS NULL OR cp.config_version = p_target_config_version)
    ORDER BY cp.config_version DESC
    LIMIT 1;
    IF NOT FOUND THEN
        RETURN QUERY SELECT p_plan_key, NULL::uuid, 0, 0, 'plan_not_found'::text;
        RETURN;
    END IF;

    UPDATE bursar.account_plan_assignments
    SET plan_id = v_target.id, updated_at = now()
    WHERE account_plan_assignments.plan_key = p_plan_key
      AND plan_id <> v_target.id;
    GET DIAGNOSTICS v_count = ROW_COUNT;

    INSERT INTO bursar.credit_plan_migrations(
        plan_key, target_plan_id, target_config_version, migrated_count
    )
    VALUES (p_plan_key, v_target.id, v_target.config_version, v_count);

    RETURN QUERY
    SELECT p_plan_key, v_target.id, v_target.config_version, v_count, NULL::text;
END;
$$;

CREATE FUNCTION bursar.increment_usage_window(
    p_user_id uuid,
    p_feature text,
    p_period_start date,
    p_period_end date
)
RETURNS TABLE(count integer, period_start date, period_end date)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account_id uuid;
    v_count integer;
BEGIN
    v_account_id := bursar.ensure_personal_account(p_user_id);
    INSERT INTO bursar.credit_usage_window(account_id, feature, period_start, period_end, count)
    VALUES (v_account_id, p_feature, p_period_start, p_period_end, 1)
    ON CONFLICT ON CONSTRAINT credit_usage_window_pkey
    DO UPDATE SET count = bursar.credit_usage_window.count + 1,
                  period_end = EXCLUDED.period_end
    RETURNING credit_usage_window.count INTO v_count;
    RETURN QUERY SELECT v_count, p_period_start, p_period_end;
END;
$$;

CREATE FUNCTION bursar.check_feature_limit(
    p_user_id uuid,
    p_feature text,
    p_max_calls integer,
    p_period_start date,
    p_period_end date
)
RETURNS TABLE(
    limited boolean,
    limit_value integer,
    used integer,
    remaining integer,
    period_start date,
    period_end date
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT COALESCE(w.count, 0) >= p_max_calls,
           p_max_calls,
           COALESCE(w.count, 0),
           GREATEST(p_max_calls - COALESCE(w.count, 0), 0),
           p_period_start,
           p_period_end
    FROM bursar.credit_accounts a
    LEFT JOIN bursar.credit_usage_window w
      ON w.account_id = a.id
     AND w.feature = p_feature
     AND w.period_start = p_period_start
    WHERE a.user_id = p_user_id
$$;

CREATE FUNCTION bursar.check_spend_cap(
    p_user_id uuid,
    p_model text,
    p_amount numeric
)
RETURNS TABLE(
    capped boolean,
    current_spend numeric,
    cap_limit numeric,
    action text,
    model text
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    WITH account AS (
        SELECT id FROM bursar.credit_accounts WHERE user_id = p_user_id
    ),
    candidates AS (
        SELECT c.*,
               CASE
                   WHEN c.cap_type = 'daily' THEN date_trunc('day', now())
                   ELSE date_trunc('month', now())
               END AS starts_at
        FROM bursar.credit_spend_caps c
        JOIN account a ON a.id = c.account_id
        WHERE c.model IS NULL OR c.model = p_model
        ORDER BY c.model NULLS LAST
        LIMIT 1
    ),
    spending AS (
        SELECT COALESCE(-SUM(e.amount), 0) AS spent
        FROM bursar.credit_ledger_entries e
        JOIN account a ON a.id = e.account_id
        JOIN candidates c ON true
        WHERE e.amount < 0
          AND e.entry_type = 'usage'
          AND e.created_at >= c.starts_at
          AND (c.model IS NULL OR e.metadata ->> 'model' = c.model)
    )
    SELECT s.spent + p_amount > c.cap_limit,
           s.spent,
           c.cap_limit,
           c.action,
           c.model
    FROM candidates c
    CROSS JOIN spending s
$$;

CREATE FUNCTION bursar.revoke_credits_by_entry_type(p_user_id uuid, p_entry_type text)
RETURNS TABLE(
    revoked_count integer,
    revoked_amount numeric,
    new_balance numeric,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_lot record;
    v_result record;
    v_count integer := 0;
    v_amount numeric := 0;
    v_account_id uuid;
BEGIN
    SELECT id INTO v_account_id
    FROM bursar.credit_accounts
    WHERE user_id = p_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 0, 0::numeric, 0::numeric, NULL::text;
        RETURN;
    END IF;

    FOR v_lot IN
        SELECT l.*, e.entry_type
        FROM bursar.credit_lots l
        JOIN bursar.credit_ledger_entries e ON e.id = l.source_entry_id
        WHERE l.account_id = v_account_id
          AND e.entry_type = p_entry_type
          AND l.consumed < l.granted
        FOR UPDATE OF l
    LOOP
        SELECT * INTO v_result
        FROM bursar.post_ledger_entry(
            v_account_id, p_user_id, -(v_lot.granted - v_lot.consumed),
            'reversal', v_lot.source_entry_id, NULL,
            'revoke:' || v_lot.id::text,
            jsonb_build_object('revoked_entry_type', p_entry_type),
            NULL, v_lot.bucket, NULL
        );
        v_count := v_count + 1;
        v_amount := v_amount + (v_lot.granted - v_lot.consumed);
    END LOOP;

    RETURN QUERY
    SELECT v_count, v_amount,
           (SELECT balance FROM bursar.credit_accounts WHERE id = v_account_id),
           NULL::text;
END;
$$;

CREATE FUNCTION bursar.create_team(p_name text, p_initial_balance numeric DEFAULT 0)
RETURNS TABLE(team_id uuid, name text, error text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_team_id uuid;
    v_account_id uuid;
BEGIN
    INSERT INTO bursar.credit_teams(name)
    VALUES (p_name)
    RETURNING id INTO v_team_id;

    INSERT INTO bursar.credit_accounts(account_type, team_id)
    VALUES ('team', v_team_id)
    RETURNING id INTO v_account_id;

    IF p_initial_balance > 0 THEN
        PERFORM bursar.post_ledger_entry(
            v_account_id, NULL, p_initial_balance, 'adjustment',
            NULL, NULL, NULL, jsonb_build_object('team_created', true),
            NULL, 'default', NULL
        );
    END IF;

    RETURN QUERY SELECT v_team_id, p_name, NULL::text;
END;
$$;

CREATE FUNCTION bursar.get_team_balance(p_team_id uuid)
RETURNS TABLE(
    team_id uuid,
    name text,
    balance numeric,
    member_count integer,
    created_at timestamptz
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT t.id,
           t.name,
           a.balance,
           (SELECT COUNT(*)::integer
            FROM bursar.credit_team_members m
            WHERE m.team_id = t.id),
           t.created_at
    FROM bursar.credit_teams t
    JOIN bursar.credit_accounts a ON a.team_id = t.id
    WHERE t.id = p_team_id
$$;

CREATE FUNCTION bursar.add_team_member(
    p_team_id uuid,
    p_user_id uuid,
    p_role text DEFAULT 'member',
    p_spend_cap numeric DEFAULT NULL
)
RETURNS TABLE(team_id uuid, user_id uuid, role text, error text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    INSERT INTO bursar.credit_team_members(team_id, user_id, role, spend_cap)
    VALUES (p_team_id, p_user_id, p_role, p_spend_cap)
    ON CONFLICT (team_id, user_id) DO UPDATE
    SET role = EXCLUDED.role,
        spend_cap = EXCLUDED.spend_cap;
    RETURN QUERY SELECT p_team_id, p_user_id, p_role, NULL::text;
END;
$$;

CREATE FUNCTION bursar.get_team_members(p_team_id uuid)
RETURNS TABLE(
    user_id uuid,
    role text,
    spend_cap numeric,
    total_spent numeric
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT m.user_id,
           m.role,
           m.spend_cap,
           COALESCE((
               SELECT -SUM(e.amount)
               FROM bursar.credit_ledger_entries e
               JOIN bursar.credit_accounts a ON a.id = e.account_id
               WHERE a.team_id = p_team_id
                 AND e.actor_user_id = m.user_id
                 AND e.amount < 0
           ), 0)::numeric
    FROM bursar.credit_team_members m
    WHERE m.team_id = p_team_id
    ORDER BY m.created_at, m.user_id
$$;

CREATE FUNCTION bursar.deduct_team(
    p_team_id uuid,
    p_user_id uuid,
    p_amount numeric,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE(
    entry_id uuid,
    team_id uuid,
    user_id uuid,
    amount numeric,
    team_balance_after numeric,
    error text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_account_id uuid;
    v_member record;
    v_spent numeric;
    v_result record;
BEGIN
    SELECT m.* INTO v_member
    FROM bursar.credit_team_members m
    WHERE m.team_id = p_team_id AND m.user_id = p_user_id;
    IF NOT FOUND THEN
        RETURN QUERY
        SELECT NULL::uuid, p_team_id, p_user_id, 0::numeric, 0::numeric,
               'not_team_member'::text;
        RETURN;
    END IF;

    SELECT id INTO v_account_id
    FROM bursar.credit_accounts
    WHERE team_id = p_team_id
    FOR UPDATE;

    IF v_member.spend_cap IS NOT NULL THEN
        SELECT COALESCE(-SUM(amount), 0) INTO v_spent
        FROM bursar.credit_ledger_entries
        WHERE account_id = v_account_id
          AND actor_user_id = p_user_id
          AND amount < 0;
        IF v_spent + p_amount > v_member.spend_cap THEN
            RETURN QUERY
            SELECT NULL::uuid, p_team_id, p_user_id, 0::numeric,
                   (SELECT balance FROM bursar.credit_accounts WHERE id = v_account_id),
                   'member_spend_cap'::text;
            RETURN;
        END IF;
    END IF;

    SELECT * INTO v_result
    FROM bursar.post_ledger_entry(
        v_account_id, p_user_id, -p_amount, 'usage',
        NULL, NULL, NULL, p_metadata, NULL, 'default', 0
    );

    RETURN QUERY
    SELECT v_result.entry_id, p_team_id, p_user_id, p_amount,
           v_result.balance_after, NULL::text;
EXCEPTION
    WHEN OTHERS THEN
        RETURN QUERY
        SELECT NULL::uuid, p_team_id, p_user_id, 0::numeric, 0::numeric,
               CASE WHEN SQLERRM LIKE '%insufficient_credits%'
                    THEN 'insufficient_credits' ELSE SQLERRM END;
END;
$$;

CREATE FUNCTION bursar.spend_by_user(p_start timestamptz, p_end timestamptz)
RETURNS TABLE(user_id uuid, total_spend numeric, entry_count bigint)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT a.user_id, -SUM(e.amount), COUNT(*)
    FROM bursar.credit_ledger_entries e
    JOIN bursar.credit_accounts a ON a.id = e.account_id
    WHERE a.user_id IS NOT NULL
      AND e.amount < 0
      AND e.entry_type = 'usage'
      AND e.created_at BETWEEN p_start AND p_end
    GROUP BY a.user_id
$$;

CREATE FUNCTION bursar.spend_by_model(p_start timestamptz, p_end timestamptz)
RETURNS TABLE(model text, total_spend numeric, entry_count bigint)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT COALESCE(e.metadata ->> 'model', 'unknown'),
           -SUM(e.amount),
           COUNT(*)
    FROM bursar.credit_ledger_entries e
    WHERE e.amount < 0
      AND e.entry_type = 'usage'
      AND e.created_at BETWEEN p_start AND p_end
    GROUP BY COALESCE(e.metadata ->> 'model', 'unknown')
$$;

CREATE FUNCTION bursar.top_users(p_limit integer, p_start timestamptz, p_end timestamptz)
RETURNS TABLE(user_id uuid, total_spend numeric)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT s.user_id, s.total_spend
    FROM bursar.spend_by_user(p_start, p_end) s
    ORDER BY s.total_spend DESC, s.user_id
    LIMIT LEAST(GREATEST(p_limit, 1), 200)
$$;

CREATE FUNCTION bursar.daily_spend(p_start timestamptz, p_end timestamptz)
RETURNS TABLE(date date, total_spend numeric, entry_count bigint)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT e.created_at::date, -SUM(e.amount), COUNT(*)
    FROM bursar.credit_ledger_entries e
    WHERE e.amount < 0
      AND e.entry_type = 'usage'
      AND e.created_at BETWEEN p_start AND p_end
    GROUP BY e.created_at::date
    ORDER BY e.created_at::date
$$;

CREATE FUNCTION bursar.aggregate_stats(p_start timestamptz, p_end timestamptz)
RETURNS TABLE(
    total_credits_consumed numeric,
    active_users bigint,
    avg_daily_spend numeric,
    top_model text,
    top_user uuid
)
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT COALESCE(-SUM(e.amount), 0),
           COUNT(DISTINCT a.user_id),
           COALESCE(-SUM(e.amount), 0)
               / GREATEST((p_end::date - p_start::date + 1), 1),
           (SELECT s.model FROM bursar.spend_by_model(p_start, p_end) s
            ORDER BY s.total_spend DESC LIMIT 1),
           (SELECT s.user_id FROM bursar.spend_by_user(p_start, p_end) s
            ORDER BY s.total_spend DESC LIMIT 1)
    FROM bursar.credit_ledger_entries e
    JOIN bursar.credit_accounts a ON a.id = e.account_id
    WHERE e.amount < 0
      AND e.entry_type = 'usage'
      AND e.created_at BETWEEN p_start AND p_end
$$;
