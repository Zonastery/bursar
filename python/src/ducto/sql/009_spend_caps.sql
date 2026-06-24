-- ducto: per-user spend caps and rate limiting.
-- credit_spend_caps table and check_spend_cap RPC.

CREATE TABLE IF NOT EXISTS public.credit_spend_caps (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES public.user_credits(user_id),
  cap_type TEXT NOT NULL CHECK (cap_type IN ('daily', 'monthly')),
  model TEXT,
  cap_limit INTEGER NOT NULL,
  action TEXT NOT NULL DEFAULT 'deny' CHECK (action IN ('deny', 'warn', 'notify')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_spend_caps_unique
    ON public.credit_spend_caps (user_id, cap_type, COALESCE(model, ''));

ALTER TABLE public.credit_spend_caps ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only credit_spend_caps' AND tablename = 'credit_spend_caps') THEN
    CREATE POLICY "Server-only credit_spend_caps" ON public.credit_spend_caps USING (false);
  END IF;
END;
$$;

-- check_spend_cap: evaluate whether a pending deduction would exceed any cap.
-- Returns JSONB with capped, current_spend, cap_limit, action, model.
-- Checks deny caps first (hard block), then warn/notify (soft).
CREATE OR REPLACE FUNCTION public.check_spend_cap(
  p_user_id UUID,
  p_model TEXT DEFAULT NULL,
  p_amount INTEGER DEFAULT 0
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_cap RECORD;
  v_spend INTEGER;
  v_window TIMESTAMPTZ;
BEGIN
  IF auth.role() IS DISTINCT FROM 'service_role' THEN
    RETURN jsonb_build_object('capped', false, 'error', 'unauthorized');
  END IF;

  -- Check deny caps first (hard limit)
  FOR v_cap IN
    SELECT action, cap_type, model, cap_limit
    FROM public.credit_spend_caps
    WHERE user_id = p_user_id
      AND action = 'deny'
      AND (model IS NULL OR model = p_model)
    ORDER BY cap_limit ASC
  LOOP
    v_window := CASE v_cap.cap_type WHEN 'daily' THEN date_trunc('day', now()) ELSE date_trunc('month', now()) END;

    SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_spend
    FROM public.credit_transactions ct
    WHERE ct.user_id = p_user_id
      AND ct.type IN ('usage', 'team_usage')
      AND ct.amount < 0
      AND ct.created_at >= v_window
      AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);

    IF v_spend + p_amount > v_cap.cap_limit THEN
      RETURN jsonb_build_object('capped', true, 'current_spend', v_spend, 'cap_limit', v_cap.cap_limit, 'action', v_cap.action, 'model', v_cap.model);
    END IF;
  END LOOP;

  -- Check warn/notify caps (soft limit)
  FOR v_cap IN
    SELECT action, cap_type, model, cap_limit
    FROM public.credit_spend_caps
    WHERE user_id = p_user_id
      AND action IN ('warn', 'notify')
      AND (model IS NULL OR model = p_model)
    ORDER BY cap_limit ASC
  LOOP
    v_window := CASE v_cap.cap_type WHEN 'daily' THEN date_trunc('day', now()) ELSE date_trunc('month', now()) END;

    SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_spend
    FROM public.credit_transactions ct
    WHERE ct.user_id = p_user_id
      AND ct.type IN ('usage', 'team_usage')
      AND ct.amount < 0
      AND ct.created_at >= v_window
      AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);

    IF v_spend + p_amount > v_cap.cap_limit THEN
      RETURN jsonb_build_object('capped', false, 'current_spend', v_spend, 'cap_limit', v_cap.cap_limit, 'action', v_cap.action, 'model', v_cap.model);
    END IF;
  END LOOP;

  RETURN jsonb_build_object('capped', false, 'current_spend', 0, 'cap_limit', 0, 'action', null);
END;
$$;

REVOKE EXECUTE ON FUNCTION public.check_spend_cap FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
