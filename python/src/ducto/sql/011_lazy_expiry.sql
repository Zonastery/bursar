-- ducto: lazy per-user credit expiry + idempotent add_credits.
--
-- Allowance windows and lease TTLs are already lazy-on-read (no cron needed).
-- Credit expiry (expire_credits/sweep_expired_credits) was the odd one out: a
-- stored aggregate that required an explicit sweep call, forcing an
-- operator-maintained cron job. This migration closes that gap with a
-- per-user-scoped sweep variant the manager layer can call lazily before
-- balance-affecting operations, and adds idempotency to credits_add so a
-- higher-level "grant subscription cycle" flow can be called safely from a
-- webhook handler that might redeliver the same event.
--
-- ── A note on function-signature evolution (read before touching this file) ──
-- This codebase has been bitten TWICE by the same class of bug: adding a
-- trailing parameter to an existing RPC creates a NEW, distinct overload in
-- Postgres (function identity = name + input arg TYPES; a default value does
-- not make two different-arity signatures "the same function"). If an
-- unqualified `REVOKE EXECUTE ON FUNCTION name ...` (no arg list) later runs
-- while two overloads coexist, Postgres cannot resolve it and raises
-- "function name is not unique" — which aborts the whole migration FILE that
-- contains it (a single multi-statement SQL string executes as one implicit
-- transaction), breaking idempotent re-migration on the second and every
-- later `setup()` run. This was originally fixed for `credits_add` by
-- qualifying its REVOKE with the exact signature (see 002_credit_rpcs.sql).
--
-- Both functions evolved below follow the SAME established pattern used
-- throughout this codebase (002_credit_rpcs.sql's credits_add,
-- 009_deduct_and_leases.sql's deduct_with_allowance/create_lease/
-- settle_lease): (1) drop EVERY existing overload of the function by name
-- (dynamic SQL over pg_proc) so the old-arity version can never linger
-- alongside the new one, (2) CREATE OR REPLACE the single current-arity
-- definition, (3) REVOKE qualified with that EXACT signature (never bare).
-- Verified empirically (not just by inspection) against a real Postgres 14
-- instance, running the equivalent of `setup()` four times back to back with
-- zero errors on every pass.
--
-- `expire_credits` required one additional, minimal companion fix: its
-- original REVOKE in 006_refunds_and_expiry.sql was unqualified (`REVOKE
-- EXECUTE ON FUNCTION public.expire_credits FROM ...`, no arg list). Because
-- 006 unconditionally re-creates the OLD 1-arg overload every time it runs
-- (it has no drop-loop of its own), that unqualified REVOKE becomes ambiguous
-- from the second full migration run onward, once this file's 2-arg overload
-- exists — reproduced directly against Postgres before writing this comment.
-- That REVOKE has been qualified to `expire_credits(BOOLEAN)` in
-- 006_refunds_and_expiry.sql alongside this migration (a one-line, purely
-- disambiguating change — identical grantees, identical behavior for every
-- existing caller) so the two files coexist idempotently forever after.
-- `credits_add`'s own REVOKE in 002_credit_rpcs.sql is already qualified
-- (the historical fix referenced above), so it needs no further changes here.

