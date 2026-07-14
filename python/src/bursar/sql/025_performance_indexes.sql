-- bursar: analytics pagination optimization + hot-path indexes (apply after EXPLAIN review).

CREATE OR REPLACE FUNCTION public.list_user_transactions(
  p_user_id UUID,
  p_types TEXT[] DEFAULT NULL,
  p_from_date TIMESTAMPTZ DEFAULT NULL,
  p_to_date TIMESTAMPTZ DEFAULT NULL,
  p_limit INTEGER DEFAULT 50,
  p_offset INTEGER DEFAULT 0
)
RETURNS TABLE (
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
BEGIN
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
    COUNT(*) OVER() AS total_count
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

CREATE INDEX IF NOT EXISTS idx_credit_transactions_usage_feature_created
    ON public.credit_transactions (user_id, (metadata->>'feature'), created_at)
    WHERE type = 'usage' AND metadata ? 'feature';

CREATE INDEX IF NOT EXISTS idx_credit_transactions_team_usage_created
    ON public.credit_transactions (user_id, (metadata->>'team_id'), created_at)
    WHERE type = 'team_usage' AND metadata ? 'team_id';

NOTIFY pgrst, 'reload schema';
