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
    'limited', v_used >= p_max_calls,
    'limit', p_max_calls,
    'used', v_used,
    'remaining', v_remaining,
    'period_start', p_period_start,
    'period_end', p_period_end
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.check_feature_limit(UUID, TEXT, INT, DATE, DATE) FROM PUBLIC, anon, authenticated;

-- Per-function GRANTs to service_role for all bursar functions. REVOKE is
-- already issued against anon/authenticated in each migration; these GRANTs
-- ensure service_role (the backend role) can call them via PostgREST.
GRANT EXECUTE ON FUNCTION public.handle_updated_at() TO service_role;
GRANT EXECUTE ON FUNCTION public.grant_signup_bonus() TO service_role;
GRANT EXECUTE ON FUNCTION public.credits_add(UUID, NUMERIC, public.credit_tx_type, JSONB, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.credits_add_internal(UUID, NUMERIC, public.credit_tx_type, JSONB, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_credits_balance(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.check_spend_cap(UUID, TEXT, NUMERIC) TO service_role;
GRANT EXECUTE ON FUNCTION public.refund_credits(UUID, NUMERIC, TEXT, JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.expire_credits(BOOLEAN, UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.spend_by_user(TIMESTAMPTZ, TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.spend_by_model(TIMESTAMPTZ, TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.top_users(INTEGER, TIMESTAMPTZ, TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.daily_spend(TIMESTAMPTZ, TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.aggregate_stats(TIMESTAMPTZ, TIMESTAMPTZ) TO service_role;
GRANT EXECUTE ON FUNCTION public.list_user_transactions(UUID, TEXT[], TIMESTAMPTZ, TIMESTAMPTZ, INTEGER, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.list_usage_events(UUID, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.create_team(TEXT, NUMERIC) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_team_balance(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.add_team_member(UUID, UUID, TEXT, NUMERIC) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_team_members(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.deduct_team(UUID, UUID, NUMERIC, JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.deduct_with_allowance(UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN, DATE, TEXT, INT, TEXT, DATE, DATE) TO service_role;
GRANT EXECUTE ON FUNCTION public.create_lease(UUID, NUMERIC, TEXT, TEXT, NUMERIC, INTEGER, INTEGER, TEXT, NUMERIC, JSONB, DATE, TEXT, INT, TEXT, DATE, DATE) TO service_role;
GRANT EXECUTE ON FUNCTION public.settle_lease(UUID, UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN, DATE, TEXT, INT, TEXT, DATE, DATE) TO service_role;
GRANT EXECUTE ON FUNCTION public.release_lease(UUID, UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.renew_lease(UUID, UUID, INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_available_credits(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.expire_due_leases() TO service_role;
GRANT EXECUTE ON FUNCTION public._walk_and_debit_buckets(UUID, NUMERIC) TO service_role;
GRANT EXECUTE ON FUNCTION public.sync_buckets_from_config(JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_user_credit_buckets(UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_active_bursar_config() TO service_role;
GRANT EXECUTE ON FUNCTION public.get_bursar_configs() TO service_role;
GRANT EXECUTE ON FUNCTION public.get_bursar_config(INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.check_plan_allowance(UUID, DATE) TO service_role;
GRANT EXECUTE ON FUNCTION public.increment_usage_window(UUID, UUID, NUMERIC, DATE) TO service_role;
GRANT EXECUTE ON FUNCTION public.check_feature_limit(UUID, TEXT, INT, DATE, DATE) TO service_role;

NOTIFY pgrst, 'reload schema';
