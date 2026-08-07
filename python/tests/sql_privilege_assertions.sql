DO $$
BEGIN
    IF has_table_privilege(
        'bursar_client',
        'bursar.credit_ledger_entries',
        'INSERT'
    ) THEN
        RAISE EXCEPTION 'bursar_client can insert ledger rows directly';
    END IF;

    IF has_function_privilege(
        'bursar_client',
        'bursar.require_internal_mutation()',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'bursar_client can execute an internal trigger helper';
    END IF;

    IF NOT has_function_privilege(
        'bursar_client',
        'bursar.post_credit(uuid,bursar.ledger_entry_kind,numeric,text,text,jsonb,text,uuid,timestamptz,numeric)',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'bursar_client cannot execute a public credit RPC';
    END IF;

    IF NOT has_function_privilege(
        'bursar_operator',
        'bursar.claim_outbox_events(integer,integer,text[])',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'bursar_operator cannot execute an operator RPC';
    END IF;

    IF has_function_privilege(
        'bursar_client',
        'bursar.claim_outbox_events(integer,integer,text[])',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'bursar_client can execute an operator RPC';
    END IF;
END
$$;
