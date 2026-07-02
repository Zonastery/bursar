-- ducto: 023 — configurable credit tiers.
--
-- Adds priority-ordered credit "tiers" (e.g. gifted / allowance / purchased)
-- on top of the existing single-scalar `user_credits.balance`. The aggregate
-- balance stays the authoritative cache (unchanged arithmetic everywhere);
-- a new `user_credit_tiers` table tracks how that aggregate is split across
-- tiers, and the debit/settle/refund/expiry RPCs gain a priority-ordered walk
-- that decides *which* tier balance(s) actually move for a given operation.
--
-- Guiding decision (see plan): when no tiers are configured, every store
-- transparently uses a single synthetic tier "default" — zero behavioral
-- change for pre-existing configs, one deduction algorithm, never two
-- parallel "legacy vs tiered" code paths.
--
-- Summary of changes:
--   1. New tables `credit_tiers` (config) and `user_credit_tiers` (per-user
--      per-tier balance), RLS-disabled-to-clients (server-only), same
--      deny-all policy pattern as `credit_plans`.
--   2. `sync_tiers_from_config(p_config)` — upserts `credit_tiers` from the
--      pricing config JSONB, mirroring `sync_plans_from_config`.
--   3. `set_active_pricing_config` patched to also call
--      `sync_tiers_from_config`.
--   4. Idempotent backfill: stamp pre-existing grant/deduction rows with
--      `tier`/`tier_breakdown` = "default", and snapshot every user's
--      current `user_credits.balance` into `user_credit_tiers('default')`.
--   5. `credits_add` gains `p_tier TEXT DEFAULT NULL` (resolves against
--      configured tiers, reconciles `expires_at` against the resolved
--      tier's `expires`/`default_ttl_days`), upserts `user_credit_tiers`.
--   6. `deduct_with_allowance` / `settle_lease`: after the existing
--      (unchanged) floor/clamp logic, walk tiers priority-ascending and
--      decrement per-tier balances before the (unchanged) aggregate
--      `UPDATE user_credits SET balance = balance - v_net`. Idempotent
--      replay echoes the ORIGINAL row's `tier_breakdown`, never recomputes.
--   7. `refund_credits`: LIFO tier restoration (reverse priority order),
--      deriving each tier's already-refunded amount from the sum of all
--      prior refunds' own `tier_breakdown` (never a running counter).
--   8. `expire_credits`: re-scoped from per-user_id to per-(user_id, tier)
--      grouping; clamps against the tier's own balance; decrements both the
--      tier balance and the aggregate; returns `expired_by_tier`.
--   9. New RPC `get_user_credit_tiers(p_user_id)`.

-- ── 1. Schema ────────────────────────────────────────────────────────────

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
-- like 016_lease_lifecycle.sql already loosened the equivalent CHECK on
-- user_credits.balance for overdraft mode.
CREATE TABLE IF NOT EXISTS public.user_credit_tiers (
    user_id UUID NOT NULL REFERENCES public.user_credits(user_id) ON DELETE CASCADE,
    tier_key TEXT NOT NULL,
    balance NUMERIC(18,4) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, tier_key)
);
CREATE INDEX IF NOT EXISTS idx_user_credit_tiers_user ON public.user_credit_tiers (user_id);

-- updated_at triggers (reuse the shared handle_updated_at() from 001).
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

-- ── 2. sync_tiers_from_config: upsert tier definitions ──────────────────
-- Mirrors sync_plans_from_config's structure/pattern (011/016/022). Accepts
-- both snake_case and camelCase keys for the same defensive reason those
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

-- ── 3. Patch set_active_pricing_config: also sync tiers ─────────────────
-- Identical body to the current canonical definition (011_feature_entitlements.sql,
-- the only later CREATE OR REPLACE of this function), plus one new PERFORM line
-- right after the existing sync_plans_from_config call. Same exact signature.

