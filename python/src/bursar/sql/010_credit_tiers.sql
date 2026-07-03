-- bursar: configurable credit tiers.
--
-- Adds priority-ordered credit "tiers" (e.g. gifted / allowance / purchased)
-- on top of the existing single-scalar `user_credits.balance`. The aggregate
-- balance stays the authoritative cache (unchanged arithmetic everywhere);
-- `user_credit_tiers` tracks how that aggregate is split across tiers.
--
-- When no tiers are configured, every store transparently uses a single
-- synthetic tier "default" — zero behavioral change for tier-less configs,
-- one deduction algorithm, never two parallel "legacy vs tiered" code paths.

-- ── Schema ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.credit_tiers (
    tier_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL,
    expires BOOLEAN NOT NULL DEFAULT false,
    default_ttl_days INTEGER,
    allow_overdraft BOOLEAN NOT NULL DEFAULT false,
    is_default BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- No CHECK (balance >= 0): the allow_overdraft tier can go negative, exactly
-- like the equivalent CHECK on user_credits.balance is loosened for overdraft
-- mode (see 009_deduct_and_leases.sql).
CREATE TABLE IF NOT EXISTS public.user_credit_tiers (
    user_id UUID NOT NULL REFERENCES public.user_credits(user_id) ON DELETE CASCADE,
    tier_key TEXT NOT NULL,
    balance NUMERIC(18,4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, tier_key)
);
CREATE INDEX IF NOT EXISTS idx_user_credit_tiers_user ON public.user_credit_tiers (user_id);

-- updated_at triggers (reuse the shared handle_updated_at() from 001_core_schema.sql).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_credit_tiers_updated_at'
        AND tgrelid = 'public.credit_tiers'::regclass
    ) THEN
        CREATE TRIGGER set_credit_tiers_updated_at
            BEFORE UPDATE ON public.credit_tiers
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'set_user_credit_tiers_updated_at'
        AND tgrelid = 'public.user_credit_tiers'::regclass
    ) THEN
        CREATE TRIGGER set_user_credit_tiers_updated_at
            BEFORE UPDATE ON public.user_credit_tiers
            FOR EACH ROW
            EXECUTE FUNCTION public.handle_updated_at();
    END IF;
END;
$$;

-- RLS: server-only access (managed through RPCs), same pattern as credit_plans.
ALTER TABLE public.credit_tiers ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only credit_tiers' AND tablename = 'credit_tiers') THEN
        CREATE POLICY "Server-only credit_tiers" ON public.credit_tiers USING (false);
    END IF;
END;
$$;

ALTER TABLE public.user_credit_tiers ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname = 'Server-only user_credit_tiers' AND tablename = 'user_credit_tiers') THEN
        CREATE POLICY "Server-only user_credit_tiers" ON public.user_credit_tiers USING (false);
    END IF;
END;
$$;

-- sync_tiers_from_config: upsert tier definitions from the pricing config
-- JSONB. Mirrors sync_plans_from_config's structure/pattern. Accepts both
-- snake_case and camelCase keys for the same defensive reason those
-- functions do (the JSONB config is normally normalised to snake_case before
-- it lands here, but this is cheap insurance against that invariant drifting).
CREATE OR REPLACE FUNCTION public.sync_tiers_from_config(p_config JSONB)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_tier_key TEXT;
    v_tier_def JSONB;
