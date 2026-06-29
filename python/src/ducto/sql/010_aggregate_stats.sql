-- ducto: aggregate statistics across all users.
-- Idempotent — safe to run multiple times (CREATE OR REPLACE).

CREATE OR REPLACE FUNCTION public.aggregate_stats(p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS JSON
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    result JSON;
    day_count BIGINT;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN NULL;
    END IF;

    -- Count distinct days in the window (UTC buckets for determinism — M16)
    SELECT COUNT(DISTINCT (created_at AT TIME ZONE 'UTC')::DATE) INTO day_count
    FROM public.credit_transactions
    WHERE type = 'usage'
      AND amount < 0
      AND created_at >= p_start
      AND created_at <= p_end;

    -- Money is NUMERIC(18,4): consumed total and the average stay NUMERIC.
    -- avg_daily_spend uses NUMERIC division (not integer division) so sub-credit
    -- daily averages are not truncated to 0 (M11). The SUM is cast to NUMERIC
    -- explicitly so dividing by a BIGINT day_count yields a NUMERIC quotient.
    SELECT json_build_object(
        'total_credits_consumed', COALESCE(SUM(ABS(amount)), 0)::NUMERIC,
        'active_users', COUNT(DISTINCT user_id)::BIGINT,
        'avg_daily_spend', CASE WHEN day_count > 0
            THEN COALESCE(SUM(ABS(amount))::NUMERIC / day_count::NUMERIC, 0)
            ELSE 0::NUMERIC END,
        'top_model', COALESCE(
            (SELECT metadata->>'model'
             FROM public.credit_transactions
             WHERE type = 'usage'
               AND amount < 0
               AND created_at >= p_start
               AND created_at <= p_end
             GROUP BY metadata->>'model'
             ORDER BY SUM(ABS(amount)) DESC
             LIMIT 1),
            ''
        ),
        'top_user', COALESCE(
            (SELECT user_id::TEXT
             FROM public.credit_transactions
             WHERE type = 'usage'
               AND amount < 0
               AND created_at >= p_start
               AND created_at <= p_end
             GROUP BY user_id
             ORDER BY SUM(ABS(amount)) DESC
             LIMIT 1),
            ''
        )
    ) INTO result
    FROM public.credit_transactions
    WHERE type = 'usage'
      AND amount < 0
      AND created_at >= p_start
      AND created_at <= p_end;

    RETURN result;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.aggregate_stats FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
