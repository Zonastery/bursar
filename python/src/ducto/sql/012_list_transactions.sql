-- 012_list_transactions.sql
-- RPC to list user credit transactions with pagination.
--
-- H18: this SECURITY DEFINER function must NOT be callable by anon/authenticated
-- without an ownership check, or any authenticated client could read arbitrary
-- users' history by passing p_user_id. We REVOKE execute from anon/authenticated
-- and add an auth.uid()/role guard consistent with the other RPCs.
-- amount is NUMERIC(18,4) (M11); changing the TABLE column type requires a DROP
-- (CREATE OR REPLACE cannot change a function's return type).

DROP FUNCTION IF EXISTS public.list_user_transactions(UUID, TEXT[], TIMESTAMPTZ, TIMESTAMPTZ, INTEGER, INTEGER);

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
  -- Authorization (consistent with 001–011 + defense-in-depth): execute is
  -- REVOKEd from anon/authenticated below, so in practice only service_role
  -- reaches this. The in-body guard additionally ensures that even if execute
  -- were granted to an end-user role, a caller can only read their OWN rows.
  IF auth.role() IS DISTINCT FROM 'service_role' AND auth.uid() IS DISTINCT FROM p_user_id THEN
    RETURN;
  END IF;

  -- First, count total matching rows for pagination
  SELECT COUNT(*) INTO v_total
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND (p_types IS NULL OR ct.type::TEXT = ANY(p_types))
    AND (p_from_date IS NULL OR ct.created_at >= p_from_date)
    AND (p_to_date IS NULL OR ct.created_at <= p_to_date);

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
    AND (p_to_date IS NULL OR ct.created_at <= p_to_date)
  ORDER BY ct.created_at DESC
  LIMIT p_limit
  OFFSET p_offset;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.list_user_transactions(UUID, TEXT[], TIMESTAMPTZ, TIMESTAMPTZ, INTEGER, INTEGER) FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
