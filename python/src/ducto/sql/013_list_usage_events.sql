-- 013_list_usage_events.sql
-- Dedicated RPC to list usage-type credit transactions for a user.
-- Separate from list_user_transactions to avoid passing types filter for
-- the common case of fetching consumption events.
--
-- H18: REVOKE execute from anon/authenticated + add an auth.uid()/role guard
-- consistent with the other RPCs, so a Supabase client cannot read arbitrary
-- users' usage history. amount is NUMERIC(18,4) (M11); changing the TABLE
-- column type requires a DROP first.

DROP FUNCTION IF EXISTS public.list_usage_events(UUID, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER, INTEGER);

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
  -- Authorization (consistent with 001–011 + defense-in-depth): execute is
  -- REVOKEd from anon/authenticated below; the in-body guard additionally
  -- limits any non-service_role caller to their OWN rows.
  IF auth.role() IS DISTINCT FROM 'service_role' AND auth.uid() IS DISTINCT FROM p_user_id THEN
    RETURN;
  END IF;

  SELECT COUNT(*) INTO v_total
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND ct.type = 'usage'
    AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
    AND (p_to_date IS NULL OR ct.created_at <= p_to_date);

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
    AND (p_to_date IS NULL OR ct.created_at <= p_to_date)
  ORDER BY ct.created_at DESC
  LIMIT p_limit
  OFFSET p_offset;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.list_usage_events(UUID, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER, INTEGER) FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
