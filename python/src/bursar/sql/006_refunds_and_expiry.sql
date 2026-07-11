-- bursar: credit refund and expiry/TTL support.
-- Idempotent — safe to run multiple times (CREATE OR REPLACE).

-- refund_credits: reverse a credit deduction (full or partial), atomically.
--
-- Everything below happens in ONE transaction. The original ledger row and the
-- balance row are taken FOR UPDATE so concurrent refunds against the same
-- original transaction serialize and cannot race the over-refund check.
-- All money is NUMERIC(18,4).
--
-- Business outcomes (structured `{"error": code}` envelope; the manager maps
-- codes to typed exceptions):
--   * not_found       — no such original transaction.
--   * over_refund     — refunding would exceed the original debit, OR the
--                       referenced transaction is NOT a debit (a credit /
--                       purchase / refund / adjustment has zero refundable
--                       amount, so any refund over-refunds). See note below on
--                       why over_refund (not not_found) is used for non-debits.
--   * already_refunded — an exact duplicate of a prior full refund (back-compat).
--
-- LIFO bucket restoration (see 010_credit_buckets.sql): restores bucket balances
-- (reverse priority order), deriving each bucket's already-refunded amount from
-- the sum of all prior refunds' own bucket_breakdown — never a running counter —
-- so repeated partial refunds compose correctly.
--
-- On success returns: refund_transaction_id, user_id, amount, new_balance,
-- bucket_breakdown. All error envelopes also carry user_id + new_balance so the
-- store/manager can surface the current balance uniformly.
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
    -- Bucket LIFO restoration
    v_orig_breakdown JSONB;
    v_prior_refund_breakdown JSONB;
    v_new_breakdown JSONB := '{}'::jsonb;
    v_to_allocate NUMERIC;
    v_bucket_key TEXT;
    v_bucket_orig_amt NUMERIC;
    v_bucket_prior NUMERIC;
    v_bucket_remaining NUMERIC;
    v_give NUMERIC;
