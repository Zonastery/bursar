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

END;
$$;

ROLLBACK;
