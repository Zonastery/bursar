-- Canonical Bursar accounting regression checks. Run against a disposable
-- PostgreSQL 17 database after `bursar migrate`.
BEGIN;

SELECT bursar.create_tenant(
    '00000000-0000-0000-0000-000000000889'::uuid,
    'canonical-regression',
    'Canonical regression'
);
SELECT set_config(
    'bursar.tenant_id',
    '00000000-0000-0000-0000-000000000889',
    false
);

DO $$
DECLARE
    v_subject_id uuid := '00000000-0000-0000-0000-000000000890';
    v_account_id uuid;
    v_grant record;
    v_debit record;
    v_replay record;
    v_balance numeric;
    v_ledger_total numeric;
    v_lot_available numeric;
    v_lot_consumed numeric;
    v_source_allocated numeric;
BEGIN
    PERFORM bursar.publish_and_activate_catalog(
        1,
        '{
          "version":1,
          "credits":{
            "buckets":{"default":{"priority":10}},
            "default_bucket":"default"
          },
          "plans":{}
        }'::jsonb,
        'canonical-regression'
    );

    SELECT *
    INTO v_grant
    FROM bursar.post_credit(
        v_subject_id,
        'purchase',
        100,
        'regression-grant',
        'regression:grant'
    );
    SELECT *
    INTO v_debit
    FROM bursar.post_credit(
        v_subject_id,
        'usage',
        -20,
        'regression-usage',
        'regression:usage'
    );
    SELECT *
    INTO v_replay
    FROM bursar.post_credit(
        v_subject_id,
        'usage',
        -20,
        'regression-usage',
        'regression:usage'
    );

    IF v_grant.error_code IS NOT NULL
       OR v_debit.error_code IS NOT NULL
       OR v_replay.error_code IS NOT NULL
       OR NOT v_replay.replayed
       OR v_replay.entry_id <> v_debit.entry_id
    THEN
        RAISE EXCEPTION
            'canonical posting/replay failed: % / % / %',
            row_to_json(v_grant),
            row_to_json(v_debit),
            row_to_json(v_replay);
    END IF;

    SELECT id, balance
    INTO v_account_id, v_balance
    FROM bursar.credit_accounts
    WHERE subject_id = v_subject_id
      AND account_kind = 'personal';

    SELECT COALESCE(sum(amount), 0)
    INTO v_ledger_total
    FROM bursar.credit_ledger_entries
    WHERE account_id = v_account_id;

    SELECT
        COALESCE(sum(granted - consumed), 0),
        COALESCE(sum(consumed), 0)
    INTO v_lot_available, v_lot_consumed
    FROM bursar.credit_lots
    WHERE account_id = v_account_id;

    SELECT COALESCE(sum(source_allocation.amount), 0)
    INTO v_source_allocated
    FROM bursar.credit_lot_source_allocations AS source_allocation
    JOIN bursar.credit_lot_allocations AS lot_allocation
      ON lot_allocation.id = source_allocation.lot_allocation_id
    WHERE lot_allocation.debit_entry_id = v_debit.entry_id;

    IF v_balance <> 80
       OR v_balance <> v_ledger_total
       OR v_balance <> v_lot_available
       OR v_lot_consumed <> 20
       OR v_source_allocated <> 20
    THEN
        RAISE EXCEPTION
            'canonical invariant failed: balance %, ledger %, lots %, consumed %, sources %',
            v_balance,
            v_ledger_total,
            v_lot_available,
            v_lot_consumed,
            v_source_allocated;
    END IF;
END
$$;

ROLLBACK;