-- ── 1. credits_add: add p_idempotency_key ────────────────────────────────
-- Evolves the credits_add() RPC (currently a 5-arg function after
-- 010_credit_tiers.sql's p_tier addition — see 002_credit_rpcs.sql) with a
-- trailing p_idempotency_key TEXT DEFAULT NULL, implementing the same
-- "check existing row WHERE metadata->>'idempotency_key' = p_idempotency_key,
-- return early if found" guard already used by deduct_with_allowance and
-- settle_lease (009_deduct_and_leases.sql ~143-152, ~571-576). A retried
-- grant (e.g. a webhook redelivered by the sender) returns the ORIGINAL
-- transaction untouched — no second grant, no second ledger row — reporting
-- the CURRENT balance/lifetime (not a frozen snapshot; a plain credit grant
-- has no floor/cap check tied to the original call the way a debit does).
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'credits_add' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.credits_add(
    p_user_id UUID,
    p_amount NUMERIC,
    p_type public.credit_tx_type DEFAULT 'adjustment',
    p_metadata JSONB DEFAULT NULL,
    p_tier TEXT DEFAULT NULL,
    p_idempotency_key TEXT DEFAULT NULL
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
    v_existing_amount NUMERIC;
    v_existing_tier TEXT;
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

    -- ── Idempotency replay (user-scoped) ─────────────────────────────────
    -- Runs before tier resolution so a replay never trips tier_not_found/
    -- tier_required/expires_at validation on a redelivered call — the
    -- original grant already happened; nothing further is validated.
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id, amount, COALESCE(metadata->>'tier', 'default')
        INTO v_transaction_id, v_existing_amount, v_existing_tier
        FROM public.credit_transactions
        WHERE user_id = p_user_id AND metadata->>'idempotency_key' = p_idempotency_key
        LIMIT 1;

        IF FOUND THEN
            SELECT balance, lifetime_purchased INTO v_new_balance, v_lifetime
            FROM public.user_credits
            WHERE user_id = p_user_id;

            RETURN jsonb_build_object(
                'id', v_transaction_id,
                'user_id', p_user_id,
                'amount', v_existing_amount,
                'new_balance', COALESCE(v_new_balance, 0),
                'lifetime_purchased', COALESCE(v_lifetime, 0),
                'tier', v_existing_tier
            );
        END IF;
    END IF;

    -- ── Tier resolution ──────────────────────────────────────────────────
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
            ELSE
                -- Parity with MemoryStore (Python/JS): an explicit expires_at
                -- must be in the future, not just present.
                IF (v_metadata->>'expires_at')::timestamptz <= now() THEN
                    RETURN jsonb_build_object(
                        'error', 'invalid_expires_at',
                        'tier', v_resolved_tier,
                        'expires_at', v_metadata->>'expires_at'
                    );
                END IF;
            END IF;
        END IF;
    END IF;

    v_metadata := v_metadata || jsonb_build_object('tier', v_resolved_tier);
    IF p_idempotency_key IS NOT NULL THEN
        v_metadata := v_metadata || jsonb_build_object('idempotency_key', p_idempotency_key);
    END IF;

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

    -- Per-tier balance: lazily created on first touch.
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

REVOKE EXECUTE ON FUNCTION public.credits_add(UUID, NUMERIC, public.credit_tx_type, JSONB, TEXT, TEXT) FROM anon, authenticated;

-- ── 2. expire_credits: add p_user_id (lazy per-user sweep) ──────────────
-- Evolves expire_credits() (defined in 006_refunds_and_expiry.sql) with a
-- trailing p_user_id UUID DEFAULT NULL. When given, only that user's expired
-- grants are considered — everything else (grouping, the tier/aggregate
-- balance clamp, the swept_at idempotency marker) is byte-for-byte identical
-- to the existing global sweep, just filtered to one user's rows. NULL (the
-- default) preserves the exact original global-sweep behavior/output shape.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname = 'expire_credits' AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.expire_credits(p_dry_run BOOLEAN DEFAULT false, p_user_id UUID DEFAULT NULL)
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
    -- double-debits. Mirrors MemoryStore, which nulls expires_at on sweep.
    -- Grouping is per-(user_id, tier) instead of per-user_id, reading tier
    -- straight off each grant's own metadata->>'tier' (a tier's `expires` flag
    -- is only consulted at add_credits time; once stamped, a transaction's
    -- fate is fixed regardless of later config changes).
    -- p_user_id (lazy per-user sweep): when given, only that user's rows are
    -- ever considered; every other user's expired grants are left untouched.
    FOR v_group IN
        SELECT DISTINCT user_id, COALESCE(metadata->>'tier', 'default') AS tier_key
        FROM public.credit_transactions
        WHERE type IN ('purchase', 'adjustment')
          AND metadata ? 'expires_at'
          AND NOT (metadata ? 'swept_at')
          AND (metadata->>'expires_at')::timestamptz <= now()
          AND (p_user_id IS NULL OR user_id = p_user_id)
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

REVOKE EXECUTE ON FUNCTION public.expire_credits(BOOLEAN, UUID) FROM anon, authenticated;

-- ── 3. Index for the per-user scoped sweep ───────────────────────────────
CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_expires
    ON public.credit_transactions (user_id, (metadata->>'expires_at'))
    WHERE metadata ? 'expires_at' AND NOT (metadata ? 'swept_at');

NOTIFY pgrst, 'reload schema';
