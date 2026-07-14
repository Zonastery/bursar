-- bursar: SQL hardening — team-scoped idempotency, service-role guards, balance check.

-- ── Team-scoped idempotency index ───────────────────────────────────────
-- The global (user_id, type, idempotency_key) index prevents the same key
-- from being reused across different teams. Split team_usage into its own
-- partial index that includes team_id.

DROP INDEX IF EXISTS public.idx_credit_transactions_idempotency_user;
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_transactions_idempotency_user
    ON public.credit_transactions (user_id, type, (metadata ->> 'idempotency_key'))
    WHERE metadata ->> 'idempotency_key' IS NOT NULL
      AND type <> 'team_usage';

CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_transactions_idempotency_team_usage
    ON public.credit_transactions (
        user_id,
        type,
        (metadata ->> 'team_id'),
        (metadata ->> 'idempotency_key')
    )
    WHERE type = 'team_usage'
      AND metadata ->> 'idempotency_key' IS NOT NULL;

-- ── credits_add: defense-in-depth service_role guard ────────────────────
CREATE OR REPLACE FUNCTION public.credits_add(
    p_user_id UUID,
    p_amount NUMERIC,
    p_type public.credit_tx_type DEFAULT 'purchase',
    p_metadata JSONB DEFAULT NULL,
    p_bucket TEXT DEFAULT NULL,
    p_idempotency_key TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
BEGIN
    IF current_setting('request.jwt.claim.role', true) IS NOT NULL
       AND current_setting('request.jwt.claim.role', true) <> 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    RETURN public.credits_add_internal(
        p_user_id, p_amount, p_type, p_metadata, p_bucket, p_idempotency_key
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.credits_add(UUID, NUMERIC, public.credit_tx_type, JSONB, TEXT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.credits_add(UUID, NUMERIC, public.credit_tx_type, JSONB, TEXT, TEXT)
    TO service_role;

-- ── check_balance_invariant: diagnostic helper for aggregate vs bucket sum ─
CREATE OR REPLACE FUNCTION public.check_balance_invariant(p_user_id UUID DEFAULT NULL)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_row RECORD;
    v_drift JSONB := '[]'::jsonb;
BEGIN
    FOR v_row IN
        SELECT
            uc.user_id,
            uc.balance AS aggregate_balance,
            COALESCE(SUM(ucb.balance), 0) AS bucket_sum
        FROM public.user_credits uc
        LEFT JOIN public.user_credit_buckets ucb ON ucb.user_id = uc.user_id
        WHERE p_user_id IS NULL OR uc.user_id = p_user_id
        GROUP BY uc.user_id, uc.balance
        HAVING uc.balance <> COALESCE(SUM(ucb.balance), 0)
    LOOP
        v_drift := v_drift || jsonb_build_array(jsonb_build_object(
            'user_id', v_row.user_id,
            'aggregate_balance', v_row.aggregate_balance,
            'bucket_sum', v_row.bucket_sum,
            'delta', v_row.aggregate_balance - v_row.bucket_sum
        ));
    END LOOP;

    RETURN jsonb_build_object(
        'ok', jsonb_array_length(v_drift) = 0,
        'drift_count', jsonb_array_length(v_drift),
        'drift', v_drift
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.check_balance_invariant(UUID) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.check_balance_invariant(UUID) TO service_role;

NOTIFY pgrst, 'reload schema';
