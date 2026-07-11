-- bursar: configurable credit buckets.
--
-- Adds priority-ordered credit "buckets" (e.g. gifted / allowance / purchased)
-- on top of the existing single-scalar `user_credits.balance`. The aggregate
-- balance stays authoritative (unchanged arithmetic everywhere); `user_credit_buckets`
-- tracks the aggregate split across buckets.
--
-- With no buckets configured, the store transparently creates a single synthetic
-- bucket "default" — zero behavioral change for bucket-less configs, one
-- deduction algorithm, never two parallel "legacy tiered" paths.

-- ── Schema ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.credit_buckets (
    bucket_key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    priority INTEGER NOT NULL,
    expires BOOLEAN NOT NULL DEFAULT false,
    ttl_days INTEGER,
    allow_overdraft BOOLEAN NOT NULL DEFAULT false,
    is_default BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- No CHECK (balance >= 0): allow_overdraft buckets can go negative, matching
-- the equivalent CHECK on user_credits.balance loosened for overdraft mode
-- (see 009_deduct_and_leases.sql).
CREATE TABLE IF NOT EXISTS public.user_credit_buckets (
    user_id UUID NOT NULL REFERENCES public.user_credits(user_id) ON DELETE CASCADE,
    bucket_key TEXT NOT NULL,
    balance NUMERIC(18,4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, bucket_key)
);
CREATE INDEX IF NOT EXISTS idx_user_credit_buckets_user ON public.user_credit_buckets (user_id);

-- At most one default bucket: credit_add / grant_signup_bonus resolves via
-- `is_default = true` when no explicit bucket_key is given.
-- At most one allow_overdraft bucket: the overdraft sink.

-- Triggers for updated_at (mirrors other tables)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_credit_buckets_updated_at'
          AND tgrelid = 'public.credit_buckets'::regclass
    ) THEN
        CREATE TRIGGER set_credit_buckets_updated_at
        BEFORE UPDATE ON public.credit_buckets
        FOR EACH ROW
        EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_user_credit_buckets_updated_at'
          AND tgrelid = 'public.user_credit_buckets'::regclass
    ) THEN
        CREATE TRIGGER set_user_credit_buckets_updated_at
        BEFORE UPDATE ON public.user_credit_buckets
        FOR EACH ROW
        EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- RLS: server-only access (managed through RPCs), same pattern as credit_plans.
ALTER TABLE public.credit_buckets ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only credit_buckets' AND tablename = 'credit_buckets') THEN
        CREATE POLICY "Server-only credit_buckets" ON public.credit_buckets USING (false);
    END IF;
END;
$$;

ALTER TABLE public.user_credit_buckets ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only user_credit_buckets' AND tablename = 'user_credit_buckets') THEN
        CREATE POLICY "Server-only user_credit_buckets" ON public.user_credit_buckets USING (false);
    END IF;
END;
$$;

-- sync_buckets_from_config: upsert bucket definitions from the pricing config
-- JSONB. Mirrors sync_plans_from_config's structure/pattern. Reads the buckets
-- section from the revamped config layout under `ledger.buckets`.
CREATE OR REPLACE FUNCTION public.sync_buckets_from_config(p_config JSONB)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_bucket_key TEXT;
    v_bucket_def JSONB;
