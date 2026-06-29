-- ducto: core credit tables.
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
-- 'team_usage' is included here (not in 008) so it is committed long before any
-- later migration references it: Postgres forbids using a freshly-added enum
-- value in the same transaction that adds it (H5).
DO $$ BEGIN
    CREATE TYPE public.credit_tx_type AS ENUM (
        'purchase', 'subscription', 'signup_bonus', 'usage', 'refund', 'adjustment', 'team_usage'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- For installs created before 'team_usage' existed: add it idempotently.
-- This runs (and commits) in 001's own migration transaction, before 008/009
-- ever reference the value at runtime.
ALTER TYPE public.credit_tx_type ADD VALUE IF NOT EXISTS 'team_usage';

-- user_credits: current balance per user (non-negative enforced at DB level)
-- Money columns are NUMERIC(18,4): fractional credits, no integer truncation.
CREATE TABLE IF NOT EXISTS public.user_credits (
    user_id UUID PRIMARY KEY,
    balance NUMERIC(18,4) NOT NULL DEFAULT 0 CHECK (balance >= 0),
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

-- credit_transactions: immutable ledger (append-only by convention)
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
-- User-scoped so the same key from two different users never collides (H16).
-- NOTE: a legacy non-user-scoped index (idx_credit_transactions_idempotency) may
-- exist from older installs; 014_numeric_money.sql drops it in favour of this one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_transactions_idempotency_user
    ON public.credit_transactions (user_id, (metadata ->> 'idempotency_key'))
    WHERE metadata ->> 'idempotency_key' IS NOT NULL;

-- Index for user lookups (most recent first)
CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_id
    ON public.credit_transactions (user_id, created_at DESC);

-- Upgrade pre-existing tables that still have type TEXT to the new enum.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'credit_transactions'
        AND column_name = 'type' AND data_type = 'text'
    ) THEN
        ALTER TABLE public.credit_transactions
        ALTER COLUMN type TYPE public.credit_tx_type USING type::public.credit_tx_type;
    END IF;
END;
$$;

-- credit_reservations: optimistic concurrency guard for expensive operations
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

-- Signup bonus trigger: give 50 free credits on user signup.
-- SECURITY DEFINER so the trigger function runs with table-owner privileges.
CREATE OR REPLACE FUNCTION public.grant_signup_bonus()
RETURNS TRIGGER
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
  VALUES (NEW.id, 50, 0)
  ON CONFLICT (user_id) DO NOTHING;

  INSERT INTO public.credit_transactions (user_id, amount, type)
  VALUES (NEW.id, 50, 'signup_bonus');

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
