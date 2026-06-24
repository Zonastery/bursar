-- ducto: team/shared balance pools.
-- credit_teams, credit_team_members tables, RPCs for team credit operations.

ALTER TYPE public.credit_tx_type ADD VALUE IF NOT EXISTS 'team_usage';

CREATE TABLE IF NOT EXISTS public.credit_teams (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  name TEXT NOT NULL,
  balance INTEGER NOT NULL DEFAULT 0,
  member_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.credit_team_members (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  team_id UUID NOT NULL REFERENCES public.credit_teams(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES public.user_credits(user_id),
  role TEXT NOT NULL DEFAULT 'member',
  spend_cap INTEGER,
  total_spent INTEGER NOT NULL DEFAULT 0,
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

-- create_team: create a team with optional initial balance.
CREATE OR REPLACE FUNCTION public.create_team(
  p_name TEXT,
  p_initial_balance INTEGER DEFAULT 0
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_team_id UUID;
BEGIN
  IF auth.role() DISTINCT FROM 'service_role' THEN
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
  p_spend_cap INTEGER DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  IF auth.role() DISTINCT FROM 'service_role' THEN
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
CREATE OR REPLACE FUNCTION public.get_team_members(p_team_id UUID)
RETURNS SETOF JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  RETURN QUERY
  SELECT jsonb_build_object(
    'user_id', tm.user_id,
    'role', tm.role,
    'spend_cap', tm.spend_cap,
    'total_spent', COALESCE(SUM(ct.amount) FILTER (WHERE ct.type = 'team_usage' AND ct.created_at >= date_trunc('month', NOW())), 0),
    'joined_at', tm.joined_at
  )
  FROM public.credit_team_members tm
  LEFT JOIN public.credit_transactions ct ON ct.user_id = tm.user_id AND ct.team_id = p_team_id
  WHERE tm.team_id = p_team_id
  GROUP BY tm.user_id, tm.role, tm.spend_cap, tm.joined_at
  ORDER BY tm.joined_at;
END;
$$;

-- deduct_team: deduct credits from team pool, attribute to user.
CREATE OR REPLACE FUNCTION public.deduct_team(
  p_team_id UUID,
  p_user_id UUID,
  p_amount INTEGER,
  p_metadata JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_balance INTEGER;
  v_spend_cap INTEGER;
  v_total_spent INTEGER;
BEGIN
  IF p_amount <= 0 THEN
    RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
  END IF;

  IF auth.role() DISTINCT FROM 'service_role' THEN
    RETURN jsonb_build_object('error', 'unauthorized');
  END IF;

  -- Check user is a member and get spend cap
  SELECT ct.spend_cap, ct.total_spent INTO v_spend_cap, v_total_spent
  FROM public.credit_team_members ct
  WHERE ct.team_id = p_team_id AND ct.user_id = p_user_id;

  IF v_spend_cap IS NULL AND v_total_spent IS NULL THEN
    RETURN jsonb_build_object('error', 'user_not_in_team');
  END IF;

  -- Enforce per-user spend cap
  IF v_spend_cap IS NOT NULL AND (v_total_spent + p_amount) > v_spend_cap THEN
    RETURN jsonb_build_object('error', 'spend_cap_exceeded');
  END IF;

  -- Get current team balance
  SELECT balance INTO v_balance
  FROM public.credit_teams
  WHERE id = p_team_id;

  IF v_balance IS NULL THEN
    RETURN jsonb_build_object('error', 'team_not_found');
  END IF;

  IF v_balance < p_amount THEN
    RETURN jsonb_build_object('error', 'insufficient_team_balance');
  END IF;

  -- Deduct from team balance
  UPDATE public.credit_teams
  SET balance = balance - p_amount,
      updated_at = now()
  WHERE id = p_team_id
  RETURNING balance INTO v_balance;

  -- Attribute to user
  UPDATE public.credit_team_members
  SET total_spent = total_spent + p_amount
  WHERE team_id = p_team_id AND user_id = p_user_id;

  -- Log transaction in credit_transactions
  INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
  VALUES (p_user_id, -p_amount, 'team_usage', p_metadata || jsonb_build_object('team_id', p_team_id));

  RETURN jsonb_build_object(
    'transaction_id', gen_random_uuid(),
    'team_id', p_team_id,
    'user_id', p_user_id,
    'amount', -p_amount,
    'team_balance_after', v_balance
  );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.create_team FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_team_balance FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.add_team_member FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_team_members FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.deduct_team FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