BEGIN
    IF p_config #>> '{ledger,buckets}' IS NOT NULL AND jsonb_typeof(p_config #> '{ledger,buckets}') = 'object' THEN
        FOR v_bucket_key, v_bucket_def IN SELECT * FROM jsonb_each(p_config #> '{ledger,buckets}')
        LOOP
            INSERT INTO public.credit_buckets (
                bucket_key, label, priority, expires, ttl_days, allow_overdraft, is_default
            )
            VALUES (
                v_bucket_key,
                COALESCE(v_bucket_def->>'label', v_bucket_key),
                COALESCE((v_bucket_def->>'priority')::INTEGER, 0),
                COALESCE((v_bucket_def->>'expires')::BOOLEAN, COALESCE((v_bucket_def->>'ttlDays')::INTEGER, (v_bucket_def->>'ttl_days')::INTEGER) IS NOT NULL),
                COALESCE((v_bucket_def->>'ttlDays')::INTEGER, (v_bucket_def->>'ttl_days')::INTEGER),
                COALESCE(
                    (v_bucket_def->>'allowOverdraft')::BOOLEAN,
                    (v_bucket_def->>'allow_overdraft')::BOOLEAN,
                    false
                ),
                COALESCE(
                    (v_bucket_def->>'isDefaultBucket')::BOOLEAN,
                    (v_bucket_def->>'is_default_bucket')::BOOLEAN,
                    (v_bucket_def->>'default')::BOOLEAN,
                    false
                )
            )
            ON CONFLICT (bucket_key) DO UPDATE SET
                label = EXCLUDED.label,
                priority = EXCLUDED.priority,
                expires = EXCLUDED.expires,
                ttl_days = EXCLUDED.ttl_days,
                allow_overdraft = EXCLUDED.allow_overdraft,
                is_default = EXCLUDED.is_default,
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sync_buckets_from_config(JSONB) FROM PUBLIC, anon, authenticated;

-- ── Backfill (idempotent) ────────────────────────────────────────────────
-- Stamp all pre-existing grant/deduction rows with bucket = "default", and
-- snapshot every user's current balance into user_credit_buckets('default').
-- Guarded so a re-run of setup() (which re-executes every migration file on
-- every deploy) is always a no-op the second time.

UPDATE public.credit_transactions
SET metadata = COALESCE(metadata,'{}'::jsonb) || jsonb_build_object('bucket','default')
WHERE type IN ('purchase','subscription','signup_bonus','adjustment') AND amount >= 0 AND NOT (metadata ? 'bucket');

UPDATE public.credit_transactions
SET metadata = COALESCE(metadata,'{}'::jsonb) || jsonb_build_object('bucket_breakdown', jsonb_build_object('default', to_jsonb(ABS(amount))))
WHERE type IN ('usage','team_usage') AND amount < 0 AND NOT (metadata ? 'bucket_breakdown');

INSERT INTO public.user_credit_buckets (user_id, bucket_key, balance)
SELECT user_id, 'default', balance FROM public.user_credits
ON CONFLICT (user_id, bucket_key) DO NOTHING;

-- get_user_credit_buckets: per-bucket balance query. Synthesizes a single
-- "default" entry when no buckets are configured, so the API shape is uniform
-- whether or not buckets are configured.
CREATE OR REPLACE FUNCTION public.get_user_credit_buckets(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_total_balance NUMERIC;
    v_buckets JSONB;
    v_bucket_count INTEGER;
BEGIN
    SELECT COALESCE(balance, 0) INTO v_total_balance
    FROM public.user_credits
    WHERE user_id = p_user_id;

    SELECT COUNT(*) INTO v_bucket_count FROM public.credit_buckets;

    IF v_bucket_count = 0 THEN
        v_buckets := jsonb_build_array(
            jsonb_build_object(
                'bucket_key', 'default',
                'label', 'default',
                'priority', 0,
                'expires', false,
                'balance', COALESCE(v_total_balance, 0)
            )
        );
    ELSE
        SELECT COALESCE(jsonb_agg(
            jsonb_build_object(
                'bucket_key', cb.bucket_key,
                'label', cb.label,
                'priority', cb.priority,
                'expires', cb.expires,
                'balance', COALESCE(ucb.balance, 0)
            )
            ORDER BY cb.priority ASC, cb.bucket_key ASC
        ), '[]'::jsonb) INTO v_buckets
        FROM public.credit_buckets cb
        LEFT JOIN public.user_credit_buckets ucb
            ON ucb.bucket_key = cb.bucket_key AND ucb.user_id = p_user_id;
    END IF;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'buckets', v_buckets,
        'total_balance', COALESCE(v_total_balance, 0)
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_user_credit_buckets(UUID) FROM PUBLIC, anon, authenticated;

NOTIFY pgrst, 'reload schema';
