-- bursar: core credit management RPCs.
-- All functions use OR REPLACE for idempotent setup.
-- All mutation functions require service_role (backend-only).

-- reserve_credits / deduct_credits (the original two-phase reservation flow)
-- were superseded by the atomic lease lifecycle (see 009_deduct_and_leases.sql)
-- and the atomic deduct_with_allowance RPC. Drop every overload by name so a
-- database upgrading from the pre-squash migration history doesn't keep them
-- around as dead code. The credit_reservations TABLE itself is unaffected —
-- it backs the lease system.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT oid::regprocedure::text AS sig FROM pg_proc
        WHERE proname IN ('reserve_credits', 'deduct_credits') AND pronamespace = 'public'::regnamespace
    LOOP
        EXECUTE 'DROP FUNCTION ' || r.sig;
    END LOOP;
END $$;

-- credits_add gained a trailing p_tier param (see 010_credit_tiers.sql for the
-- tier-resolution logic). Drop any pre-existing overload by name first so
-- CREATE OR REPLACE below fully replaces it (no-op on fresh installs).
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

-- credits_add: Atomically add credits to user's balance and log transaction.
-- Money is NUMERIC(18,4). Purchases must be a positive, finite amount;
-- only the explicit 'adjustment' type may carry a negative/zero amount.
--
-- p_tier (see 010_credit_tiers.sql): resolves against configured credit tiers
-- and reconciles p_metadata's `expires_at` against the resolved tier's
-- `expires`/`default_ttl_days`. When no tiers are configured, p_tier must be
-- NULL/'default' and expires_at passes through unchanged (zero behavioral
-- change for pre-existing, tier-less configs).
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
        ORDER BY priority ASC, tier_key ASC
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


-- get_credits_balance: Read current balance and lifetime purchased total.
CREATE OR REPLACE FUNCTION public.get_credits_balance(p_user_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_balance NUMERIC;
    v_lifetime NUMERIC;
BEGIN
    IF auth.role() IS DISTINCT FROM 'service_role' THEN
        RETURN jsonb_build_object('error', 'unauthorized');
    END IF;

    SELECT balance, lifetime_purchased INTO v_balance, v_lifetime
    FROM public.user_credits
    WHERE user_id = p_user_id;

    RETURN jsonb_build_object(
        'user_id', p_user_id,
        'balance', COALESCE(v_balance, 0),
        'lifetime_purchased', COALESCE(v_lifetime, 0)
    );
END;
$$;


-- Defense-in-depth: revoke direct execute from user roles.
-- Only service_role RPC calls (via Supabase client with service key) should succeed.
REVOKE EXECUTE ON FUNCTION public.credits_add(UUID, NUMERIC, public.credit_tx_type, JSONB, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.get_credits_balance FROM PUBLIC, anon, authenticated;

-- Refresh PostgREST schema cache so REST API can resolve the new RPCs.
NOTIFY pgrst, 'reload schema';