BEGIN
    -- Prevent concurrent refund on same transaction (advisory + row locks below).
    PERFORM pg_advisory_xact_lock(hashtext('refund_' || p_transaction_id));

    -- Fetch + lock the original transaction row so its refund total cannot move
    -- under us while we compute the over-refund check. metadata is selected so
    -- the bucket_breakdown driving LIFO restoration is available.
    SELECT id, user_id, amount, type, metadata INTO v_tx
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

    -- ── Bucket LIFO restoration ─────────────────────────────────────────────
    -- bucket_remaining[t] is derived fresh each time from
    -- original_breakdown[t] - sum(prior refunds' own breakdown[t]) — never a
    -- running counter — so repeated partial refunds compose correctly.
    v_orig_breakdown := COALESCE(v_tx.metadata->'bucket_breakdown', jsonb_build_object('default', v_original_debit));

    SELECT COALESCE(jsonb_object_agg(kv.bucket_key, kv.bucket_sum), '{}'::jsonb) INTO v_prior_refund_breakdown
    FROM (
        SELECT e.key AS bucket_key, SUM((e.value)::numeric) AS bucket_sum
        FROM public.credit_transactions ct
        CROSS JOIN LATERAL jsonb_each_text(COALESCE(ct.metadata->'bucket_breakdown', '{}'::jsonb)) AS e(key, value)
        WHERE ct.reference_id = p_transaction_id AND ct.type = 'refund'
        GROUP BY e.key
    ) kv;

    v_to_allocate := v_refund_amount;

    -- Walk buckets in REVERSE priority order (highest-priority-number / last
    -- drained bucket first). Buckets no longer present in credit_buckets (config
    -- drift) sort last, mirroring the deduct walk's "orphans appended last".
    FOR v_bucket_key, v_bucket_orig_amt IN
        SELECT e.key, (e.value)::numeric
        FROM jsonb_each_text(v_orig_breakdown) AS e(key, value)
        LEFT JOIN public.credit_buckets ct ON ct.bucket_key = e.key
        ORDER BY COALESCE(ct.priority, -2147483648) DESC, e.key DESC
    LOOP
        EXIT WHEN v_to_allocate <= 0;

        v_bucket_prior := COALESCE((v_prior_refund_breakdown->>v_bucket_key)::numeric, 0);
        v_bucket_remaining := GREATEST(v_bucket_orig_amt - v_bucket_prior, 0);
        v_give := LEAST(v_bucket_remaining, v_to_allocate);

        IF v_give > 0 THEN
            INSERT INTO public.user_credit_buckets (user_id, bucket_key, balance)
            VALUES (v_tx.user_id, v_bucket_key, v_give)
            ON CONFLICT (user_id, bucket_key) DO UPDATE SET
                balance = public.user_credit_buckets.balance + v_give,
                updated_at = now();

            v_new_breakdown := v_new_breakdown || jsonb_build_object(v_bucket_key, v_give);
            v_to_allocate := v_to_allocate - v_give;
        END IF;
    END LOOP;

    INSERT INTO public.credit_transactions (user_id, amount, type, reference_type, reference_id, metadata)
    VALUES (v_tx.user_id, v_refund_amount, 'refund', p_reason, p_transaction_id,
            p_metadata || jsonb_build_object('reason', p_reason, 'bucket_breakdown', v_new_breakdown))
    RETURNING id INTO v_refund_tx_id;

    RETURN jsonb_build_object(
        'refund_transaction_id', v_refund_tx_id,
        'user_id', v_tx.user_id,
        'amount', v_refund_amount,
        'new_balance', v_new_balance,
        'bucket_breakdown', v_new_breakdown
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.refund_credits FROM PUBLIC, anon, authenticated;

-- expire_credits: sweep expired credits from all users' balances, grouped
-- per-(user_id, bucket). Returns count, amount, and per-bucket breakdown of
-- expired credits. When p_dry_run is true, reports without modifying state.
CREATE OR REPLACE FUNCTION public.expire_credits(p_dry_run BOOLEAN DEFAULT false)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_expired_count INTEGER := 0;
    v_expired_amount NUMERIC := 0;
    v_expired_by_bucket JSONB := '{}'::jsonb;
    v_group RECORD;
    v_group_expired NUMERIC;
    v_current_bucket_balance NUMERIC;
    v_current_balance NUMERIC;
BEGIN
    -- A grant is "sweepable" when it has an expires_at in the past AND has not
    -- already been swept (no 'swept_at' marker). Marking swept grants is what
    -- makes the sweep idempotent: a second run finds nothing and never
    -- double-debits. Mirrors MemoryStore, which nulls expires_at on sweep.
    -- Grouping is per-(user_id, bucket) instead of per-user_id, reading bucket
    -- straight off each grant's own metadata->>'bucket' (a bucket's `expires` flag
    -- is only consulted at add_credits time; once stamped, a transaction's
    -- fate is fixed regardless of later config changes).
    FOR v_group IN
        SELECT DISTINCT user_id, COALESCE(metadata->>'bucket', 'default') AS bucket_key
        FROM public.credit_transactions
        WHERE type IN ('purchase', 'subscription', 'signup_bonus', 'adjustment')
          AND metadata ? 'expires_at'
          AND NOT (metadata ? 'swept_at')
          AND (metadata->>'expires_at')::timestamptz <= now()
    LOOP
        -- Total un-swept expired grants for this (user, bucket).
        SELECT COALESCE(SUM(amount), 0) INTO v_group_expired
        FROM public.credit_transactions
        WHERE user_id = v_group.user_id
          AND COALESCE(metadata->>'bucket', 'default') = v_group.bucket_key
          AND type IN ('purchase', 'subscription', 'signup_bonus', 'adjustment')
          AND metadata ? 'expires_at'
          AND NOT (metadata ? 'swept_at')
          AND (metadata->>'expires_at')::timestamptz <= now();

        -- Lock the aggregate balance row (prevents racing a concurrent deduction).
        SELECT COALESCE(balance, 0) INTO v_current_balance
        FROM public.user_credits
        WHERE user_id = v_group.user_id
        FOR UPDATE;

        -- Lock (if present) this bucket's own balance row for this user.
        SELECT balance INTO v_current_bucket_balance
        FROM public.user_credit_buckets
        WHERE user_id = v_group.user_id AND bucket_key = v_group.bucket_key
        FOR UPDATE;
        v_current_bucket_balance := COALESCE(v_current_bucket_balance, 0);

        -- Cap at both the bucket's own balance and the aggregate (never expire
        -- money that isn't actually there under either ceiling).
        v_group_expired := LEAST(v_group_expired, v_current_bucket_balance, v_current_balance);

        IF v_group_expired > 0 THEN
            v_expired_count := v_expired_count + 1;
            v_expired_amount := v_expired_amount + v_group_expired;
            v_expired_by_bucket := v_expired_by_bucket || jsonb_build_object(
                v_group.bucket_key,
                COALESCE((v_expired_by_bucket->>v_group.bucket_key)::numeric, 0) + v_group_expired
            );

            IF NOT p_dry_run THEN
                -- Deduct expired amount from both the bucket and aggregate balances.
                UPDATE public.user_credit_buckets
                SET balance = balance - v_group_expired, updated_at = now()
                WHERE user_id = v_group.user_id AND bucket_key = v_group.bucket_key;

                UPDATE public.user_credits
                SET balance = balance - v_group_expired,
                    updated_at = now()
                WHERE user_id = v_group.user_id;

                -- Log one adjustment transaction per (user, bucket).
                INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
                VALUES (v_group.user_id, -v_group_expired, 'adjustment',
                        jsonb_build_object('reason', 'credit_expired', 'expired_amount', v_group_expired, 'bucket', v_group.bucket_key));
            END IF;
        END IF;

        -- Mark the grants we just considered as swept so they're never
        -- re-swept (only on a real run; a dry run must not mutate state).
        IF NOT p_dry_run THEN
            UPDATE public.credit_transactions
            SET metadata = metadata || jsonb_build_object('swept_at', to_jsonb(now()))
            WHERE user_id = v_group.user_id
              AND COALESCE(metadata->>'bucket', 'default') = v_group.bucket_key
              AND type IN ('purchase', 'subscription', 'signup_bonus', 'adjustment')
              AND metadata ? 'expires_at'
              AND NOT (metadata ? 'swept_at')
              AND (metadata->>'expires_at')::timestamptz <= now();
        END IF;
    END LOOP;

    RETURN jsonb_build_object(
        'expired_count', v_expired_count,
        'expired_amount', v_expired_amount,
        'expired_by_bucket', v_expired_by_bucket,
        'dry_run', p_dry_run
    );
END;
$$;

-- Index for expiry sweep (finds un-swept expired grants without full scan)
CREATE INDEX IF NOT EXISTS idx_credit_transactions_expires_at
    ON public.credit_transactions ((metadata ->> 'expires_at'))
    WHERE metadata ? 'expires_at' AND NOT (metadata ? 'swept_at');

-- Index for refund_credits' reference_id lookups (duplicate detection, prior-
-- refund sum, prior bucket_breakdown) — otherwise each is a seq scan over the
-- ever-growing ledger.
CREATE INDEX IF NOT EXISTS idx_credit_transactions_reference_id
    ON public.credit_transactions (reference_id)
    WHERE reference_id IS NOT NULL;

-- Qualified with the exact signature defined above (011_lazy_expiry.sql adds a
-- p_user_id-scoped overload; an unqualified REVOKE here would become ambiguous
-- ("function name is not unique") the moment that second overload exists,
-- breaking idempotent re-migration exactly like the credits_add bug fixed in
-- 002_credit_rpcs.sql — see 011_lazy_expiry.sql for the full explanation).
REVOKE EXECUTE ON FUNCTION public.expire_credits(BOOLEAN) FROM PUBLIC, anon, authenticated;

NOTIFY pgrst, 'reload schema';
