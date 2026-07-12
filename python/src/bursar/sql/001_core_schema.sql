-- bursar: core credit tables.
-- Idempotent — safe to run multiple times (CREATE IF NOT EXISTS).

-- Utility trigger function: sets updated_at on row modification.
-- Self-contained so the migration works even without Supabase's built-in.
CREATE OR REPLACE FUNCTION public.handle_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

-- Enum type for transaction categories. Extensible via ALTER TYPE ... ADD VALUE.
DO $$ BEGIN
    CREATE TYPE public.credit_tx_type AS ENUM (
        'purchase', 'subscription', 'signup_bonus', 'usage', 'refund', 'adjustment', 'team_usage'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- user_credits: current balance per user.
-- Money columns are NUMERIC(18,4): fractional credits, no integer truncation.
-- No CHECK (balance >= 0): overdraft billing mode requires the balance to be
-- able to go negative down to a per-policy floor enforced in RPC logic.
CREATE TABLE IF NOT EXISTS public.user_credits (
    user_id UUID PRIMARY KEY,
    balance NUMERIC(18,4) NOT NULL DEFAULT 0,
    lifetime_purchased NUMERIC(18,4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_user_credits_updated_at'
        AND tgrelid = 'public.user_credits'::regclass
    ) THEN
        CREATE TRIGGER set_user_credits_updated_at
            BEFORE UPDATE ON public.user_credits
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- credit_transactions: immutable ledger (append-only by convention).
-- amount is NUMERIC(18,4): fractional, signed (negative = debit).
CREATE TABLE IF NOT EXISTS public.credit_transactions (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.user_credits(user_id),
    amount NUMERIC(18,4) NOT NULL,
    type public.credit_tx_type NOT NULL,
    reference_type TEXT,
    reference_id UUID,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotency guarantee: unique on (user_id, idempotency_key) inside metadata JSONB.
-- User-scoped so the same key from two different users never collides.
--
-- NOTE: the key is NOT namespaced by operation type, so it is shared across
-- every RPC that accepts p_idempotency_key (credits_add, deduct_with_allowance,
-- settle_lease, grant_subscription_cycle's underlying add_credits call, ...).
-- If a caller ever reused the same key across two different operation types
-- for the same user (e.g. a credits_add and a deduct_with_allowance both
-- keyed "evt_123"), the second call hits this unique index as a genuine
-- collision and is misinterpreted as a replay of the first, returning the
-- wrong result rather than raising. Callers must mint idempotency keys that
-- are unique per (user, operation), e.g. by prefixing with the operation name
-- or using the upstream event id verbatim only when it is already
-- operation-specific (the common case: payment-provider webhook event ids).
-- User+type-scoped idempotency: the same key may be used across different
-- operation types (e.g. a purchase and a deduction) without collision.
DROP INDEX IF EXISTS public.idx_credit_transactions_idempotency_user;
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_transactions_idempotency_user
    ON public.credit_transactions (user_id, type, (metadata ->> 'idempotency_key'))
    WHERE metadata ->> 'idempotency_key' IS NOT NULL;

-- Index for user lookups (most recent first)
CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_id
    ON public.credit_transactions (user_id, created_at DESC);

-- credit_reservations: time-bounded holds against a user's balance. Backs
-- both the legacy reservation table shape and the lease lifecycle (see
-- 009_deduct_and_leases.sql for the lease-specific columns).
CREATE TABLE IF NOT EXISTS public.credit_reservations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES public.user_credits(user_id),
    amount NUMERIC(18,4) NOT NULL CHECK (amount > 0),
    operation_type TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '10 minutes'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for reservation cleanup queries
CREATE INDEX IF NOT EXISTS idx_credit_reservations_user_expires
    ON public.credit_reservations (user_id, expires_at);

-- RLS: users see own data. RPCs (SECURITY DEFINER) bypass this for admin ops.
ALTER TABLE public.user_credits ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Users can view own credits' AND tablename = 'user_credits') THEN
        CREATE POLICY "Users can view own credits" ON public.user_credits
            FOR SELECT USING (auth.uid() = user_id);
    END IF;
END;
$$;

ALTER TABLE public.credit_transactions ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Users can view own transactions' AND tablename = 'credit_transactions') THEN
        CREATE POLICY "Users can view own transactions" ON public.credit_transactions
            FOR SELECT USING (auth.uid() = user_id);
    END IF;
END;
$$;

ALTER TABLE public.credit_reservations ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Users can view own reservations' AND tablename = 'credit_reservations') THEN
        CREATE POLICY "Users can view own reservations" ON public.credit_reservations
            FOR SELECT USING (auth.uid() = user_id);
    END IF;
END;
$$;

-- Signup bonus trigger: give free credits (amount from active pricing config's
-- `signup_grant` field under `ledger` (new schema), falling back to 50) on user signup.
-- SECURITY DEFINER so the trigger function runs with table-owner privileges.
CREATE OR REPLACE FUNCTION public.grant_signup_bonus()
RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_bonus NUMERIC;
BEGIN
  SELECT COALESCE(
    (SELECT (config->'ledger'->>'signup_grant')::numeric FROM public.credit_pricing_config WHERE active = TRUE LIMIT 1),
    50
  ) INTO v_bonus;

  -- p_bucket = NULL (not a hardcoded 'gifted'): credits_add_internal then
  -- resolves the configured default bucket, or the synthetic 'default' bucket
  -- when no buckets are configured. A hardcoded 'gifted' would make it return
  -- bucket_not_found — silently swallowed by PERFORM — whenever that bucket
  -- isn't defined (e.g. every bucket-less install), dropping the bonus entirely.
  --
  -- Calls credits_add_internal (defined in 011_lazy_expiry.sql), NOT the
  -- guarded credits_add: a real Supabase GoTrue signup INSERT runs with no
  -- PostgREST/JWT request context, so auth.role() reads NULL here — the
  -- guarded credits_add would reject with {"error":"unauthorized"},
  -- silently dropping every signup bonus in production (masked in tests only
  -- because the test harness's auth.role() stub defaults to 'service_role').
  -- credits_add_internal has no such guard and is not independently
  -- reachable over the API (REVOKEd from PUBLIC/anon/authenticated).
  PERFORM public.credits_add_internal(NEW.id, v_bonus, 'signup_bonus', NULL, NULL);

  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'on_signup_credit_bonus'
    AND tgrelid = 'auth.users'::regclass
  ) THEN
    CREATE CONSTRAINT TRIGGER on_signup_credit_bonus
      AFTER INSERT ON auth.users
      DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW
      EXECUTE FUNCTION public.grant_signup_bonus();
  END IF;
END;
$$;
