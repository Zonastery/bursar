-- ducto: team/shared balance pools.
-- credit_teams, credit_team_members tables, RPCs for team credit operations.
--
-- NOTE: the 'team_usage' enum value is added in 001 (NOT here). Postgres forbids
-- using a freshly-added enum value in the same transaction that adds it, so it
-- must be committed by an earlier migration before any function below uses it (H5).
--
-- Money columns are NUMERIC(18,4) (M11): team balance, member spend_cap/total_spent.

CREATE TABLE IF NOT EXISTS public.credit_teams (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  name TEXT NOT NULL,
  balance NUMERIC(18,4) NOT NULL DEFAULT 0,
  member_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.credit_team_members (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  team_id UUID NOT NULL REFERENCES public.credit_teams(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES public.user_credits(user_id),
  role TEXT NOT NULL DEFAULT 'member',
  spend_cap NUMERIC(18,4),
  total_spent NUMERIC(18,4) NOT NULL DEFAULT 0,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (team_id, user_id)
);

-- RLS: server-only access (managed through RPCs)
ALTER TABLE public.credit_teams ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only credit_teams' AND tablename = 'credit_teams') THEN
    CREATE POLICY "Server-only credit_teams" ON public.credit_teams USING (false);
  END IF;
END;
$$;

ALTER TABLE public.credit_team_members ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only credit_team_members' AND tablename = 'credit_team_members') THEN
    CREATE POLICY "Server-only credit_team_members" ON public.credit_team_members USING (false);
  END IF;
END;
$$;

-- Money params moved INTEGER -> NUMERIC (M11). Drop old overloads so the
-- NUMERIC definitions fully replace them (no-ops on fresh installs).
DROP FUNCTION IF EXISTS public.create_team(TEXT, INTEGER);
DROP FUNCTION IF EXISTS public.add_team_member(UUID, UUID, TEXT, INTEGER);
DROP FUNCTION IF EXISTS public.deduct_team(UUID, UUID, INTEGER, JSONB);

-- create_team: create a team with optional initial balance.
CREATE OR REPLACE FUNCTION public.create_team(
  p_name TEXT,
  p_initial_balance NUMERIC DEFAULT 0
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_team_id UUID;
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    RETURN jsonb_build_object('error', 'unauthorized');
  END IF;

  INSERT INTO public.credit_teams (name, balance)
  VALUES (p_name, p_initial_balance)
  RETURNING id INTO v_team_id;

  RETURN jsonb_build_object(
    'team_id', v_team_id,
    'name', p_name
  );
END;
$$;

-- get_team_balance: fetch team balance and member count.
CREATE OR REPLACE FUNCTION public.get_team_balance(p_team_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_team RECORD;
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    RETURN jsonb_build_object('error', 'unauthorized');
  END IF;

  SELECT id, name, balance, member_count INTO v_team
  FROM public.credit_teams
  WHERE id = p_team_id;

  IF v_team.id IS NULL THEN
    RETURN jsonb_build_object('error', 'team_not_found');
  END IF;

  RETURN jsonb_build_object(
    'team_id', v_team.id,
    'name', v_team.name,
    'balance', v_team.balance,
    'member_count', v_team.member_count
  );
END;
$$;

-- add_team_member: add a user to a team.
CREATE OR REPLACE FUNCTION public.add_team_member(
  p_team_id UUID,
  p_user_id UUID,
  p_role TEXT DEFAULT 'member',
  p_spend_cap NUMERIC DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    RETURN jsonb_build_object('error', 'unauthorized');
  END IF;

  INSERT INTO public.credit_team_members (team_id, user_id, role, spend_cap, total_spent)
  VALUES (p_team_id, p_user_id, p_role, p_spend_cap, 0)
  ON CONFLICT (team_id, user_id) DO UPDATE SET
    role = p_role,
    spend_cap = COALESCE(p_spend_cap, credit_team_members.spend_cap);

  UPDATE public.credit_teams
  SET member_count = (SELECT COUNT(*) FROM public.credit_team_members WHERE team_id = p_team_id),
      updated_at = now()
  WHERE id = p_team_id;

  RETURN jsonb_build_object(
    'team_id', p_team_id,
    'user_id', p_user_id,
    'role', p_role
  );
END;
$$;

-- get_team_members: list all members of a team (SETOF).
--
-- C4: credit_transactions has no team_id column — the team id lives in
-- metadata->>'team_id', so the join reads it from there.
-- M2 / contract §3: total_spent is the SAME monthly-windowed team_usage spend
-- that deduct_team enforces the per-user spend_cap against (single source of
-- truth, reset monthly). ABS() turns the stored negative debit into a positive
-- spent figure. Buckets are pinned to UTC for determinism (M16).
CREATE OR REPLACE FUNCTION public.get_team_members(p_team_id UUID)
RETURNS SETOF JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    RETURN;
  END IF;

  RETURN QUERY
  SELECT jsonb_build_object(
    'user_id', tm.user_id,
    'role', tm.role,
    'spend_cap', tm.spend_cap,
    'total_spent', COALESCE(SUM(ABS(ct.amount)) FILTER (
        WHERE ct.type = 'team_usage'
          AND ct.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')
      ), 0),
    'joined_at', tm.joined_at
  )
  FROM public.credit_team_members tm
  LEFT JOIN public.credit_transactions ct
    ON ct.user_id = tm.user_id
   AND ct.metadata->>'team_id' = p_team_id::text
  WHERE tm.team_id = p_team_id
  GROUP BY tm.user_id, tm.role, tm.spend_cap, tm.joined_at
  ORDER BY tm.joined_at;
END;
$$;

-- deduct_team: deduct credits from team pool, attribute to user.
--
-- Money is NUMERIC(18,4). The per-user spend cap (M2 / contract §3) is enforced
-- against the SAME monthly-windowed team_usage spend that get_team_members
-- reports — NOT the lifetime credit_team_members.total_spent counter — so the
-- cap and the displayed total agree. Idempotency is user-scoped (matches
-- deduct_credits / H16): a replay of the same metadata->>'idempotency_key'
-- returns the original transaction without double-charging the pool.
CREATE OR REPLACE FUNCTION public.deduct_team(
  p_team_id UUID,
  p_user_id UUID,
  p_amount NUMERIC,
  p_metadata JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_balance NUMERIC;
  v_spend_cap NUMERIC;
  v_is_member BOOLEAN;
  v_month_spent NUMERIC;
  v_tx_id UUID;
  v_idempotency_key TEXT;
  v_window TIMESTAMPTZ;
BEGIN
  IF p_amount IS NULL OR p_amount <= 0 THEN
    RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
  END IF;

  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    RETURN jsonb_build_object('error', 'unauthorized');
  END IF;

  v_idempotency_key := p_metadata->>'idempotency_key';

  -- Idempotency replay (user-scoped): return the original team_usage tx.
  IF v_idempotency_key IS NOT NULL THEN
    SELECT id INTO v_tx_id
    FROM public.credit_transactions
    WHERE user_id = p_user_id
      AND metadata->>'idempotency_key' = v_idempotency_key;
    IF FOUND THEN
      RETURN jsonb_build_object(
        'transaction_id', v_tx_id,
        'team_id', p_team_id,
        'user_id', p_user_id,
        'amount', -p_amount,
        'team_balance_after', (SELECT balance FROM public.credit_teams WHERE id = p_team_id),
        'idempotent', true
      );
    END IF;
  END IF;

  -- Check user is a member and get spend cap
  SELECT ctm.spend_cap, true INTO v_spend_cap, v_is_member
  FROM public.credit_team_members ctm
  WHERE ctm.team_id = p_team_id AND ctm.user_id = p_user_id;

  IF v_is_member IS NULL THEN
    RETURN jsonb_build_object('error', 'user_not_in_team');
  END IF;

  -- Enforce per-user spend cap against the current monthly team spend (UTC).
  IF v_spend_cap IS NOT NULL THEN
    v_window := date_trunc('month', now() AT TIME ZONE 'UTC');
    SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_month_spent
    FROM public.credit_transactions ct
    WHERE ct.user_id = p_user_id
      AND ct.type = 'team_usage'
      AND ct.metadata->>'team_id' = p_team_id::text
      AND ct.created_at >= v_window;

    IF (v_month_spent + p_amount) > v_spend_cap THEN
      RETURN jsonb_build_object('error', 'cap_reached', 'current_spend', v_month_spent, 'cap_limit', v_spend_cap);
    END IF;
  END IF;

  -- Get current team balance (locked to prevent concurrent deductions)
  SELECT balance INTO v_balance
  FROM public.credit_teams
  WHERE id = p_team_id
  FOR UPDATE;

  IF v_balance IS NULL THEN
    RETURN jsonb_build_object('error', 'team_not_found');
  END IF;

  IF v_balance < p_amount THEN
    RETURN jsonb_build_object('error', 'insufficient_credits');
  END IF;

  -- Deduct from team balance
  UPDATE public.credit_teams
  SET balance = balance - p_amount,
      updated_at = now()
  WHERE id = p_team_id
  RETURNING balance INTO v_balance;

  -- Keep the lifetime attribution counter in sync (informational only;
  -- cap enforcement uses the monthly window above).
  UPDATE public.credit_team_members
  SET total_spent = total_spent + p_amount
  WHERE team_id = p_team_id AND user_id = p_user_id;

  -- Log transaction; concurrent duplicate idempotency key -> return original.
  BEGIN
    INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
    VALUES (p_user_id, -p_amount, 'team_usage', p_metadata || jsonb_build_object('team_id', p_team_id))
    RETURNING id INTO v_tx_id;
  EXCEPTION WHEN unique_violation THEN
    SELECT id INTO v_tx_id
    FROM public.credit_transactions
    WHERE user_id = p_user_id
      AND metadata->>'idempotency_key' = v_idempotency_key;
    RETURN jsonb_build_object(
      'transaction_id', v_tx_id,
      'team_id', p_team_id,
      'user_id', p_user_id,
      'amount', -p_amount,
      'team_balance_after', (SELECT balance FROM public.credit_teams WHERE id = p_team_id),
      'idempotent', true
    );
  END;

  RETURN jsonb_build_object(
    'transaction_id', v_tx_id,
    'team_id', p_team_id,
    'user_id', p_user_id,
    'amount', -p_amount,
    'team_balance_after', v_balance,
    'idempotent', false
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.create_team FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_team_balance FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.add_team_member FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_team_members FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.deduct_team FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
