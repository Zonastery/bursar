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

-- credits_add gained a trailing p_bucket param (see 010_credit_tiers.sql for the
-- bucket-resolution logic). Drop any pre-existing overload by name first so
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

-- SUPERSEDED by 011_lazy_expiry.sql — this stub is immediately overwritten.
CREATE OR REPLACE FUNCTION public.credits_add(UUID, NUMERIC, public.credit_tx_type, JSONB, TEXT)
RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path TO '' AS $$
BEGIN RETURN NULL; END;
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
REVOKE EXECUTE ON FUNCTION public.get_credits_balance FROM PUBLIC, anon, authenticated;

-- Refresh PostgREST schema cache so REST API can resolve the new RPCs.
NOTIFY pgrst, 'reload schema';
