-- Migration: 030_operator_runtime_role.sql
-- Purpose: Split cross-tenant operator and partition DDL into dedicated owner roles.
-- Depends on: 029_multitenancy_security.sql plus payload partitions and pg_partman.
-- Security: NOLOGIN NOBYPASSRLS owners receive separately scoped table, policy,
--   extension, and DDL capabilities; callers get schema USAGE plus reviewed EXECUTE.
--
-- Contents
--   1. Operator table, helper, and cross-tenant RLS capabilities
--   2. Partition-owner pg_partman and payload ownership boundary
--   3. Managed-child hardening
--   4. Partition maintenance and operator ownership transfer
--   5. Owner-role catalog comments
--
-- Run cross-tenant operator RPCs under a dedicated non-login owner.
--
-- The migration owner is deliberately NOSUPERUSER and NOBYPASSRLS in a
-- least-privilege deployment.  Migration 029 left operator SECURITY DEFINER
-- functions owned by that principal while also forcing RLS on tenant tables,
-- so those functions could only work when migrations happened to run as a
-- superuser.  Keep FORCE RLS and give the operator function owner only the
-- table commands and policies its documented RPC surface requires.

-- ---------------------------------------------------------------------------
-- 1. Operator table, helper, and cross-tenant RLS capabilities
-- ---------------------------------------------------------------------------

-- Grant only the schemas and per-table commands exercised by reviewed operator
-- RPCs; the following command-specific RLS policies keep future grants explicit.
GRANT USAGE ON SCHEMA bursar, extensions
TO bursar_operator_runtime;

GRANT SELECT, UPDATE ON TABLE bursar.storage_settings
TO bursar_operator_runtime;
GRANT SELECT ON TABLE bursar.catalog_plan_quotas
TO bursar_operator_runtime;
GRANT SELECT, INSERT, UPDATE ON TABLE bursar.tenants
TO bursar_operator_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE bursar.event_outbox
TO bursar_operator_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE bursar.credit_usage_charges
TO bursar_operator_runtime;
GRANT SELECT ON TABLE bursar.credit_accounts
TO bursar_operator_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE bursar.usage_charge_payloads
TO bursar_operator_runtime;
GRANT SELECT, UPDATE ON TABLE bursar.billing_events
TO bursar_operator_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE bursar.billing_event_payloads
TO bursar_operator_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE bursar.quota_events
TO bursar_operator_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE bursar.quota_usage_events
TO bursar_operator_runtime;
GRANT SELECT, UPDATE ON TABLE bursar.credit_leases
TO bursar_operator_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE bursar.usage_daily_rollups
TO bursar_operator_runtime;
-- Both private owners re-evaluate ordinary CHECK and validation helpers while
-- mutating rows or partitions. Mirror the runtime-owner dependency closure.
DO $$
DECLARE
    v_function record;
BEGIN
    FOR v_function IN
        SELECT function_info.oid::regprocedure AS function_name
        FROM pg_proc AS function_info
        JOIN pg_namespace AS namespace_info
          ON namespace_info.oid = function_info.pronamespace
        WHERE namespace_info.nspname = 'bursar'
          AND NOT function_info.prosecdef
        ORDER BY function_info.oid
    LOOP
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION %s '
            'TO bursar_operator_runtime, bursar_partition_runtime',
            v_function.function_name
        );
    END LOOP;
END
$$;

-- Grant the operator owner only the cryptographic and JSON validation primitives
-- reached by its row checks, triggers, and validation helpers.
GRANT EXECUTE
ON FUNCTION extensions.digest(bytea, text)
TO bursar_operator_runtime;
GRANT EXECUTE
ON FUNCTION extensions.gen_random_bytes(integer)
TO bursar_operator_runtime;
GRANT EXECUTE
ON FUNCTION extensions.jsonb_matches_schema(json, jsonb)
TO bursar_operator_runtime;
GRANT EXECUTE
ON FUNCTION extensions.jsonschema_is_valid(json)
TO bursar_operator_runtime;
GRANT EXECUTE
ON FUNCTION extensions.jsonschema_validation_errors(json, json)
TO bursar_operator_runtime;

