-- ducto: usage analytics queries.
-- Idempotent — safe to run multiple times (CREATE OR REPLACE).

-- spend_by_user: aggregate spend by user in a time window.
CREATE OR REPLACE FUNCTION public.spend_by_user(p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS TABLE(user_id TEXT, total_spend BIGINT, transaction_count BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ct.user_id::TEXT,
        COALESCE(SUM(ABS(ct.amount)), 0)::BIGINT AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM public.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at <= p_end
    GROUP BY ct.user_id
    ORDER BY total_spend DESC;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.spend_by_user FROM anon, authenticated;

-- spend_by_model: aggregate spend by model in a time window.
CREATE OR REPLACE FUNCTION public.spend_by_model(p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS TABLE(model TEXT, total_spend BIGINT, transaction_count BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        COALESCE(ct.metadata->>'model', 'unknown')::TEXT AS model,
        COALESCE(SUM(ABS(ct.amount)), 0)::BIGINT AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM public.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at <= p_end
    GROUP BY ct.metadata->>'model'
    ORDER BY total_spend DESC;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.spend_by_model FROM anon, authenticated;

-- top_users: top users by spend in a time window.
CREATE OR REPLACE FUNCTION public.top_users(p_limit INTEGER, p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS TABLE(user_id TEXT, total_spend BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ct.user_id::TEXT,
        COALESCE(SUM(ABS(ct.amount)), 0)::BIGINT AS total_spend
    FROM public.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at <= p_end
    GROUP BY ct.user_id
    ORDER BY total_spend DESC
    LIMIT p_limit;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.top_users FROM anon, authenticated;

-- daily_spend: daily spend aggregation in a time window.
CREATE OR REPLACE FUNCTION public.daily_spend(p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS TABLE(date TEXT, total_spend BIGINT, transaction_count BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ct.created_at::DATE::TEXT AS date,
        COALESCE(SUM(ABS(ct.amount)), 0)::BIGINT AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM public.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at <= p_end
    GROUP BY ct.created_at::DATE
    ORDER BY ct.created_at::DATE;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.daily_spend FROM anon, authenticated;

CREATE INDEX IF NOT EXISTS idx_credit_transactions_created_at ON public.credit_transactions (created_at);
CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_id_created_at ON public.credit_transactions (user_id, created_at);

NOTIFY pgrst, 'reload schema';
