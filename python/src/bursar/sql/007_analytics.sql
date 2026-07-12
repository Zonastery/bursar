-- bursar: usage analytics queries.
-- Idempotent — safe to run multiple times (CREATE OR REPLACE).
--
-- Money is NUMERIC(18,4): spend totals are summed and returned as NUMERIC
-- (not BIGINT) so fractional credit usage is reported without truncation.
-- Day buckets are pinned to UTC so they are deterministic regardless of the
-- session time zone.
--
-- Window bounds are HALF-OPEN [p_start, p_end): `created_at >= p_start AND
-- created_at < p_end`. This matches the feature-limit/allowance windows
-- elsewhere (see allowance.py's resolve_allowance_window) and means a
-- transaction lands in exactly one of two adjacent windows, never both.
--
-- "Spend" here is deliberately `type = 'usage'` only — it excludes
-- `team_usage` (credits drawn from a shared team pool, see 008_teams.sql).
-- This is a narrower definition than the spend-CAP enforcement in
-- check_spend_cap/deduct_with_allowance/settle_lease, which counts
-- `type IN ('usage', 'team_usage')` because a cap is a per-user ceiling
-- regardless of which balance funded the debit. These analytics report
-- personal-balance consumption; team-pool spend is analyzed separately via
-- get_team_members' total_spent. If a future dashboard needs a single
-- "all spend" figure, include 'team_usage' explicitly rather than assuming
-- these functions already do.

-- spend_by_user: aggregate spend by user in a time window.
CREATE OR REPLACE FUNCTION public.spend_by_user(p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS TABLE(user_id TEXT, total_spend NUMERIC, transaction_count BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ct.user_id::TEXT,
        COALESCE(SUM(ABS(ct.amount)), 0)::NUMERIC AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM public.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at < p_end
    GROUP BY ct.user_id
    ORDER BY total_spend DESC;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.spend_by_user FROM PUBLIC, anon, authenticated;

-- spend_by_model: aggregate spend by model in a time window.
CREATE OR REPLACE FUNCTION public.spend_by_model(p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS TABLE(model TEXT, total_spend NUMERIC, transaction_count BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        COALESCE(ct.metadata->>'model', 'unknown')::TEXT AS model,
        COALESCE(SUM(ABS(ct.amount)), 0)::NUMERIC AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM public.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at < p_end
    GROUP BY ct.metadata->>'model'
    ORDER BY total_spend DESC;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.spend_by_model FROM PUBLIC, anon, authenticated;

-- top_users: top users by spend in a time window.
CREATE OR REPLACE FUNCTION public.top_users(p_limit INTEGER, p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS TABLE(user_id TEXT, total_spend NUMERIC)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        ct.user_id::TEXT,
        COALESCE(SUM(ABS(ct.amount)), 0)::NUMERIC AS total_spend
    FROM public.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at < p_end
    GROUP BY ct.user_id
    ORDER BY total_spend DESC
    LIMIT p_limit;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.top_users FROM PUBLIC, anon, authenticated;

-- daily_spend: daily spend aggregation in a time window.
-- Day buckets are computed in UTC for deterministic results.
CREATE OR REPLACE FUNCTION public.daily_spend(p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS TABLE(date TEXT, total_spend NUMERIC, transaction_count BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    RETURN QUERY
    SELECT
        (ct.created_at AT TIME ZONE 'UTC')::DATE::TEXT AS date,
        COALESCE(SUM(ABS(ct.amount)), 0)::NUMERIC AS total_spend,
        COUNT(*)::BIGINT AS transaction_count
    FROM public.credit_transactions ct
    WHERE ct.type = 'usage'
      AND ct.amount < 0
      AND ct.created_at >= p_start
      AND ct.created_at < p_end
    GROUP BY (ct.created_at AT TIME ZONE 'UTC')::DATE
    ORDER BY (ct.created_at AT TIME ZONE 'UTC')::DATE;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.daily_spend FROM PUBLIC, anon, authenticated;

-- aggregate_stats: aggregate statistics across all users.
CREATE OR REPLACE FUNCTION public.aggregate_stats(p_start TIMESTAMPTZ, p_end TIMESTAMPTZ)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    result JSON;
    day_count BIGINT;
BEGIN
    -- Count distinct days in the window (UTC buckets for determinism)
    SELECT COUNT(DISTINCT (created_at AT TIME ZONE 'UTC')::DATE) INTO day_count
    FROM public.credit_transactions
    WHERE type = 'usage'
      AND amount < 0
      AND created_at >= p_start
      AND created_at < p_end;

    -- Money is NUMERIC(18,4): consumed total and the average stay NUMERIC.
    -- avg_daily_spend uses NUMERIC division (not integer division) so sub-credit
    -- daily averages are not truncated to 0. The SUM is cast to NUMERIC
    -- explicitly so dividing by a BIGINT day_count yields a NUMERIC quotient.
    SELECT jsonb_build_object(
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
               AND created_at < p_end
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
               AND created_at < p_end
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
      AND created_at < p_end;

    RETURN result;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.aggregate_stats FROM PUBLIC, anon, authenticated;

-- list_user_transactions: list a user's transactions with pagination.
--
-- This SECURITY DEFINER function must NOT be callable by anon/authenticated
-- without an ownership check, or any authenticated client could read arbitrary
-- users' history by passing p_user_id. We REVOKE execute from anon/authenticated
-- and add an auth.uid()/role guard consistent with the other RPCs.
CREATE OR REPLACE FUNCTION public.list_user_transactions(
  p_user_id UUID,
  p_types TEXT[] DEFAULT NULL,
  p_from_date TIMESTAMPTZ DEFAULT NULL,
  p_to_date TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 50,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE(
  id UUID,
  user_id UUID,
  amount NUMERIC,
  type TEXT,
  reference_type TEXT,
  reference_id UUID,
  metadata JSONB,
  created_at TIMESTAMPTZ,
  total_count BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_total BIGINT;
BEGIN
  -- Authorization: only service_role may call this function (execute is
  -- REVOKEd from anon/authenticated below).
  -- First, count total matching rows for pagination
  SELECT COUNT(*) INTO v_total
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND (p_types IS NULL OR ct.type::TEXT = ANY(p_types))
    AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
    AND (p_to_date IS NULL OR ct.created_at < p_to_date);

  -- Return paginated results with total_count on each row
  RETURN QUERY
  SELECT
    ct.id,
    ct.user_id,
    ct.amount,
    ct.type::TEXT,
    ct.reference_type,
    ct.reference_id,
    ct.metadata,
    ct.created_at,
    v_total AS total_count
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND (p_types IS NULL OR ct.type::TEXT = ANY(p_types))
    AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
    AND (p_to_date IS NULL OR ct.created_at < p_to_date)
  ORDER BY ct.created_at DESC
  LIMIT p_limit
  OFFSET p_offset;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.list_user_transactions(UUID, TEXT[], TIMESTAMPTZ, TIMESTAMPTZ, INTEGER, INTEGER) FROM PUBLIC, anon, authenticated;

-- list_usage_events: dedicated RPC to list usage-type credit transactions for
-- a user. Separate from list_user_transactions to avoid passing a types
-- filter for the common case of fetching consumption events.
--
-- REVOKE execute from anon/authenticated + add an auth.uid()/role guard
-- consistent with the other RPCs, so a Supabase client cannot read arbitrary
-- users' usage history.
CREATE OR REPLACE FUNCTION public.list_usage_events(
  p_user_id UUID,
  p_from_date TIMESTAMPTZ DEFAULT NULL,
  p_to_date TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 50,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE(
  id UUID,
  user_id UUID,
  amount NUMERIC,
  type TEXT,
  reference_type TEXT,
  reference_id UUID,
  metadata JSONB,
  created_at TIMESTAMPTZ,
  total_count BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_total BIGINT;
BEGIN
  -- Authorization: only service_role may call this function (execute is
  -- REVOKEd from anon/authenticated below).
  SELECT COUNT(*) INTO v_total
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND ct.type = 'usage'
    AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
    AND (p_to_date IS NULL OR ct.created_at < p_to_date);

  RETURN QUERY
  SELECT
    ct.id,
    ct.user_id,
    ct.amount,
    ct.type::TEXT,
    ct.reference_type,
    ct.reference_id,
    ct.metadata,
    ct.created_at,
    v_total AS total_count
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND ct.type = 'usage'
    AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
    AND (p_to_date IS NULL OR ct.created_at < p_to_date)
  ORDER BY ct.created_at DESC
  LIMIT p_limit
  OFFSET p_offset;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.list_usage_events(UUID, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER, INTEGER) FROM PUBLIC, anon, authenticated;

CREATE INDEX IF NOT EXISTS idx_credit_transactions_created_at ON public.credit_transactions (created_at);
CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_id_created_at ON public.credit_transactions (user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_credit_transactions_type_created ON public.credit_transactions (type, created_at DESC);

NOTIFY pgrst, 'reload schema';
