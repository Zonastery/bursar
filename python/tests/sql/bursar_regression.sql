-- Canonical Bursar baseline regression checks. Run against a disposable
-- PostgreSQL 15+ database after `bursar migrate`.
BEGIN;

DO $$
DECLARE
    v_user_id uuid := gen_random_uuid();
    v_account_id uuid;
    v_balance numeric;
    v_ledger_total numeric;
    v_lot_available numeric;
BEGIN
    PERFORM bursar.credits_add(
        v_user_id, 100, 'purchase', '{}'::jsonb, NULL, 'default',
        'regression:grant'
    );
    PERFORM bursar.deduct_with_allowance(
        p_user_id => v_user_id,
        p_amount => 20,
        p_idempotency_key => 'regression:usage'
    );

    SELECT id, balance INTO v_account_id, v_balance
    FROM bursar.credit_accounts
    WHERE user_id = v_user_id;

    SELECT COALESCE(sum(amount), 0) INTO v_ledger_total
    FROM bursar.credit_ledger_entries
    WHERE account_id = v_account_id;

    SELECT COALESCE(sum(granted - consumed), 0) INTO v_lot_available
    FROM bursar.credit_lots
    WHERE account_id = v_account_id
      AND (expires_at IS NULL OR expires_at > now());

    IF v_balance <> v_ledger_total OR v_balance <> v_lot_available THEN
        RAISE EXCEPTION
            'canonical invariant failed: balance %, ledger %, lots %',
            v_balance, v_ledger_total, v_lot_available;
    END IF;

    IF to_regclass('bursar.credit_transactions') IS NOT NULL
       OR to_regclass('bursar.user_credit_buckets') IS NOT NULL
       OR to_regclass('bursar.user_credits') IS NOT NULL
       OR to_regclass('bursar.credit_reservations') IS NOT NULL THEN
        RAISE EXCEPTION 'removed compatibility table exists';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'bursar'
          AND p.proname IN (
              'project_credit_transaction',
              'list_transactions',
              'list_transactions_cursor_with_total'
          )
    ) THEN
        RAISE EXCEPTION 'removed compatibility function exists';
    END IF;
END;
$$;

ROLLBACK;
