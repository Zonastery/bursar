DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM aclexplode(
            COALESCE(
                (
                    SELECT defaults.defaclacl
                    FROM pg_default_acl AS defaults
                    WHERE defaults.defaclrole = 'bursar_runtime'::regrole::oid
                      AND defaults.defaclnamespace = 0
                      AND defaults.defaclobjtype = 'f'
                ),
                acldefault('f', 'bursar_runtime'::regrole::oid)
            )
        ) AS privilege
        WHERE privilege.grantee = 0
          AND privilege.privilege_type = 'EXECUTE'
    ) THEN
        RAISE EXCEPTION
            'bursar_runtime default function ACL grants EXECUTE to PUBLIC';
    END IF;

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
        'bursar_runtime',
        'bursar.current_provider_environment()',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION
            'bursar_runtime cannot execute the provider-environment helper';
    END IF;

    IF has_function_privilege(
        'bursar_client',
        'bursar.current_provider_environment()',
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION
            'bursar_client can execute the private provider-environment helper';
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
