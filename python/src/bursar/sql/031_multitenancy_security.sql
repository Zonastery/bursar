-- Finalize tenant runtime security after all tables and RPCs exist.
-- Tenant columns, keys, relationships, uniqueness, and storage contracts are
-- defined in their baseline schema files. This step only installs the runtime
-- role, forced RLS, partition policy helper, and operator tenant lifecycle RPCs.

-- Tenant RPCs execute as a dedicated, non-BYPASSRLS owner. This is essential
-- on Supabase, where service_role and postgres bypass RLS. Operator/storage
-- functions retain their migration owner and are never granted to tenant
-- callers.
DO $$
DECLARE
    v_migration_role name := current_user;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'bursar_runtime'
    ) THEN
        CREATE ROLE bursar_runtime
        NOLOGIN
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOINHERIT
        NOBYPASSRLS;
    END IF;

    -- Roles are cluster-global and can survive schema resets. A managed
    -- Supabase migration role is intentionally not a superuser, so it cannot
    -- reassert NOSUPERUSER/NOREPLICATION/NOBYPASSRLS with ALTER ROLE. Verify
    -- the complete fail-closed shape instead of requiring forbidden authority.
    IF EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'bursar_runtime'
          AND (
              rolcanlogin
              OR rolsuper
              OR rolcreatedb
              OR rolcreaterole
              OR rolinherit
              OR rolreplication
              OR rolbypassrls
          )
    ) THEN
        RAISE EXCEPTION 'bursar_runtime has unsafe role attributes'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_auth_members AS membership
        JOIN pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE member_role.rolname = 'bursar_runtime'
    ) THEN
        RAISE EXCEPTION 'bursar_runtime must not inherit from another role'
            USING ERRCODE = '55000';
    END IF;

    -- PostgreSQL 16+ gives a non-superuser CREATEROLE caller ADMIN over a
    -- newly-created role but not SET by default. Object ownership transfer
    -- requires SET permission on the destination role, so grant only that
    -- membership option explicitly. This is required by managed Supabase,
    -- whose postgres role intentionally is not a superuser.
    EXECUTE format(
        'GRANT bursar_runtime TO %I WITH INHERIT FALSE, SET TRUE',
        v_migration_role
    );
END
$$;

GRANT USAGE ON SCHEMA bursar TO bursar_runtime;
GRANT CREATE ON SCHEMA bursar TO bursar_runtime;
GRANT USAGE ON SCHEMA extensions TO bursar_runtime;
GRANT EXECUTE
ON FUNCTION extensions.digest(bytea, text)
TO bursar_runtime;
GRANT EXECUTE
ON FUNCTION extensions.gen_random_bytes(integer)
TO bursar_runtime;
GRANT EXECUTE
ON FUNCTION extensions.jsonb_matches_schema(json, jsonb)
TO bursar_runtime;
GRANT EXECUTE
ON FUNCTION extensions.jsonschema_is_valid(json)
TO bursar_runtime;
GRANT EXECUTE
ON FUNCTION extensions.jsonschema_validation_errors(json, json)
TO bursar_runtime;

DO $$
DECLARE
    v_table record;
BEGIN
    FOR v_table IN
        SELECT table_info.oid::regclass AS table_name
        FROM pg_class AS table_info
        JOIN pg_namespace AS namespace_info
          ON namespace_info.oid = table_info.relnamespace
        WHERE namespace_info.nspname = 'bursar'
          AND table_info.relkind IN ('r', 'p')
          AND NOT table_info.relispartition
          AND (
              EXISTS (
                  SELECT 1
                  FROM pg_attribute AS attribute_info
                  WHERE attribute_info.attrelid = table_info.oid
                    AND attribute_info.attname = 'tenant_id'
                    AND NOT attribute_info.attisdropped
              )
              OR table_info.relname = 'tenants'
          )
    LOOP
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE '
            'ON TABLE %s TO bursar_runtime',
            v_table.table_name
        );
    END LOOP;
END
$$;

GRANT SELECT ON bursar.storage_settings TO bursar_runtime;
-- storage_settings is a global (non-tenant) singleton, but 026 blanket-enables
-- RLS on every bursar table. Without an explicit runtime policy the SELECT grant
-- above returns zero rows for the NOBYPASSRLS bursar_runtime role, silently
-- NULLing the storage config that the SECURITY INVOKER quota validators
-- (check_quota_usage_event, validate_catalog_plan_quota) read directly -- which
-- would disable the event-lateness and rolling-quota-retention guards. No tenant
-- data lives here, so grant runtime read of this operator-global config.
CREATE POLICY storage_settings_runtime_read ON bursar.storage_settings
FOR SELECT TO bursar_runtime
USING (TRUE);
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bursar TO bursar_runtime;

-- Runtime-owned SECURITY DEFINER RPCs may call ordinary validation, policy,
-- trigger, and calculation helpers. Grant only those SECURITY INVOKER
-- dependencies. Cross-tenant storage and pg_partman maintenance deliberately
-- remain unavailable to the tenant runtime role.
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA bursar FROM bursar_runtime;

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
            'GRANT EXECUTE ON FUNCTION %s TO bursar_runtime',
            v_function.function_name
        );
    END LOOP;