-- One policy per SQL command prevents a later table grant from silently
-- widening this role.  Tenant selection belongs inside the tenant-bound RPCs;
-- global operator jobs intentionally work across tenants.
DO $$
DECLARE
    v_table name;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'storage_settings',
        'catalog_plan_quotas',
        'tenants',
        'event_outbox',
        'credit_usage_charges',
        'credit_accounts',
        'usage_charge_payloads',
        'billing_events',
        'billing_event_payloads',
        'quota_events',
        'quota_usage_events',
        'credit_leases',
        'usage_daily_rollups'
    ]::name[]
    LOOP
        EXECUTE format(
            'CREATE POLICY %I ON bursar.%I '
            'FOR SELECT TO bursar_operator_runtime USING (TRUE)',
            left('operator_runtime_select_' || v_table, 63),
            v_table
        );
    END LOOP;

    FOREACH v_table IN ARRAY ARRAY[
        'storage_settings',
        'tenants',
        'event_outbox',
        'credit_usage_charges',
        'usage_charge_payloads',
        'billing_events',
        'billing_event_payloads',
        'quota_events',
        'quota_usage_events',
        'credit_leases',
        'usage_daily_rollups'
    ]::name[]
    LOOP
        EXECUTE format(
            'CREATE POLICY %I ON bursar.%I '
            'FOR UPDATE TO bursar_operator_runtime '
            'USING (TRUE) WITH CHECK (TRUE)',
            left('operator_runtime_update_' || v_table, 63),
            v_table
        );
    END LOOP;

    CREATE POLICY operator_runtime_insert_tenants ON bursar.tenants
    FOR INSERT TO bursar_operator_runtime
    WITH CHECK (TRUE);

    FOREACH v_table IN ARRAY ARRAY[
        'event_outbox',
        'credit_usage_charges',
        'usage_charge_payloads',
        'billing_event_payloads',
        'quota_events',
        'quota_usage_events',
        'usage_daily_rollups'
    ]::name[]
    LOOP
        EXECUTE format(
            'CREATE POLICY %I ON bursar.%I '
            'FOR DELETE TO bursar_operator_runtime USING (TRUE)',
            left('operator_runtime_delete_' || v_table, 63),
            v_table
        );
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 2. Partition-owner pg_partman and payload ownership boundary
-- ---------------------------------------------------------------------------

-- pg_partman is SECURITY INVOKER. Its private definer owns Bursar's payload
-- partitions, templates, and maintenance helpers, can maintain pg_partman
-- configuration, and can read the global retention policy.
-- The role is NOLOGIN; only the trusted migration principal gets SET membership.
GRANT USAGE, CREATE ON SCHEMA bursar
TO bursar_partition_runtime;
GRANT USAGE ON SCHEMA partman
TO bursar_partition_runtime;
GRANT SELECT ON TABLE bursar.storage_settings
TO bursar_partition_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE partman.part_config
TO bursar_partition_runtime;
GRANT SELECT, UPDATE, DELETE ON TABLE partman.part_config_sub
TO bursar_partition_runtime;
-- pg_partman's SECURITY INVOKER entry points call other extension helpers.
-- Grant the installed extension's complete function closure to this NOLOGIN
-- owner; its table and schema capabilities remain explicitly bounded below.
GRANT EXECUTE ON ALL ROUTINES IN SCHEMA partman
TO bursar_partition_runtime;

-- pg_partman verifies template ownership before creating each future child.
-- CREATE is needed only for this ownership transfer, not at runtime.
GRANT CREATE ON SCHEMA partman
TO bursar_partition_runtime;
ALTER TABLE partman.template_bursar_usage_charge_payloads
OWNER TO bursar_partition_runtime;
ALTER TABLE partman.template_bursar_billing_event_payloads
OWNER TO bursar_partition_runtime;
REVOKE CREATE ON SCHEMA partman
FROM bursar_partition_runtime;

-- Permit the forced-RLS partition owner to read only the global retention policy.
CREATE POLICY partition_runtime_read_storage_settings
ON bursar.storage_settings
FOR SELECT TO bursar_partition_runtime
USING (TRUE);

-- Transfer both partitioned parents and every existing child to the private DDL
-- owner so future pg_partman maintenance does not inherit migration-login authority.
ALTER TABLE bursar.usage_charge_payloads
OWNER TO bursar_partition_runtime;
ALTER TABLE bursar.billing_event_payloads
OWNER TO bursar_partition_runtime;

DO $$
DECLARE
    v_partition regclass;
BEGIN
    FOR v_partition IN
        SELECT child.oid::regclass
        FROM pg_inherits AS inheritance
        JOIN pg_class AS child
          ON child.oid = inheritance.inhrelid
        JOIN pg_class AS parent
          ON parent.oid = inheritance.inhparent
        JOIN pg_namespace AS parent_schema
          ON parent_schema.oid = parent.relnamespace
        WHERE parent_schema.nspname = 'bursar'
          AND parent.relname IN (
              'usage_charge_payloads',
              'billing_event_payloads'
          )
        ORDER BY child.oid
    LOOP
        EXECUTE format(
            'ALTER TABLE %s OWNER TO bursar_partition_runtime',
            v_partition
        );
    END LOOP;
END
$$;

-- Keep the 028 child hardener private to the partition owner and maintenance.
REVOKE ALL
ON FUNCTION bursar.secure_tenant_partition(regclass)
FROM PUBLIC, bursar_client, bursar_operator, bursar_runtime,
bursar_operator_runtime;
ALTER FUNCTION bursar.secure_tenant_partition(regclass)
OWNER TO bursar_partition_runtime;

-- Re-harden every existing child under the final owner and policy model.
SET LOCAL ROLE bursar_partition_runtime;
DO $$
DECLARE
    v_partition regclass;