CREATE OR REPLACE FUNCTION public.set_active_pricing_config(
    p_config JSONB,
    p_label TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_new_id UUID;
    v_next_version INTEGER;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Serialize concurrent publishers so version assignment can't race (M14).
    PERFORM pg_advisory_xact_lock(hashtext('ducto_pricing_version'));

    SELECT COALESCE(MAX(version), 0) + 1 INTO v_next_version
    FROM public.credit_pricing_config;

    -- Deactivate all existing active configs
    UPDATE public.credit_pricing_config SET active = false WHERE active = true;

    -- Insert new active config
    INSERT INTO public.credit_pricing_config (config, active, version, label)
    VALUES (p_config, true, v_next_version, p_label)
    RETURNING id INTO v_new_id;

    -- Sync plan definitions into credit_plans table
    PERFORM public.sync_plans_from_config(p_config);

    -- Sync tier definitions into credit_tiers table (023)
    PERFORM public.sync_tiers_from_config(p_config);

    RETURN jsonb_build_object(
        'id', v_new_id,
        'version', v_next_version,
        'active', true
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.set_active_pricing_config(JSONB, TEXT) FROM anon, authenticated;

-- ── 4. Backfill (idempotent) ─────────────────────────────────────────────
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

-- ── 5. credits_add: resolve/validate tier, reconcile expires_at ─────────
-- Adding a parameter changes the function's identity (extra type in the
-- arg list), so the pre-existing 4-arg overload must be dropped first —
-- same idiom 022 uses for deduct_with_allowance/settle_lease/create_lease.
DROP FUNCTION IF EXISTS public.credits_add(UUID, NUMERIC, public.credit_tx_type, JSONB);

-- Tier resolution/expiry-reconciliation only engages once tiers are actually
-- configured (credit_tiers has rows) — this is what keeps a no-tiers config
-- byte-for-byte behavior-identical to pre-023 (existing tests call
-- add_credits(..., expires_at=...) with no tiers configured at all and must
-- keep working unmodified; see plan §1 "one code path, zero behavioral
-- change when tiers are absent"). When no tiers are configured, p_tier must
-- be NULL/'default' and expires_at passes through exactly as it does today.
CREATE OR REPLACE FUNCTION public.credits_add(
    p_user_id UUID,
    p_amount NUMERIC,
    p_type public.credit_tx_type DEFAULT 'adjustment',
    p_metadata JSONB DEFAULT NULL,
    p_tier TEXT DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_new_balance NUMERIC;
    v_lifetime NUMERIC;
    v_transaction_id UUID;
    v_tiers_configured BOOLEAN;
    v_resolved_tier TEXT;
    v_tier_expires BOOLEAN;
    v_tier_ttl_days INTEGER;
    v_has_expires_at BOOLEAN;
    v_computed_expires_at TIMESTAMPTZ;
    v_metadata JSONB;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Reject non-finite amounts (NaN / +-Infinity) outright.
    IF p_amount IS NULL OR NOT (p_amount = p_amount) OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- Purchases (and other credit grants) must be strictly positive.
    -- Negative/zero amounts are only allowed via an explicit 'adjustment'.
    IF p_type <> 'adjustment' AND p_amount <= 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    -- ── Tier resolution (023) ────────────────────────────────────────────
    v_tiers_configured := EXISTS (SELECT 1 FROM public.credit_tiers);

    IF NOT v_tiers_configured THEN
        IF p_tier IS NOT NULL AND p_tier <> 'default' THEN
            RETURN jsonb_build_object('error', 'tier_not_found', 'tier', p_tier);
        END IF;
        v_resolved_tier := 'default';
    ELSIF p_tier IS NOT NULL THEN
        SELECT tier_key, expires, default_ttl_days
        INTO v_resolved_tier, v_tier_expires, v_tier_ttl_days
        FROM public.credit_tiers
        WHERE tier_key = p_tier;

        IF NOT FOUND THEN
            RETURN jsonb_build_object('error', 'tier_not_found', 'tier', p_tier);
        END IF;
    ELSE
        SELECT tier_key, expires, default_ttl_days
        INTO v_resolved_tier, v_tier_expires, v_tier_ttl_days
        FROM public.credit_tiers
        WHERE is_default = true
        LIMIT 1;

        IF NOT FOUND THEN
            RETURN jsonb_build_object('error', 'tier_required');
        END IF;
    END IF;

    v_metadata := COALESCE(p_metadata, '{}'::jsonb);

    -- ── expires_at reconciliation against the resolved tier ─────────────
    -- Only applies when tiers are configured (v_tier_expires is NULL, i.e.
    -- falsy, in the no-tiers-configured branch above, so this whole block
    -- is skipped there).
    IF v_tiers_configured THEN
        v_has_expires_at := v_metadata ? 'expires_at';

        IF NOT COALESCE(v_tier_expires, false) THEN
            IF v_has_expires_at THEN
                RETURN jsonb_build_object('error', 'tier_does_not_expire', 'tier', v_resolved_tier);
            END IF;
        ELSE
            IF NOT v_has_expires_at THEN
                IF v_tier_ttl_days IS NULL THEN
                    RETURN jsonb_build_object('error', 'expires_at_required', 'tier', v_resolved_tier);
                END IF;
                v_computed_expires_at := now() + (v_tier_ttl_days || ' days')::interval;
                v_metadata := v_metadata || jsonb_build_object('expires_at', to_jsonb(v_computed_expires_at));
            END IF;
        END IF;
    END IF;

    v_metadata := v_metadata || jsonb_build_object('tier', v_resolved_tier);

    INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
    VALUES (p_user_id, p_amount, CASE WHEN p_type = 'purchase' THEN p_amount ELSE 0 END)
    ON CONFLICT (user_id) DO UPDATE SET
        balance = public.user_credits.balance + p_amount,
        lifetime_purchased = CASE WHEN p_type = 'purchase'
            THEN public.user_credits.lifetime_purchased + p_amount
            ELSE public.user_credits.lifetime_purchased
        END,
        updated_at = now()
    RETURNING balance, lifetime_purchased INTO v_new_balance, v_lifetime;

    -- Per-tier balance (023): lazily created on first touch.
    INSERT INTO public.user_credit_tiers (user_id, tier_key, balance)
    VALUES (p_user_id, v_resolved_tier, p_amount)
    ON CONFLICT (user_id, tier_key) DO UPDATE SET
        balance = public.user_credit_tiers.balance + p_amount,
        updated_at = now();

    INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
    VALUES (p_user_id, p_amount, p_type, v_metadata)
    RETURNING id INTO v_transaction_id;

    RETURN jsonb_build_object(
        'id', v_transaction_id,
        'user_id', p_user_id,
        'amount', p_amount,
        'new_balance', v_new_balance,
        'lifetime_purchased', v_lifetime,
        'tier', v_resolved_tier
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.credits_add(UUID, NUMERIC, public.credit_tx_type, JSONB, TEXT) FROM anon, authenticated;

-- ── 6. deduct_with_allowance: tier-aware commit step ─────────────────────
-- Steps through the floor check are byte-for-byte identical to the current
-- (022) body. Only addition: after the floor check and before the (still
-- unchanged) aggregate UPDATE, walk tiers priority-ascending and decrement
-- the per-tier balances that actually funded this debit. Idempotent replay
-- (both the early p_idempotency_key check AND the unique_violation handler)
-- echoes back the ORIGINAL row's stored tier_breakdown — never recomputed.
CREATE OR REPLACE FUNCTION public.deduct_with_allowance(
    p_user_id          UUID,
    p_amount           NUMERIC,
    p_idempotency_key  TEXT DEFAULT NULL,
    p_min_balance      NUMERIC DEFAULT 0,
    p_model            TEXT DEFAULT NULL,
    p_metadata         JSONB DEFAULT '{}'::jsonb,
    p_skip_allowance   BOOLEAN DEFAULT FALSE,
    p_period_start     DATE DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance              NUMERIC;
    v_plan_id              UUID;
    v_free_allowance       NUMERIC;
    v_period_start         DATE;
    v_used                 NUMERIC;
    v_remaining            NUMERIC;
    v_consume              NUMERIC := 0;
    v_net                  NUMERIC;
    v_cap                  RECORD;
    v_cap_spend            NUMERIC;
    v_cap_window           TIMESTAMPTZ;
    v_cap_warning          TEXT := NULL;
    v_new_balance          NUMERIC;
    v_transaction_id       UUID;
    v_metadata             JSONB;
    v_existing_id          UUID;
    v_existing_amt         NUMERIC;
    v_existing_cons        NUMERIC;
    v_existing_bal_after   NUMERIC;
    v_existing_tier_bd     JSONB;
    -- Tier walk (023)
    v_tier_breakdown       JSONB := '{}'::jsonb;
    v_tier_remaining       NUMERIC;
    v_walk                 RECORD;
    v_tier_balance         NUMERIC;
    v_take                 NUMERIC;
    v_sink_tier            TEXT;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF p_amount IS NULL
       OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric
       OR p_amount = '-Infinity'::numeric
       OR p_amount < 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    SELECT balance, plan_id INTO v_balance, v_plan_id
    FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0) ON CONFLICT (user_id) DO NOTHING;
        SELECT balance, plan_id INTO v_balance, v_plan_id
        FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    END IF;

    -- (2) Idempotency replay: return the original balance_after/tier_breakdown
    --     from tx metadata rather than the (wrong) current balance (Fix 8).
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id,
               ABS(amount),
               COALESCE((metadata->>'allowance_consumed')::numeric, 0),
               COALESCE((metadata->>'balance_after')::numeric, v_balance),
               COALESCE(metadata->'tier_breakdown', '{}'::jsonb)
        INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after, v_existing_tier_bd
        FROM public.credit_transactions
        WHERE user_id = p_user_id
          AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id,
                'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons,
                'balance_after', v_existing_bal_after,
                'idempotent', true,
                'cap_warning', NULL,
                'tier_breakdown', v_existing_tier_bd
            );
        END IF;
    END IF;

    -- (3) Allowance: skipped for fixed-cost jobs (p_skip_allowance = TRUE, Fix 7).
    -- v_period_start (WS9): explicit p_period_start (rolling_30d/anniversary,
    -- resolved by the manager) else the current UTC calendar month (unchanged).
    IF NOT p_skip_allowance AND v_plan_id IS NOT NULL THEN
        SELECT free_allowance INTO v_free_allowance
        FROM public.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM public.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_remaining := GREATEST(COALESCE(v_free_allowance, 0) - COALESCE(v_used, 0), 0);
        v_consume   := LEAST(v_remaining, p_amount);
    END IF;

    v_net := p_amount - v_consume;

    BEGIN
        IF v_consume > 0 THEN
            INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
            VALUES (p_user_id, v_plan_id, v_period_start, v_consume)
            ON CONFLICT (user_id, plan_id, billing_period) DO UPDATE SET
                usage = public.credit_usage_window.usage + v_consume,
                updated_at = now();
        END IF;

        FOR v_cap IN
            SELECT action, cap_type, model, cap_limit
            FROM public.credit_spend_caps
            WHERE user_id = p_user_id AND (model IS NULL OR model = p_model)
            ORDER BY (action = 'deny') DESC, cap_limit ASC
        LOOP
            v_cap_window := CASE v_cap.cap_type
                WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
                ELSE date_trunc('month', now() AT TIME ZONE 'UTC')
            END;
            SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_cap_spend
            FROM public.credit_transactions ct
            WHERE ct.user_id = p_user_id AND ct.type IN ('usage', 'team_usage') AND ct.amount < 0
              AND ct.created_at >= v_cap_window
              AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);
            IF v_cap_spend + v_net > v_cap.cap_limit THEN
                IF v_cap.action = 'deny' THEN
                    RAISE EXCEPTION 'ducto_cap_reached' USING ERRCODE = 'DU001';
                ELSE
                    IF v_cap_warning IS NULL THEN v_cap_warning := v_cap.action; END IF;
                END IF;
            END IF;
        END LOOP;

        IF v_balance - v_net < p_min_balance THEN
            RAISE EXCEPTION 'ducto_insufficient_credits' USING ERRCODE = 'DU002';
        END IF;

        -- ── Tier walk (023): decide WHICH tier balance(s) fund this debit.
        -- The aggregate UPDATE below is unchanged and remains authoritative;
        -- this only decides how user_credit_tiers is split. Walk order:
        -- configured tiers by priority ASC, then any tier_keys this user
        -- holds balance under that are no longer in credit_tiers (config
        -- drift safety net), appended last.
        v_tier_remaining := v_net;

        FOR v_walk IN
            SELECT tier_key, priority, 0 AS grp FROM public.credit_tiers
            UNION ALL
            SELECT uct.tier_key, 0, 1 AS grp
            FROM public.user_credit_tiers uct
            WHERE uct.user_id = p_user_id
              AND NOT EXISTS (SELECT 1 FROM public.credit_tiers ct WHERE ct.tier_key = uct.tier_key)
            ORDER BY grp ASC, priority ASC, tier_key ASC
        LOOP
            EXIT WHEN v_tier_remaining <= 0;

            SELECT balance INTO v_tier_balance
            FROM public.user_credit_tiers
            WHERE user_id = p_user_id AND tier_key = v_walk.tier_key
            FOR UPDATE;
            v_tier_balance := COALESCE(v_tier_balance, 0);

            v_take := LEAST(v_tier_balance, v_tier_remaining);
            IF v_take > 0 THEN
                UPDATE public.user_credit_tiers
                SET balance = balance - v_take, updated_at = now()
                WHERE user_id = p_user_id AND tier_key = v_walk.tier_key;

                v_tier_breakdown := v_tier_breakdown || jsonb_build_object(v_walk.tier_key, v_take);
                v_tier_remaining := v_tier_remaining - v_take;
            END IF;
        END LOOP;

        -- Overdraft sink: only reachable when a negative floor sanctioned
        -- going negative (the floor check above already guarantees
        -- v_balance - v_net >= p_min_balance, so configured-tier balances,
        -- which sum to v_balance, always fully cover v_net in strict mode).
        IF v_tier_remaining > 0 THEN
            SELECT tier_key INTO v_sink_tier FROM public.credit_tiers WHERE allow_overdraft = true LIMIT 1;
            IF v_sink_tier IS NULL THEN
                SELECT tier_key INTO v_sink_tier FROM public.credit_tiers ORDER BY priority DESC, tier_key DESC LIMIT 1;
            END IF;
            IF v_sink_tier IS NULL THEN
                v_sink_tier := 'default';
            END IF;

            INSERT INTO public.user_credit_tiers (user_id, tier_key, balance)
            VALUES (p_user_id, v_sink_tier, -v_tier_remaining)
            ON CONFLICT (user_id, tier_key) DO UPDATE SET
                balance = public.user_credit_tiers.balance - v_tier_remaining,
                updated_at = now();

            v_tier_breakdown := v_tier_breakdown || jsonb_build_object(
                v_sink_tier, COALESCE((v_tier_breakdown->>v_sink_tier)::numeric, 0) + v_tier_remaining
            );
            v_tier_remaining := 0;
        END IF;

        UPDATE public.user_credits
        SET balance = balance - v_net, updated_at = now()
        WHERE user_id = p_user_id
        RETURNING balance INTO v_new_balance;

        -- Store balance_after/tier_breakdown in metadata for correct
        -- idempotent replay (Fix 8 / 023).
        v_metadata := COALESCE(p_metadata, '{}'::jsonb)
            || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model))
            || jsonb_build_object('allowance_consumed', v_consume, 'balance_after', v_new_balance, 'tier_breakdown', v_tier_breakdown);

        INSERT INTO public.credit_transactions (user_id, amount, type, reference_type, metadata)
        VALUES (p_user_id, -v_net, 'usage', p_metadata->>'reference_type', v_metadata)
        RETURNING id INTO v_transaction_id;

    EXCEPTION
        WHEN SQLSTATE 'DU001' THEN
            RETURN jsonb_build_object('error', 'cap_reached', 'action', 'deny');
        WHEN SQLSTATE 'DU002' THEN
            RETURN jsonb_build_object('error', 'insufficient_credits');
        WHEN unique_violation THEN
            SELECT id,
                   ABS(amount),
                   COALESCE((metadata->>'allowance_consumed')::numeric, 0),
                   COALESCE((metadata->>'balance_after')::numeric, v_balance),
                   COALESCE(metadata->'tier_breakdown', '{}'::jsonb)
            INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_bal_after, v_existing_tier_bd
            FROM public.credit_transactions
            WHERE user_id = p_user_id AND metadata->>'idempotency_key' = p_idempotency_key
            LIMIT 1;
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id, 'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons, 'balance_after', v_existing_bal_after,
                'idempotent', true, 'cap_warning', NULL, 'tier_breakdown', v_existing_tier_bd
            );
    END;

    RETURN jsonb_build_object(
        'transaction_id', v_transaction_id,
        'amount', v_net,
        'allowance_consumed', v_consume,
        'balance_after', v_new_balance,
        'idempotent', false,
        'cap_warning', v_cap_warning,
        'tier_breakdown', v_tier_breakdown
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.deduct_with_allowance(UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN, DATE) FROM anon, authenticated;

-- ── 7. settle_lease: identical tier walk on the already-floor-clamped v_net ──
CREATE OR REPLACE FUNCTION public.settle_lease(
    p_user_id         UUID,
    p_lease_id        UUID,
    p_amount          NUMERIC,
    p_idempotency_key TEXT DEFAULT NULL,
    p_min_balance     NUMERIC DEFAULT 0,
    p_model           TEXT DEFAULT NULL,
    p_metadata        JSONB DEFAULT '{}'::jsonb,
    p_skip_allowance  BOOLEAN DEFAULT FALSE,
    p_period_start    DATE DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance        NUMERIC;
    v_plan_id        UUID;
    v_status         TEXT;
    v_settle_tx      UUID;
    v_lease_expires  TIMESTAMPTZ;
    v_billing_mode   TEXT;
    v_overdraft_floor NUMERIC;
    v_settle_floor   NUMERIC;
    v_max_debit      NUMERIC;
    v_free_allowance NUMERIC;
    v_period_start   DATE;
    v_used           NUMERIC;
    v_consume        NUMERIC := 0;
    v_net            NUMERIC;
    v_cap            RECORD;
    v_cap_window     TIMESTAMPTZ;
    v_cap_spend      NUMERIC;
    v_cap_warning    TEXT := NULL;
    v_new_balance    NUMERIC;
    v_tx_id          UUID;
    v_metadata       JSONB;
    v_existing_id    UUID;
    v_existing_amt   NUMERIC;
    v_existing_cons  NUMERIC;
    v_existing_tier_bd JSONB;
    -- Tier walk (023)
    v_tier_breakdown JSONB := '{}'::jsonb;
    v_tier_remaining NUMERIC;
    v_walk           RECORD;
    v_tier_balance   NUMERIC;
    v_take           NUMERIC;
    v_sink_tier      TEXT;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    IF p_amount IS NULL OR NOT (p_amount = p_amount)
       OR p_amount = 'Infinity'::numeric OR p_amount = '-Infinity'::numeric OR p_amount < 0 THEN
        RETURN jsonb_build_object('error', 'invalid_amount', 'amount', p_amount);
    END IF;

    SELECT balance, plan_id INTO v_balance, v_plan_id
    FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (p_user_id, 0, 0) ON CONFLICT (user_id) DO NOTHING;
        SELECT balance, plan_id INTO v_balance, v_plan_id
        FROM public.user_credits WHERE user_id = p_user_id FOR UPDATE;
    END IF;

    -- Idempotency replay (user-scoped).
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0),
               COALESCE(metadata->'tier_breakdown', '{}'::jsonb)
        INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_tier_bd
        FROM public.credit_transactions
        WHERE user_id = p_user_id AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;
        IF FOUND THEN
            RETURN jsonb_build_object(
                'transaction_id', v_existing_id, 'amount', v_existing_amt,
                'allowance_consumed', v_existing_cons, 'balance_after', v_balance,
                'idempotent', true, 'cap_warning', NULL, 'tier_breakdown', v_existing_tier_bd
            );
        END IF;
    END IF;

    -- Lock + validate the lease state; also read billing policy columns (C1).
    SELECT status, settle_tx_id, expires_at, billing_mode, overdraft_floor
    INTO v_status, v_settle_tx, v_lease_expires, v_billing_mode, v_overdraft_floor
    FROM public.credit_reservations
    WHERE id = p_lease_id AND user_id = p_user_id FOR UPDATE;

    IF NOT FOUND OR v_status = 'released' THEN
        RETURN jsonb_build_object('error', 'lease_not_found', 'balance_after', v_balance);
    END IF;
    IF v_status = 'settled' THEN
        IF v_settle_tx IS NOT NULL THEN
            SELECT id, ABS(amount), COALESCE((metadata->>'allowance_consumed')::numeric, 0),
                   COALESCE(metadata->'tier_breakdown', '{}'::jsonb)
            INTO v_existing_id, v_existing_amt, v_existing_cons, v_existing_tier_bd
            FROM public.credit_transactions WHERE id = v_settle_tx;
            IF FOUND THEN
                RETURN jsonb_build_object(
                    'transaction_id', v_existing_id, 'amount', v_existing_amt,
                    'allowance_consumed', v_existing_cons, 'balance_after', v_balance,
                    'idempotent', true, 'cap_warning', NULL, 'tier_breakdown', v_existing_tier_bd
                );
            END IF;
        END IF;
        RETURN jsonb_build_object('amount', 0, 'balance_after', v_balance, 'idempotent', true, 'tier_breakdown', '{}'::jsonb);
    END IF;
    IF v_status = 'expired' OR v_lease_expires <= now() THEN
        UPDATE public.credit_reservations SET status = 'expired' WHERE id = p_lease_id;
        RETURN jsonb_build_object('error', 'lease_expired', 'balance_after', v_balance);
    END IF;

    -- Zero-cost settle releases the lease without charging (M3).
    IF p_amount = 0 THEN
        UPDATE public.credit_reservations SET status = 'settled' WHERE id = p_lease_id;
        RETURN jsonb_build_object('transaction_id', NULL, 'amount', 0, 'balance_after', v_balance, 'idempotent', false, 'tier_breakdown', '{}'::jsonb);
    END IF;

    -- Allowance consume on the actual cost (mirrors deduct_with_allowance).
    -- Skipped when p_skip_allowance = TRUE (Fix 7 / #4): fixed-cost batch jobs
    -- reserved via the lease path must not deplete the free inference allowance.
    -- v_period_start (WS9): explicit p_period_start else the current UTC
    -- calendar month (unchanged).
    IF NOT p_skip_allowance AND v_plan_id IS NOT NULL THEN
        SELECT free_allowance INTO v_free_allowance FROM public.credit_plans WHERE id = v_plan_id;
        v_period_start := COALESCE(p_period_start, (date_trunc('month', now() AT TIME ZONE 'UTC'))::DATE);
        SELECT COALESCE(SUM(usage), 0) INTO v_used
        FROM public.credit_usage_window
        WHERE user_id = p_user_id AND plan_id = v_plan_id AND billing_period = v_period_start;
        v_consume := LEAST(GREATEST(COALESCE(v_free_allowance, 0) - COALESCE(v_used, 0), 0), p_amount);
    END IF;
    v_net := p_amount - v_consume;

    -- Floor enforcement (C1): clamp v_net so the post-settle balance stays ≥ floor.
    -- strict / strict_prepaid → floor is p_min_balance (engine's min_balance).
    -- overdraft → floor is the overdraft_floor stored on the lease (can be negative).
    IF v_billing_mode IN ('strict', 'strict_prepaid') THEN
        v_settle_floor := COALESCE(p_min_balance, 0);
    ELSE
        v_settle_floor := COALESCE(v_overdraft_floor, 0);
    END IF;
    v_max_debit := GREATEST(0, v_balance - v_settle_floor);
    IF v_net > v_max_debit THEN
        v_net := v_max_debit;
        -- Re-clamp allowance consume so it doesn't exceed amount - net.
        IF v_net < p_amount THEN
            v_consume := LEAST(v_consume, p_amount - v_net);
        END IF;
    END IF;

    -- Spend cap is ADVISORY at settle (never blocks): record the strongest breach.
    FOR v_cap IN
        SELECT action, cap_type, model, cap_limit FROM public.credit_spend_caps
        WHERE user_id = p_user_id AND (model IS NULL OR model = p_model)
        ORDER BY (action = 'deny') DESC, cap_limit ASC
    LOOP
        v_cap_window := CASE v_cap.cap_type
            WHEN 'daily' THEN date_trunc('day', now() AT TIME ZONE 'UTC')
            ELSE date_trunc('month', now() AT TIME ZONE 'UTC')
        END;
        SELECT COALESCE(SUM(ABS(ct.amount)), 0) INTO v_cap_spend
        FROM public.credit_transactions ct
        WHERE ct.user_id = p_user_id AND ct.type IN ('usage', 'team_usage') AND ct.amount < 0
          AND ct.created_at >= v_cap_window
          AND (v_cap.model IS NULL OR ct.metadata->>'model' = v_cap.model);
        IF v_cap_spend + v_net > v_cap.cap_limit AND (v_cap_warning IS NULL OR (v_cap_warning <> 'deny' AND v_cap.action = 'deny')) THEN
            v_cap_warning := v_cap.action;
        END IF;
    END LOOP;

    IF v_consume > 0 THEN
        INSERT INTO public.credit_usage_window (user_id, plan_id, billing_period, usage)
        VALUES (p_user_id, v_plan_id, v_period_start, v_consume)
        ON CONFLICT (user_id, plan_id, billing_period)
        DO UPDATE SET usage = public.credit_usage_window.usage + v_consume, updated_at = now();
    END IF;

    -- ── Tier walk (023): identical algorithm to deduct_with_allowance,
    -- applied to this already-floor-clamped v_net. No special-casing.
    v_tier_remaining := v_net;

    FOR v_walk IN
        SELECT tier_key, priority, 0 AS grp FROM public.credit_tiers
        UNION ALL
        SELECT uct.tier_key, 0, 1 AS grp
        FROM public.user_credit_tiers uct
        WHERE uct.user_id = p_user_id
          AND NOT EXISTS (SELECT 1 FROM public.credit_tiers ct WHERE ct.tier_key = uct.tier_key)
        ORDER BY grp ASC, priority ASC, tier_key ASC
    LOOP
        EXIT WHEN v_tier_remaining <= 0;

        SELECT balance INTO v_tier_balance
        FROM public.user_credit_tiers
        WHERE user_id = p_user_id AND tier_key = v_walk.tier_key
        FOR UPDATE;
        v_tier_balance := COALESCE(v_tier_balance, 0);

        v_take := LEAST(v_tier_balance, v_tier_remaining);
        IF v_take > 0 THEN
            UPDATE public.user_credit_tiers
            SET balance = balance - v_take, updated_at = now()
            WHERE user_id = p_user_id AND tier_key = v_walk.tier_key;

            v_tier_breakdown := v_tier_breakdown || jsonb_build_object(v_walk.tier_key, v_take);
            v_tier_remaining := v_tier_remaining - v_take;
        END IF;
    END LOOP;

    IF v_tier_remaining > 0 THEN
        SELECT tier_key INTO v_sink_tier FROM public.credit_tiers WHERE allow_overdraft = true LIMIT 1;
        IF v_sink_tier IS NULL THEN
            SELECT tier_key INTO v_sink_tier FROM public.credit_tiers ORDER BY priority DESC, tier_key DESC LIMIT 1;
        END IF;
        IF v_sink_tier IS NULL THEN
            v_sink_tier := 'default';
        END IF;

        INSERT INTO public.user_credit_tiers (user_id, tier_key, balance)
        VALUES (p_user_id, v_sink_tier, -v_tier_remaining)
        ON CONFLICT (user_id, tier_key) DO UPDATE SET
            balance = public.user_credit_tiers.balance - v_tier_remaining,
            updated_at = now();

        v_tier_breakdown := v_tier_breakdown || jsonb_build_object(
            v_sink_tier, COALESCE((v_tier_breakdown->>v_sink_tier)::numeric, 0) + v_tier_remaining
        );
        v_tier_remaining := 0;
    END IF;

    v_metadata := COALESCE(p_metadata, '{}'::jsonb)
        || jsonb_strip_nulls(jsonb_build_object('idempotency_key', p_idempotency_key, 'model', p_model))
        || jsonb_build_object('allowance_consumed', v_consume, 'balance_after', v_balance - v_net, 'tier_breakdown', v_tier_breakdown);

    UPDATE public.user_credits SET balance = balance - v_net, updated_at = now()
    WHERE user_id = p_user_id RETURNING balance INTO v_new_balance;

    INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
    VALUES (p_user_id, -v_net, 'usage', v_metadata) RETURNING id INTO v_tx_id;

    UPDATE public.credit_reservations SET status = 'settled', settle_tx_id = v_tx_id WHERE id = p_lease_id;

    RETURN jsonb_build_object(
        'transaction_id', v_tx_id, 'amount', v_net, 'allowance_consumed', v_consume,
        'balance_after', v_new_balance, 'idempotent', false, 'cap_warning', v_cap_warning,
        'tier_breakdown', v_tier_breakdown
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.settle_lease(UUID, UUID, NUMERIC, TEXT, NUMERIC, TEXT, JSONB, BOOLEAN, DATE) FROM anon, authenticated;

-- ── 8. refund_credits: LIFO tier restoration ─────────────────────────────
-- Steps through the existing over-refund check are unchanged (only the SELECT
-- of the original transaction row gains the `metadata` column, needed to read
-- its tier_breakdown). The refund/reference linkage reused here is the
-- pre-existing `reference_id` column on the refund row (set to the original
-- transaction id) — the same column the over-refund/already-refunded checks
-- above already query by; no new linkage field is introduced.
CREATE OR REPLACE FUNCTION public.refund_credits(
    p_transaction_id UUID,
    p_amount NUMERIC DEFAULT NULL,
    p_reason TEXT DEFAULT NULL,
    p_metadata JSONB DEFAULT '{}'::jsonb
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_tx RECORD;
    v_already_refunded BOOLEAN;
    v_original_debit NUMERIC;      -- positive magnitude of the original debit
    v_prior_refunded NUMERIC;      -- sum of all prior refunds for this original
    v_remaining NUMERIC;           -- still-refundable amount
    v_refund_amount NUMERIC;
    v_new_balance NUMERIC;
    v_refund_tx_id UUID;
    -- Tier LIFO restoration (023)
    v_orig_breakdown JSONB;
    v_prior_refund_breakdown JSONB;
    v_new_breakdown JSONB := '{}'::jsonb;
    v_to_allocate NUMERIC;
    v_tier_key TEXT;
    v_tier_orig_amt NUMERIC;
    v_tier_prior NUMERIC;
    v_tier_remaining NUMERIC;
    v_give NUMERIC;
BEGIN
    -- Prevent concurrent refund on same transaction (advisory + row locks below).
    PERFORM pg_advisory_xact_lock(hashtext('refund_' || p_transaction_id));

    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- Fetch + lock the original transaction row so its refund total cannot move
    -- under us while we compute the over-refund check. (023: also select
    -- metadata so the tier_breakdown driving LIFO restoration is available.)
    SELECT id, user_id, amount, type, metadata INTO v_tx
    FROM public.credit_transactions
    WHERE id = p_transaction_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object(
            'error', 'not_found',
            'user_id', '',
            'new_balance', 0
        );
    END IF;

    -- Lock the balance row up front. Same lock the debit took, so a refund and a
    -- concurrent deduct on the same user serialize. Created if missing (the row
    -- should already exist for any user with a prior debit, but be defensive).
    SELECT balance INTO v_new_balance
    FROM public.user_credits
    WHERE user_id = v_tx.user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        INSERT INTO public.user_credits (user_id, balance, lifetime_purchased)
        VALUES (v_tx.user_id, 0, 0)
        ON CONFLICT (user_id) DO NOTHING;

        SELECT balance INTO v_new_balance
        FROM public.user_credits
        WHERE user_id = v_tx.user_id
        FOR UPDATE;
    END IF;

    -- (2) Reject refunding a non-debit. Only a `usage`/`team_usage` deduction
    -- (negative amount) is refundable. A purchase / refund / adjustment / bonus
    -- has nothing to give back, so its refundable amount is 0 and ANY refund
    -- over-refunds. We return `over_refund` (not `not_found`) because the row
    -- DOES exist — `not_found` would be misleading; `over_refund` precisely says
    -- "more than is refundable" (which for a non-debit is anything > 0).
    IF v_tx.type NOT IN ('usage', 'team_usage') OR v_tx.amount >= 0 THEN
        RETURN jsonb_build_object(
            'error', 'over_refund',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- Positive magnitude of the original debit (amount is negative for a debit).
    v_original_debit := ABS(v_tx.amount);

    -- (3a) Back-compat duplicate detection: a prior FULL refund of this exact
    -- transaction (one refund row whose amount equals the full original debit)
    -- replays as `already_refunded`. Cumulative partials are NOT treated as
    -- duplicates here — they fall through to the over-refund cap in (1)/(3b).
    SELECT EXISTS (
        SELECT 1 FROM public.credit_transactions
        WHERE reference_id = p_transaction_id
          AND type = 'refund'
          AND amount = v_original_debit
    ) INTO v_already_refunded;

    IF v_already_refunded THEN
        RETURN jsonb_build_object(
            'error', 'already_refunded',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- Determine the requested refund amount (NULL ⇒ full remaining).
    -- Sum of all prior refunds for this original (refund rows store a positive
    -- amount). Read under the FOR UPDATE lock taken above.
    SELECT COALESCE(SUM(amount), 0) INTO v_prior_refunded
    FROM public.credit_transactions
    WHERE reference_id = p_transaction_id
      AND type = 'refund';

    v_remaining := v_original_debit - v_prior_refunded;

    -- Requested amount: explicit value, else the full remaining refundable.
    v_refund_amount := COALESCE(p_amount, v_remaining);

    -- (1) Over-refund rejection: prior refunds + this refund must not exceed the
    -- original debit. Equivalently: this refund must not exceed what remains.
    -- A non-positive request (<= 0), or one that exceeds the remaining balance
    -- (including the case where the original is already fully refunded so
    -- v_remaining = 0), is rejected WITHOUT refunding.
    IF v_refund_amount <= 0 OR v_refund_amount > v_remaining THEN
        RETURN jsonb_build_object(
            'error', 'over_refund',
            'user_id', v_tx.user_id,
            'new_balance', COALESCE(v_new_balance, 0)
        );
    END IF;

    -- (3b) Apply: restore balance and append the refund ledger row. Cumulative
    -- partials accumulate via successive refund rows; the cap above guarantees
    -- the running total never exceeds v_original_debit.
    UPDATE public.user_credits
    SET balance = balance + v_refund_amount,
        updated_at = now()
    WHERE user_id = v_tx.user_id
    RETURNING balance INTO v_new_balance;

    -- ── Tier LIFO restoration (023) ──────────────────────────────────────
    -- tier_remaining[t] is derived fresh each time from
    -- original_breakdown[t] - sum(prior refunds' own breakdown[t]) — never a
    -- running counter — so repeated partial refunds compose correctly.
    v_orig_breakdown := COALESCE(v_tx.metadata->'tier_breakdown', jsonb_build_object('default', v_original_debit));

    SELECT COALESCE(jsonb_object_agg(kv.tier_key, kv.tier_sum), '{}'::jsonb) INTO v_prior_refund_breakdown
    FROM (
        SELECT e.key AS tier_key, SUM((e.value)::numeric) AS tier_sum
        FROM public.credit_transactions ct
        CROSS JOIN LATERAL jsonb_each_text(COALESCE(ct.metadata->'tier_breakdown', '{}'::jsonb)) AS e(key, value)
        WHERE ct.reference_id = p_transaction_id AND ct.type = 'refund'
        GROUP BY e.key
    ) kv;

    v_to_allocate := v_refund_amount;

    -- Walk tiers in REVERSE priority order (highest-priority-number / last
    -- drained tier first). Tiers no longer present in credit_tiers (config
    -- drift) sort last, mirroring the deduct walk's "orphans appended last".
    FOR v_tier_key, v_tier_orig_amt IN
        SELECT e.key, (e.value)::numeric
        FROM jsonb_each_text(v_orig_breakdown) AS e(key, value)
        LEFT JOIN public.credit_tiers ct ON ct.tier_key = e.key
        ORDER BY COALESCE(ct.priority, -2147483648) DESC, e.key DESC
    LOOP
        EXIT WHEN v_to_allocate <= 0;

        v_tier_prior := COALESCE((v_prior_refund_breakdown->>v_tier_key)::numeric, 0);
        v_tier_remaining := GREATEST(v_tier_orig_amt - v_tier_prior, 0);
        v_give := LEAST(v_tier_remaining, v_to_allocate);

        IF v_give > 0 THEN
            INSERT INTO public.user_credit_tiers (user_id, tier_key, balance)
            VALUES (v_tx.user_id, v_tier_key, v_give)
            ON CONFLICT (user_id, tier_key) DO UPDATE SET
                balance = public.user_credit_tiers.balance + v_give,
                updated_at = now();

            v_new_breakdown := v_new_breakdown || jsonb_build_object(v_tier_key, v_give);
            v_to_allocate := v_to_allocate - v_give;
        END IF;
    END LOOP;

    INSERT INTO public.credit_transactions (user_id, amount, type, reference_type, reference_id, metadata)
    VALUES (v_tx.user_id, v_refund_amount, 'refund', p_reason, p_transaction_id,
            p_metadata || jsonb_build_object('reason', p_reason, 'tier_breakdown', v_new_breakdown))
    RETURNING id INTO v_refund_tx_id;

    RETURN jsonb_build_object(
        'refund_transaction_id', v_refund_tx_id,
        'user_id', v_tx.user_id,
        'amount', v_refund_amount,
        'new_balance', v_new_balance,
        'tier_breakdown', v_new_breakdown
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.refund_credits FROM anon, authenticated;

-- ── 9. expire_credits: re-scoped to per-(user_id, tier) grouping ─────────
CREATE OR REPLACE FUNCTION public.expire_credits(p_dry_run BOOLEAN DEFAULT false)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_expired_count INTEGER := 0;
    v_expired_amount NUMERIC := 0;
    v_expired_by_tier JSONB := '{}'::jsonb;
    v_group RECORD;
    v_group_expired NUMERIC;
    v_current_tier_balance NUMERIC;
    v_current_balance NUMERIC;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    -- A grant is "sweepable" when it has an expires_at in the past AND has not
    -- already been swept (no 'swept_at' marker). Marking swept grants is what
    -- makes the sweep idempotent: a second run finds nothing and never
    -- double-debits (H4). Mirrors MemoryStore, which nulls expires_at on sweep.
    -- (023) Grouping is now per-(user_id, tier) instead of per-user_id, reading
    -- tier straight off each grant's own metadata->>'tier' (a tier's `expires`
    -- flag is only consulted at add_credits time; once stamped, a
    -- transaction's fate is fixed regardless of later config changes).
    FOR v_group IN
        SELECT DISTINCT user_id, COALESCE(metadata->>'tier', 'default') AS tier_key
        FROM public.credit_transactions
        WHERE type IN ('purchase', 'adjustment')
          AND metadata ? 'expires_at'
          AND NOT (metadata ? 'swept_at')
          AND (metadata->>'expires_at')::timestamptz <= now()
    LOOP
        -- Total un-swept expired grants for this (user, tier).
        SELECT COALESCE(SUM(amount), 0) INTO v_group_expired
        FROM public.credit_transactions
        WHERE user_id = v_group.user_id
          AND COALESCE(metadata->>'tier', 'default') = v_group.tier_key
          AND type IN ('purchase', 'adjustment')
          AND metadata ? 'expires_at'
          AND NOT (metadata ? 'swept_at')
          AND (metadata->>'expires_at')::timestamptz <= now();

        -- Lock the aggregate balance row (prevents racing a concurrent deduction).
        SELECT COALESCE(balance, 0) INTO v_current_balance
        FROM public.user_credits
        WHERE user_id = v_group.user_id
        FOR UPDATE;

        -- Lock (if present) this tier's own balance row for this user.
        SELECT balance INTO v_current_tier_balance
        FROM public.user_credit_tiers
        WHERE user_id = v_group.user_id AND tier_key = v_group.tier_key
        FOR UPDATE;
        v_current_tier_balance := COALESCE(v_current_tier_balance, 0);

        -- Cap at both the tier's own balance and the aggregate (never expire
        -- money that isn't actually there under either ceiling).
        v_group_expired := LEAST(v_group_expired, v_current_tier_balance, v_current_balance);

        IF v_group_expired > 0 THEN
            v_expired_count := v_expired_count + 1;
            v_expired_amount := v_expired_amount + v_group_expired;
            v_expired_by_tier := v_expired_by_tier || jsonb_build_object(
                v_group.tier_key,
                COALESCE((v_expired_by_tier->>v_group.tier_key)::numeric, 0) + v_group_expired
            );

            IF NOT p_dry_run THEN
                -- Deduct expired amount from both the tier and aggregate balances.
                UPDATE public.user_credit_tiers
                SET balance = balance - v_group_expired, updated_at = now()
                WHERE user_id = v_group.user_id AND tier_key = v_group.tier_key;

                UPDATE public.user_credits
                SET balance = balance - v_group_expired,
                    updated_at = now()
                WHERE user_id = v_group.user_id;

                -- Log one adjustment transaction per (user, tier).
                INSERT INTO public.credit_transactions (user_id, amount, type, metadata)
                VALUES (v_group.user_id, -v_group_expired, 'adjustment',
                        jsonb_build_object('reason', 'credit_expired', 'expired_amount', v_group_expired, 'tier', v_group.tier_key));
            END IF;
        END IF;

        -- Mark the grants we just considered as swept so they're never
        -- re-swept (only on a real run; a dry run must not mutate state).
        IF NOT p_dry_run THEN
            UPDATE public.credit_transactions
            SET metadata = metadata || jsonb_build_object('swept_at', to_jsonb(now()))
            WHERE user_id = v_group.user_id
              AND COALESCE(metadata->>'tier', 'default') = v_group.tier_key
              AND type IN ('purchase', 'adjustment')
              AND metadata ? 'expires_at'
              AND NOT (metadata ? 'swept_at')
              AND (metadata->>'expires_at')::timestamptz <= now();
        END IF;
    END LOOP;

    RETURN jsonb_build_object(
        'expired_count', v_expired_count,
        'expired_amount', v_expired_amount,
        'expired_by_tier', v_expired_by_tier,
        'dry_run', p_dry_run
    );
END;
$$;

REVOKE EXECUTE ON FUNCTION public.expire_credits FROM anon, authenticated;

-- ── 10. get_user_credit_tiers: per-tier balance query ────────────────────
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
        -- No tiers configured: synthesize a single "default" entry so the API
        -- shape is uniform whether or not tiers are configured.
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