END
$$;

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
          AND function_info.prosecdef
          AND function_info.proname NOT IN (
              'get_storage_settings',
              'configure_storage',
              'claim_outbox_events',
              'export_usage_charge',
              'export_billing_event_payload',
              'complete_outbox_event',
              'archive_billing_event_payload',
              'fail_outbox_event',
              'run_storage_partition_maintenance',
              'run_storage_maintenance',
              'maybe_run_storage_maintenance',
              'resolve_active_tenant_for_trigger',
              'create_tenant'
          )
        ORDER BY function_info.oid
    LOOP
        EXECUTE format(
            'ALTER FUNCTION %s OWNER TO bursar_runtime',
            v_function.function_name
        );
    END LOOP;
END
$$;

-- The runtime-owned trigger is the stable host-integration API. Its only
-- cross-tenant capability is resolving one active tenant slug through the
-- migration-owned helper; all catalog reads and mutations then run with
-- transaction-local tenant context under forced RLS.
REVOKE ALL
ON FUNCTION bursar.resolve_active_tenant_for_trigger(text)
FROM PUBLIC;

GRANT EXECUTE
ON FUNCTION bursar.resolve_active_tenant_for_trigger(text)
TO bursar_runtime;

-- PostgreSQL checks EXECUTE when the host creates its trigger, not when that
-- trigger later fires. Grant the trusted migration session just enough access
-- to attach the runtime-owned hook. SET ROLE is required because the migration
-- role has SET-only membership in bursar_runtime and therefore does not inherit
-- ownership privileges.
SET LOCAL ROLE bursar_runtime;

DO $$
BEGIN
    EXECUTE format(
        'GRANT EXECUTE ON FUNCTION '
        || 'bursar.provision_subject_account_on_insert() TO %I',
        session_user
    );
END
$$;

RESET ROLE;

REVOKE CREATE ON SCHEMA bursar FROM bursar_runtime;

-- Apply tenant RLS to every business table and every partition created during
-- pg_partman registration. The maintenance wrapper secures future children.
DO $$
DECLARE
    v_table record;
BEGIN
    FOR v_table IN
        SELECT table_info.relname
        FROM pg_class AS table_info
        JOIN pg_namespace AS namespace_info
          ON namespace_info.oid = table_info.relnamespace
        WHERE namespace_info.nspname = 'bursar'
          AND table_info.relkind IN ('r', 'p')
          AND EXISTS (
              SELECT 1
              FROM pg_attribute AS attribute_info
              WHERE attribute_info.attrelid = table_info.oid
                AND attribute_info.attname = 'tenant_id'
                AND NOT attribute_info.attisdropped
          )
        ORDER BY table_info.relname
    LOOP
        EXECUTE format(
            'ALTER TABLE bursar.%I ENABLE ROW LEVEL SECURITY',
            v_table.relname
        );
        EXECUTE format(
            'ALTER TABLE bursar.%I FORCE ROW LEVEL SECURITY',
            v_table.relname
        );
        EXECUTE format(
            'CREATE POLICY %I ON bursar.%I '
            'FOR ALL TO bursar_runtime '
            'USING ('
            'tenant_id = (SELECT bursar.current_tenant_id()) '
            'AND (SELECT bursar.current_tenant_is_active())'
            ') '
            'WITH CHECK ('
            'tenant_id = (SELECT bursar.current_tenant_id()) '
            'AND (SELECT bursar.current_tenant_is_active())'
            ')',
            left('tenant_isolation_' || v_table.relname, 63),
            v_table.relname
        );
    END LOOP;
END
$$;

ALTER TABLE bursar.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self_read ON bursar.tenants
FOR SELECT TO bursar_runtime
USING (id = (SELECT bursar.current_tenant_id()));

CREATE FUNCTION bursar.secure_tenant_partition(p_partition regclass)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_partition text;
    v_parent text;
    v_policy text;