BEGIN
    IF p_config ? 'tiers' AND jsonb_typeof(p_config->'tiers') = 'object' THEN
        FOR v_tier_key, v_tier_def IN SELECT * FROM jsonb_each(p_config->'tiers')
        LOOP
            INSERT INTO public.credit_tiers (
                tier_key, name, priority, expires, default_ttl_days, allow_overdraft, is_default
            )
            VALUES (
                v_tier_key,
                v_tier_def->>'name',
                COALESCE((v_tier_def->>'priority')::INTEGER, 0),
                COALESCE((v_tier_def->>'expires')::BOOLEAN, false),
                COALESCE((v_tier_def->>'default_ttl_days')::INTEGER, (v_tier_def->>'defaultTtlDays')::INTEGER),
                COALESCE((v_tier_def->>'allow_overdraft')::BOOLEAN, (v_tier_def->>'allowOverdraft')::BOOLEAN, false),
                COALESCE((v_tier_def->>'is_default')::BOOLEAN, (v_tier_def->>'isDefault')::BOOLEAN, false)
            )
            ON CONFLICT (tier_key) DO UPDATE SET
                name = EXCLUDED.name,
                priority = EXCLUDED.priority,
                expires = EXCLUDED.expires,
                default_ttl_days = EXCLUDED.default_ttl_days,
                allow_overdraft = EXCLUDED.allow_overdraft,
                is_default = EXCLUDED.is_default,
                updated_at = now();
        END LOOP;
    END IF;
END;
$$;

REVOKE EXECUTE ON FUNCTION public.sync_tiers_from_config(JSONB) FROM anon, authenticated;

-- ── Backfill (idempotent) ────────────────────────────────────────────────
-- Stamp all pre-existing grant/deduction rows with tier = "default", and
-- snapshot every user's current balance into user_credit_tiers('default').
-- Guarded so a re-run of setup() (which re-executes every migration file on
-- every deploy) is always a no-op the second time.

UPDATE public.credit_transactions
SET metadata = COALESCE(metadata,'{}'::jsonb) || jsonb_build_object('tier','default')
WHERE type IN ('purchase','subscription','signup_bonus','adjustment') AND amount >= 0 AND NOT (metadata ? 'tier');

UPDATE public.credit_transactions
SET metadata = COALESCE(metadata,'{}'::jsonb) || jsonb_build_object('tier_breakdown', jsonb_build_object('default', to_jsonb(ABS(amount))))
WHERE type IN ('usage','team_usage') AND amount < 0 AND NOT (metadata ? 'tier_breakdown');

INSERT INTO public.user_credit_tiers (user_id, tier_key, balance)
SELECT user_id, 'default', balance FROM public.user_credits
ON CONFLICT (user_id, tier_key) DO NOTHING;

-- get_user_credit_tiers: per-tier balance query. Synthesizes a single
-- "default" entry when no tiers are configured, so the API shape is uniform
-- whether or not tiers are configured.
CREATE OR REPLACE FUNCTION public.get_user_credit_tiers(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_total_balance NUMERIC;
    v_tiers JSONB;
    v_tier_count INTEGER;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT COALESCE(balance, 0) INTO v_total_balance
    FROM public.user_credits
    WHERE user_id = p_user_id;

    SELECT COUNT(*) INTO v_tier_count FROM public.credit_tiers;

    IF v_tier_count = 0 THEN
        v_tiers := jsonb_build_array(
            jsonb_build_object(
                'tier_key', 'default',
                'name', 'default',
                'priority', 0,
                'expires', false,
                'balance', COALESCE(v_total_balance, 0)
            )
        );
    ELSE
        SELECT COALESCE(jsonb_agg(
            jsonb_build_object(
                'tier_key', ct.tier_key,
                'name', ct.name,
                'priority', ct.priority,
                'expires', ct.expires,
                'balance', COALESCE(uct.balance, 0)
            )
            ORDER BY ct.priority ASC, ct.tier_key ASC
        ), '[]'::jsonb) INTO v_tiers
        FROM public.credit_tiers ct
        LEFT JOIN public.user_credit_tiers uct
            ON uct.tier_key = ct.tier_key AND uct.user_id = p_user_id;
    END IF;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'tiers', v_tiers,
        'total_balance', COALESCE(v_total_balance, 0)
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.get_user_credit_tiers(UUID) FROM anon, authenticated;

NOTIFY pgrst, 'reload schema';