BEGIN
    FOR v_partition IN
        SELECT child.oid::regclass
        FROM pg_inherits AS inheritance
        JOIN pg_class AS child
          ON child.oid = inheritance.inhrelid
        JOIN pg_class AS parent
          ON parent.oid = inheritance.inhparent
        JOIN pg_namespace AS parent_schema
          ON parent_schema.oid = parent.relnamespace
        WHERE parent_schema.nspname = 'bursar'
          AND parent.relname IN (
              'usage_charge_payloads',
              'billing_event_payloads'
          )
        ORDER BY child.oid
    LOOP
        PERFORM bursar.secure_tenant_partition(v_partition);
    END LOOP;
END
$$;
RESET ROLE;

-- ---------------------------------------------------------------------------
-- 4. Partition maintenance and operator ownership transfer
-- ---------------------------------------------------------------------------

-- pg_partman's maintenance routines are SECURITY INVOKER and require their
-- partition owner to update part_config and perform partition DDL. The private
-- base is defined under its final name in 028; expose only this parameterized
-- wrapper to the operator caller.
REVOKE ALL
ON FUNCTION bursar.run_storage_partition_maintenance_base(text, timestamptz)
FROM PUBLIC, bursar_client, bursar_operator, bursar_runtime;
GRANT EXECUTE
ON FUNCTION bursar.run_storage_partition_maintenance_base(text, timestamptz)
TO bursar_operator_runtime;
ALTER FUNCTION bursar.run_storage_partition_maintenance_base(text, timestamptz)
OWNER TO bursar_partition_runtime;

-- Expose only the parameterized maintenance call; its private base crosses into
-- the partition owner that holds pg_partman configuration and DDL capability.
CREATE FUNCTION bursar.run_storage_partition_maintenance(
    p_parent_table text,
    p_now timestamptz DEFAULT now()
)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    SELECT bursar.run_storage_partition_maintenance_base(
        p_parent_table,
        p_now
    )
$$;

REVOKE ALL
ON FUNCTION bursar.run_storage_partition_maintenance(text, timestamptz)
FROM PUBLIC, bursar_client, bursar_runtime;

-- Comment the operator wrapper while the migration principal still owns it.
-- PostgreSQL requires function ownership for COMMENT, so this must precede
-- the ownership-transfer block below.
COMMENT ON FUNCTION bursar.run_storage_partition_maintenance(
    text,
    timestamptz
) IS 'Runs bounded partition maintenance through a private partition-owner boundary.';

-- Transfer the exact operator-callable SECURITY DEFINER surface.  The caller
-- group retains EXECUTE, but it has neither table grants nor membership in the
-- owner role.
GRANT CREATE ON SCHEMA bursar TO bursar_operator_runtime;

DO $$
DECLARE
    v_function text;
    v_operator_functions constant text[] := ARRAY[
        'bursar.get_storage_settings()',
        'bursar.configure_storage(integer,integer,integer,integer,integer,integer,integer,integer,integer,integer,integer,integer,integer,integer)',
        'bursar.claim_outbox_events(integer,integer,text[])',
        'bursar.claim_outbox_events(uuid,integer,integer,text[])',
        'bursar.export_usage_charge(uuid)',
        'bursar.export_billing_event_payload(uuid)',
        'bursar.complete_outbox_event(bigint,uuid)',
        'bursar.archive_billing_event_payload(uuid,text,text)',
        'bursar.fail_outbox_event(bigint,uuid,text,integer,integer)',
        'bursar.run_storage_partition_maintenance(text,timestamptz)',
        'bursar.run_storage_maintenance(timestamptz)',
        'bursar.maybe_run_storage_maintenance(timestamptz)',
        'bursar.create_tenant(uuid,text,text)',
        'bursar.set_tenant_status(uuid,text)',
        'bursar.resolve_active_tenant_for_trigger(text)',
        'bursar.renew_tenant_outbox_claim(uuid,bigint,uuid,integer)',
        'bursar.complete_tenant_outbox_event(uuid,bigint,uuid)',
        'bursar.fail_tenant_outbox_event(uuid,bigint,uuid,text,integer,integer)',
        'bursar.get_outbox_stats(uuid)',
        'bursar.list_outbox_dead_letters(uuid,timestamptz,bigint,integer)',
        'bursar.requeue_outbox_dead_letter(uuid,bigint)'
    ];
BEGIN
    FOREACH v_function IN ARRAY v_operator_functions LOOP
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION %s TO bursar_operator',
            v_function
        );
        EXECUTE format(
            'ALTER FUNCTION %s OWNER TO bursar_operator_runtime',
            v_function
        );
    END LOOP;
END
$$;

REVOKE CREATE ON SCHEMA bursar FROM bursar_operator_runtime;

-- ---------------------------------------------------------------------------
-- 5. Owner-role catalog comments
-- ---------------------------------------------------------------------------

COMMENT ON ROLE bursar_operator_runtime IS
'NOLOGIN owner for forced-RLS cross-tenant Bursar operator RPCs.';

COMMENT ON ROLE bursar_partition_runtime IS
'NOLOGIN owner for Bursar pg_partman maintenance and payload partitions.';