BEGIN
    SELECT
        child.relname,
        parent.relname
    INTO v_partition, v_parent
    FROM pg_inherits
    JOIN pg_class AS child
      ON child.oid = pg_inherits.inhrelid
    JOIN pg_namespace AS child_schema
      ON child_schema.oid = child.relnamespace
    JOIN pg_class AS parent
      ON parent.oid = pg_inherits.inhparent
    JOIN pg_namespace AS parent_schema
      ON parent_schema.oid = parent.relnamespace
    WHERE child.oid = p_partition
      AND child_schema.nspname = 'bursar'
      AND parent_schema.nspname = 'bursar'
      AND parent.relname IN (
          'usage_charge_payloads',
          'billing_event_payloads'
      );

    IF NOT FOUND THEN
        RAISE EXCEPTION 'invalid tenant partition'
            USING ERRCODE = '22023';
    END IF;

    v_policy := left('tenant_isolation_' || v_partition, 63);

    EXECUTE format(
        'ALTER TABLE bursar.%I ENABLE ROW LEVEL SECURITY',
        v_partition
    );
    EXECUTE format(
        'ALTER TABLE bursar.%I FORCE ROW LEVEL SECURITY',
        v_partition
    );
    EXECUTE format(
        'REVOKE ALL ON TABLE bursar.%I FROM PUBLIC, bursar_runtime',
        v_partition
    );
    EXECUTE format(
        'COMMENT ON TABLE bursar.%I IS %L',
        v_partition,
        format(
            'pg_partman-managed child of bursar.%I; direct access is denied.',
            v_parent
        )
    );

    IF NOT EXISTS (
        SELECT 1
        FROM pg_policy
        WHERE polrelid = p_partition
          AND polname = v_policy
    ) THEN
        EXECUTE format(
            'CREATE POLICY %I ON bursar.%I '
            'FOR ALL TO bursar_runtime '
            'USING ('
            'tenant_id = (SELECT bursar.current_tenant_id()) '
            'AND (SELECT bursar.current_tenant_is_active())'
            ') '
            'WITH CHECK ('
            'tenant_id = (SELECT bursar.current_tenant_id()) '
            'AND (SELECT bursar.current_tenant_is_active())'
            ')',
            v_policy,
            v_partition
        );
    END IF;
END
$$;

REVOKE ALL
ON FUNCTION bursar.secure_tenant_partition(regclass)
FROM PUBLIC;

-- The initial pg_partman children predate this helper. Re-apply the helper so
-- they receive the same privileges, forced RLS policy, and documentation as
-- children created by later maintenance runs.
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

CREATE FUNCTION bursar.create_tenant(
    p_tenant_id uuid,
    p_slug text,
    p_display_name text DEFAULT NULL
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO ''
AS $$
DECLARE
    v_id uuid;
BEGIN
    IF p_tenant_id IS NULL
       OR NOT bursar.is_bounded_text(p_slug, 100)
       OR lower(btrim(p_slug))
          !~ '^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$'
       OR (
           p_display_name IS NOT NULL
           AND NOT bursar.is_bounded_text(p_display_name, 255)
       )
    THEN
        RAISE EXCEPTION 'invalid tenant'
            USING ERRCODE = '22023';
    END IF;

    INSERT INTO bursar.tenants AS existing(id, slug, display_name)
    VALUES (p_tenant_id, lower(btrim(p_slug)), p_display_name)
    ON CONFLICT (slug) DO UPDATE
    SET display_name = EXCLUDED.display_name
    WHERE existing.id = EXCLUDED.id
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        RAISE EXCEPTION 'tenant slug is already assigned to another id'
            USING ERRCODE = '23505';
    END IF;

    RETURN v_id;
END
$$;

CREATE FUNCTION bursar.set_tenant_status(
    p_tenant_id uuid,
    p_status text
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path TO ''
AS $$
    UPDATE bursar.tenants
    SET status = p_status
    WHERE id = p_tenant_id
      AND p_status IN ('active', 'suspended', 'closed')
    RETURNING true
$$;

REVOKE ALL ON FUNCTION bursar.create_tenant(uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.set_tenant_status(uuid, text) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'service_role'
    ) THEN
        GRANT EXECUTE
        ON FUNCTION bursar.claim_outbox_events(integer, integer, text[])
        TO service_role;
        GRANT EXECUTE
        ON FUNCTION bursar.claim_outbox_events(uuid, integer, integer, text[])
        TO service_role;
        GRANT EXECUTE
        ON FUNCTION bursar.create_tenant(uuid, text, text)
        TO service_role;
        GRANT EXECUTE
        ON FUNCTION bursar.set_tenant_status(uuid, text)
        TO service_role;
    END IF;
END
$$;

COMMENT ON TABLE bursar.tenants IS
'SaaS tenant boundary for all Bursar catalog, credit, billing, and usage data.';

COMMENT ON FUNCTION bursar.current_tenant_id() IS
'Returns the transaction-bound tenant UUID or trusted JWT app_metadata tenant UUID.';

COMMENT ON FUNCTION bursar.require_tenant_id() IS
'Returns the current tenant UUID and fails closed when no tenant is bound.';

COMMENT ON FUNCTION bursar.current_tenant_is_active() IS
'Returns true only when the current tenant exists and is active.';

COMMENT ON FUNCTION bursar.secure_tenant_partition(regclass) IS
'Revokes direct access and applies forced tenant RLS to a managed partition.';

COMMENT ON FUNCTION bursar.claim_outbox_events(integer, integer, text []) IS
'Claims cross-tenant outbox work and returns the owning tenant UUID.';

COMMENT ON FUNCTION bursar.claim_outbox_events(uuid, integer, integer, text []) IS
'Claims outbox work for one active tenant and returns the owning tenant UUID.';

COMMENT ON FUNCTION bursar.create_tenant(uuid, text, text) IS
'Operator-only idempotent tenant provisioning RPC.';

COMMENT ON FUNCTION bursar.set_tenant_status(uuid, text) IS
'Operator-only tenant activation, suspension, and closure RPC.';
