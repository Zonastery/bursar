-- bursar: per-feature invocation-count limits (cadence-based rate limiting).
--
-- Ledger-derived, exactly like spend caps (005_spend_caps.sql): there is NO
-- new counter table. A feature invocation is counted by counting
-- already-committed `usage` transactions whose `metadata.feature ==
-- <feature>` within `[period_start, period_end)`. This gets three things for
-- free: (1) `release_lease` never counts (it never inserts a `usage` row);
-- (2) `refund_credits` never frees up quota (the original `usage` row is
-- untouched by a refund); (3) no new storage/migration is needed beyond this
-- one advisory read RPC.
--
-- The authoritative enforcement (deny at admission/deduct, advisory warning
-- at settle) lives inline in the three atomic RPCs edited in
-- 009_deduct_and_leases.sql (`deduct_with_allowance`, `create_lease`,
-- `settle_lease`) — this migration only adds the advisory `check_feature_limit`
-- read RPC (UI-only, never used for admission control), modeled directly on
-- `check_spend_cap` (same SECURITY DEFINER / service_role guard / empty
-- search_path pattern).

-- check_feature_limit: evaluate a user's current invocation count for a
-- feature within an explicit [p_period_start, p_period_end) window. The
-- caller (the manager) has already resolved the FeatureLimit from the user's
-- plan and the calendar window via resolve_calendar_window — this RPC only
-- counts. Pure read, no side effects, never an admission gate.
CREATE OR REPLACE FUNCTION public.check_feature_limit(
  p_user_id UUID,
  p_feature TEXT,
  p_max_calls INT,
  p_period_start DATE,
  p_period_end DATE
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_used      INT;
  v_remaining INT;
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    RETURN jsonb_build_object('limited', false, 'error', 'unauthorized');
  END IF;

  -- Windows are pinned to UTC (dates are treated as UTC midnight) for
  -- deterministic bucketing, matching resolve_calendar_window's contract.
  -- Deliberately no `amount < 0` filter (unlike check_spend_cap, which only
  -- cares about actual dollars spent): a call fully covered by free allowance
  -- nets to amount = 0 but is still one invocation and must still count.
  SELECT COUNT(*) INTO v_used
  FROM public.credit_transactions ct
  WHERE ct.user_id = p_user_id
    AND ct.type = 'usage'
    AND ct.metadata->>'feature' = p_feature
    AND ct.created_at >= (p_period_start::timestamp AT TIME ZONE 'UTC')
    AND ct.created_at < (p_period_end::timestamp AT TIME ZONE 'UTC');

  v_used := COALESCE(v_used, 0);
  v_remaining := GREATEST(p_max_calls - v_used, 0);

  RETURN jsonb_build_object(
    'limited', true,
    'limit', p_max_calls,
    'used', v_used,
    'remaining', v_remaining,
    'period_start', p_period_start,
    'period_end', p_period_end
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.check_feature_limit(UUID, TEXT, INT, DATE, DATE) FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
