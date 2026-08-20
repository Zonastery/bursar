-- Migration: 029_multitenancy_security.sql
-- Purpose: Install tenant runtime roles, forced RLS, caller ACLs, and operator RPCs.
-- Depends on: Complete schema/RPC surface through 028 and host migration ownership.
-- Security: Non-login owners execute narrow SECURITY DEFINER surfaces; FORCE RLS
--   binds business rows to one active transaction tenant while operator work is separate.
--
-- Contents
--   1. Fail-closed runtime, client, and operator roles
--   2. Runtime object access and function ownership
--   3. Client RPC allow-list and host trigger boundary
--   4. Forced tenant RLS and operator lifecycle RPCs
--   5. Security catalog comments
--
-- Finalize tenant runtime security after all tables and RPCs exist.
-- Tenant columns, keys, relationships, uniqueness, and storage contracts are
-- defined in their baseline schema files. This step only installs the runtime
-- role, forced RLS, and operator tenant lifecycle RPCs.

-- ---------------------------------------------------------------------------
-- 1. Fail-closed runtime, client, and operator roles
-- ---------------------------------------------------------------------------

-- Tenant RPCs execute as a dedicated, non-BYPASSRLS owner. Applications call
-- those RPCs through the least-privilege bursar_client group role. Cross-tenant
-- maintenance is exposed separately through bursar_operator.
DO $$
DECLARE
    v_migration_role name := current_user;
    v_role name;
BEGIN
    FOREACH v_role IN ARRAY ARRAY[
        'bursar_runtime',
        'bursar_client',
        'bursar_operator'
    ]::name[]
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = v_role
        ) THEN
            EXECUTE format(
                'CREATE ROLE %I '
                'NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOINHERIT NOREPLICATION NOBYPASSRLS',
                v_role
            );
        END IF;
    END LOOP;

    -- Roles are cluster-global and can survive schema resets. Verify their
    -- complete fail-closed shape instead of silently trusting an existing role.
    IF EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname IN (
            'bursar_runtime',
            'bursar_client',
            'bursar_operator'
        )
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
        RAISE EXCEPTION 'a Bursar role has unsafe attributes'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_auth_members AS membership
        JOIN pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE member_role.rolname IN (
            'bursar_runtime',
            'bursar_client',
            'bursar_operator'
        )
    ) THEN
        RAISE EXCEPTION 'Bursar roles must not inherit from other roles'
            USING ERRCODE = '55000';
    END IF;

    -- bursar_runtime owns tenant RPCs and may never be assumed by a host
    -- principal. Only the migration owner receives SET permission so it can
    -- transfer and maintain Bursar-owned objects.
    IF EXISTS (
        SELECT 1
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted_role
          ON granted_role.oid = membership.roleid
        JOIN pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE granted_role.rolname = 'bursar_runtime'
          AND member_role.rolname <> v_migration_role
    ) THEN
        RAISE EXCEPTION 'bursar_runtime has an unauthorized member'
            USING ERRCODE = '55000';
    END IF;

    -- Host login principals may assume exactly one caller role. Membership is
    -- SET-only so every transaction has to opt into Bursar's narrow RPC ACL;
    -- unsafe PostgreSQL attributes remain a hard deployment failure.
    IF EXISTS (
        SELECT 1
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted_role
          ON granted_role.oid = membership.roleid
        JOIN pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE granted_role.rolname IN ('bursar_client', 'bursar_operator')
          AND member_role.rolname <> v_migration_role
          AND (
              NOT member_role.rolcanlogin
              OR member_role.rolsuper
              OR member_role.rolcreatedb
              OR member_role.rolcreaterole
              OR member_role.rolreplication
              OR member_role.rolbypassrls
              OR membership.admin_option
              OR membership.inherit_option
              OR NOT membership.set_option
          )
    ) THEN
        RAISE EXCEPTION 'a Bursar caller principal or membership is unsafe'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT membership.member
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted_role
          ON granted_role.oid = membership.roleid
        JOIN pg_roles AS member_role
          ON member_role.oid = membership.member
        WHERE granted_role.rolname IN ('bursar_client', 'bursar_operator')
          AND member_role.rolname <> v_migration_role
        GROUP BY membership.member
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'a Bursar caller principal has conflicting roles'
            USING ERRCODE = '55000';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_auth_members AS membership
        JOIN pg_roles AS granted_role
          ON granted_role.oid = membership.roleid
        JOIN pg_roles AS member_role
          ON member_role.oid = membership.member
        JOIN pg_roles AS migration_role
          ON migration_role.rolname = v_migration_role
        WHERE granted_role.rolname IN ('bursar_client', 'bursar_operator')
          AND member_role.rolname <> v_migration_role
          AND pg_has_role(member_role.oid, migration_role.oid, 'MEMBER')
    ) THEN
        RAISE EXCEPTION 'a Bursar caller principal can assume the migration owner'
            USING ERRCODE = '55000';
    END IF;

    -- Object ownership transfer requires SET permission on the destination
    -- role. The migration connection may SET the caller roles explicitly,
    -- so one trusted deployment DSN remains a complete default setup without
    -- leaking operator authority into ordinary transactions.
    EXECUTE format(
        'GRANT bursar_runtime TO %I WITH INHERIT FALSE, SET TRUE',
        v_migration_role
    );
    EXECUTE format(
        'GRANT bursar_client TO %I WITH INHERIT FALSE, SET TRUE',
        v_migration_role
    );
    EXECUTE format(
        'GRANT bursar_operator TO %I WITH INHERIT FALSE, SET TRUE',
        v_migration_role
    );
END
$$;

-- ---------------------------------------------------------------------------
-- 2. Runtime object access and function ownership
-- ---------------------------------------------------------------------------

-- PostgreSQL grants EXECUTE on newly created functions to PUBLIC through the
-- global default ACL. bursar_runtime can create functions only in the bursar
-- schema, so make that owner role fail closed before granting schema CREATE.
-- A schema-scoped default REVOKE cannot subtract the global PUBLIC grant.
SET LOCAL ROLE bursar_runtime;
ALTER DEFAULT PRIVILEGES
REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;

-- Clear every direct client/operator object capability before rebuilding the
-- runtime and caller allow-lists from explicit schema, table, and function grants.
REVOKE ALL ON SCHEMA bursar FROM bursar_client, bursar_operator;
REVOKE ALL ON ALL TABLES IN SCHEMA bursar
FROM bursar_client, bursar_operator;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA bursar
FROM bursar_client, bursar_operator;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA bursar
FROM bursar_client, bursar_operator;

-- Give the runtime owner only the schemas and extension primitives required by
-- tenant RPC bodies; schema CREATE is revoked after ownership transfer.
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

-- Grant CRUD only on tenant-bearing parent tables (plus tenants); FORCE RLS below
-- remains authoritative for which active tenant rows the owner may see or write.
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

-- The global storage singleton is read-only to tenant runtime functions.
GRANT SELECT ON bursar.storage_settings TO bursar_runtime;
-- storage_settings is a global (non-tenant) singleton, but 026 blanket-enables
-- RLS on every bursar table. Without an explicit runtime policy the SELECT grant
-- above returns zero rows for the NOBYPASSRLS bursar_runtime role, silently
-- NULLing the storage config that the SECURITY INVOKER quota validators
-- (check_quota_usage_event, validate_catalog_plan_quota) read directly -- which
-- would disable the event-lateness and rolling-quota-retention guards. No tenant
-- data lives here, so grant runtime read of this operator-global config.
-- Permit the non-bypass runtime owner to read the non-tenant singleton under RLS.
CREATE POLICY storage_settings_runtime_read ON bursar.storage_settings
FOR SELECT TO bursar_runtime
USING (TRUE);
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA bursar TO bursar_runtime;

-- Runtime-owned SECURITY DEFINER RPCs may call ordinary validation, policy,
-- trigger, and calculation helpers. Grant only those SECURITY INVOKER
-- dependencies. Cross-tenant storage and pg_partman maintenance deliberately
-- remain unavailable to the tenant runtime role.
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA bursar FROM bursar_runtime;

-- Grant execution of ordinary invoker dependencies called from runtime-owned
-- definer functions; table privileges and RLS still constrain their data access.
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

-- Transfer all tenant SECURITY DEFINER functions except the explicitly reserved
-- storage, operator, partition, and host-integration surface.
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
          AND function_info.proowner = current_user::regrole
          AND function_info.proname NOT IN (
              'get_storage_settings',
              'configure_storage',
              'claim_outbox_events',
              'export_usage_charge',
              'export_billing_event_payload',
              'complete_outbox_event',
              'archive_billing_event_payload',
              'fail_outbox_event',
              'secure_tenant_partition',
              'run_storage_partition_maintenance_base',
              'run_storage_maintenance',
              'maybe_run_storage_maintenance',
              'renew_tenant_outbox_claim',
              'complete_tenant_outbox_event',
              'fail_tenant_outbox_event',
              'get_outbox_stats',
              'list_outbox_dead_letters',
              'requeue_outbox_dead_letter',
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

-- ---------------------------------------------------------------------------
-- 3. Client RPC allow-list and host trigger boundary
-- ---------------------------------------------------------------------------

-- Only the documented, tenant-scoped RPC surface is callable by application
-- connections. Runtime-owned helpers remain private even though tenant RPCs
-- need them internally through SECURITY DEFINER execution.
GRANT USAGE ON SCHEMA bursar TO bursar_client;

-- The migration role has SET-only membership in the runtime owner role. Run
-- this grant as the actual function owner, then restore the migration role for
-- the migration-owned integration helpers below.
SET LOCAL ROLE bursar_runtime;

-- Grant the fixed, signature-qualified SDK surface; helpers remain runtime-only.
DO $$
DECLARE
    v_function text;
    v_client_functions constant text[] := ARRAY[
        'bursar.publish_and_activate_catalog(integer,jsonb,text,boolean,jsonb)',
        'bursar.catalog_revision_by_number(bigint)',
        'bursar.list_catalog_revisions(integer)',
        'bursar.activate_catalog_revision(bigint,jsonb)',
        'bursar.active_catalog_revision()',
        'bursar.execute_grant_program(text,text,uuid,text,uuid,text,jsonb)',
        'bursar.post_credit(uuid,bursar.ledger_entry_kind,numeric,text,text,jsonb,text,uuid,timestamptz,numeric)',
        'bursar.record_usage(uuid,text,numeric,text,text,text,text,jsonb,jsonb,jsonb)',
        'bursar.charge_usage_for_operation(uuid,text,numeric,text,text,text,text,jsonb,jsonb,jsonb)',
        'bursar.refund_credit_by_entry(uuid,numeric,text,text,jsonb)',
        'bursar.revoke_subject_credits_by_operation(uuid,text)',
        'bursar.deduct_team(uuid,uuid,numeric,text,text,jsonb)',
        'bursar.create_team(uuid,text,text,numeric)',
        'bursar.set_team_member(uuid,uuid,text,numeric)',
        'bursar.remove_team_member(uuid,uuid)',
        'bursar.list_team_members(uuid)',
        'bursar.get_team_balance(uuid)',
        'bursar.grant_billing_credit(uuid,text)',
        'bursar.post_billing_refund(uuid,uuid,bigint,text)',
        'bursar.create_lease_for_operation(uuid,text,numeric,text,interval,jsonb,text,jsonb,jsonb,numeric,integer)',
        'bursar.settle_lease(uuid,uuid,numeric,text,text,text,text,jsonb,jsonb,jsonb)',
        'bursar.renew_lease(uuid,uuid,interval)',
        'bursar.release_lease(uuid,uuid)',
        'bursar.sweep_expired_lots(integer,uuid,boolean)',
        'bursar.expire_leases(integer)',
        'bursar.revoke_lot(uuid,numeric,text)',
        'bursar.claim_billing_event(text,text,text,jsonb,integer,integer)',
        'bursar.complete_billing_event(text,text,uuid)',
        'bursar.fail_billing_event(text,text,uuid,text)',
        'bursar.record_subscription_conflict(uuid,text,text,text,text,jsonb)',
        'bursar.pseudonymize_financial_subject(uuid)',
        'bursar.claim_auto_recharge_attempt(uuid,text)',
        'bursar.advance_auto_recharge_attempt(uuid,bursar.recharge_attempt_status,text,text,text,jsonb)',
        'bursar.upsert_billing_customer(uuid,text,text,text)',
        'bursar.upsert_billing_subscription(uuid,text,text,text,uuid,bursar.billing_subscription_status,timestamptz,timestamptz,boolean,jsonb,timestamptz,timestamptz,timestamptz,timestamptz,timestamptz)',
        'bursar.upsert_billing_payment(uuid,text,text,bigint,bigint,text,text,bursar.billing_payment_status,timestamptz,text,jsonb)',
        'bursar.create_billing_credit_grant(uuid,uuid,uuid,numeric,integer,uuid)',
        'bursar.upsert_billing_refund(uuid,text,bigint,text,text,timestamptz,uuid,text,jsonb)',
        'bursar.upsert_auto_recharge_profile(uuid,boolean,text,uuid,integer,numeric,integer,text,integer,text,text,boolean,text,boolean)',
        'bursar.upsert_billing_preferences(uuid,boolean,boolean,boolean,boolean,boolean)',
        'bursar.upsert_billing_invoice(uuid,text,text,uuid,text,bigint,bigint,text,timestamptz,timestamptz,jsonb,timestamptz)',
        'bursar.upsert_billing_dispute(text,text,uuid,text,text,jsonb,timestamptz)',
        'bursar.create_checkout_intent(uuid,text,text,text,text,bytea,timestamptz,text,text,text)',
        'bursar.advance_checkout_intent(uuid,text,text,text)',
        'bursar.set_subject_plan(uuid,text,timestamptz)',
        'bursar.unassign_plan(uuid,text)',
        'bursar.reconcile_subscription_entitlement(uuid,uuid,uuid,bursar.billing_subscription_status,timestamptz,timestamptz,boolean,text,text)',
        'bursar.expire_subscription_grace_period(uuid,uuid,timestamptz,timestamptz,text)',
        'bursar.set_plan_revision_pin(uuid,boolean)',
        'bursar.start_plan_migration(uuid,uuid)',
        'bursar.migrate_plan_batch(uuid,integer)',
        'bursar.open_subscription_change(uuid,uuid,timestamptz,text,text,text)',
        'bursar.advance_subscription_change(bigint,text,text,text)',
        'bursar.apply_due_plan_assignment_changes(integer)',
        'bursar.get_credit_bucket_balances(uuid)',
        'bursar.get_credit_state(uuid)',
        'bursar.get_credit_operation_details(uuid,uuid,text)',
        'bursar.get_credit_grant_details(uuid,uuid)',
        'bursar.get_credit_lease(uuid,uuid)',
        'bursar.get_credit_lease_pricing_context(uuid,uuid)',
        'bursar.get_subject_plan(uuid)',
        'bursar.get_subject_allowance(uuid,timestamptz)',
        'bursar.get_subject_entitlements(uuid,timestamptz)',
        'bursar.get_subject_quota_state(uuid,text)',
        'bursar.list_subject_quota_events(uuid,timestamptz,integer,text,uuid)',
        'bursar.get_billing_customer(uuid,text)',
        'bursar.get_billing_customer_by_provider(text,text)',
        'bursar.get_billing_subscription_by_provider(text,text)',
        'bursar.list_billing_subscriptions(uuid)',
        'bursar.list_expired_grace_subscriptions(timestamptz,integer)',
        'bursar.get_billing_payment_by_provider(text,text)',
        'bursar.get_billing_preferences(uuid)',
        'bursar.get_auto_recharge_profile(uuid)',
        'bursar.get_auto_recharge_attempt(uuid)',
        'bursar.get_auto_recharge_attempt_by_provider(text,text)',
        'bursar.count_auto_recharge_attempts(uuid,timestamptz)',
        'bursar.resolve_catalog_offer(text,text,text)',
        'bursar.resolve_active_catalog_offer(text)',
        'bursar.get_catalog_offer_context(uuid,uuid)',
        'bursar.resolve_catalog_topup(text,text,text)',
        'bursar.resolve_catalog_plan(text,text,text)',
        'bursar.get_checkout_intent(uuid,uuid)',
        'bursar.get_open_billing_subscription_change(text,text)',
        'bursar.get_billing_subscription_change(bigint)',
        'bursar.get_billing_credit_grant_by_payment(uuid)',
        'bursar.list_billing_invoices(uuid,timestamptz,uuid,integer)',
        'bursar.list_ledger(uuid,timestamptz,uuid,integer,text[],timestamptz,timestamptz,boolean)',
        'bursar.list_usage_charges(uuid,timestamptz,uuid,integer,timestamptz,timestamptz,boolean)',
        'bursar.get_ledger_entry(uuid,uuid)',
        'bursar.spend_by_user(timestamptz,timestamptz)',
        'bursar.spend_by_model(timestamptz,timestamptz)',
        'bursar.daily_spend(timestamptz,timestamptz)',
        'bursar.aggregate_usage_stats(timestamptz,timestamptz)'
    ];
BEGIN
    FOREACH v_function IN ARRAY v_client_functions LOOP
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION %s TO bursar_client',
            v_function
        );
    END LOOP;
END
$$;

RESET ROLE;

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

-- End the temporary ownership-transfer capability before serving application calls.
REVOKE CREATE ON SCHEMA bursar FROM bursar_runtime;

-- ---------------------------------------------------------------------------
-- 4. Forced tenant RLS and operator lifecycle RPCs
-- ---------------------------------------------------------------------------

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

-- The tenant registry exposes only the transaction tenant itself to runtime RPCs.
ALTER TABLE bursar.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE bursar.tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self_read ON bursar.tenants
FOR SELECT TO bursar_runtime
USING (id = (SELECT bursar.current_tenant_id()));

-- Provision or replay one global tenant slug without permitting ID reassignment.
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
    IF NOT bursar.is_bounded_text(p_slug, 100)
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
    VALUES (
        COALESCE(p_tenant_id, bursar.uuid_v7()),
        lower(btrim(p_slug)),
        p_display_name
    )
    ON CONFLICT (slug) DO UPDATE
    SET display_name = EXCLUDED.display_name
    WHERE p_tenant_id IS NULL
       OR existing.id = EXCLUDED.id
    RETURNING id INTO v_id;

    IF v_id IS NULL THEN
        RAISE EXCEPTION 'tenant slug is already assigned to another id'
            USING ERRCODE = '23505';
    END IF;

    RETURN v_id;
END
$$;

-- Apply one allow-listed tenant lifecycle status by exact tenant identity.
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

-- Close both global tenant lifecycle functions before the operator grant below.
REVOKE ALL ON FUNCTION bursar.create_tenant(uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION bursar.set_tenant_status(uuid, text) FROM PUBLIC;

-- Cross-tenant workers and deployment tooling use a distinct caller role. The
-- exact function allow-list is granted while ownership is transferred in 030.
GRANT USAGE ON SCHEMA bursar TO bursar_operator;

-- ---------------------------------------------------------------------------
-- 5. Security catalog comments
-- ---------------------------------------------------------------------------

COMMENT ON TABLE bursar.tenants IS
'SaaS tenant boundary for all Bursar catalog, credit, billing, and usage data.';

COMMENT ON FUNCTION bursar.current_tenant_id() IS
'Returns the tenant UUID explicitly bound to the current transaction.';

COMMENT ON FUNCTION bursar.require_tenant_id() IS
'Returns the current tenant UUID and fails closed when no tenant is bound.';

COMMENT ON FUNCTION bursar.current_tenant_is_active() IS
'Returns true only when the current tenant exists and is active.';

COMMENT ON FUNCTION bursar.claim_outbox_events(integer, integer, text []) IS
'Claims cross-tenant outbox work and returns the owning tenant UUID.';

COMMENT ON FUNCTION bursar.claim_outbox_events(uuid, integer, integer, text []) IS
'Claims outbox work for one active tenant and returns the owning tenant UUID.';

COMMENT ON FUNCTION bursar.create_tenant(uuid, text, text) IS
'Operator-only idempotent tenant provisioning RPC.';

COMMENT ON FUNCTION bursar.set_tenant_status(uuid, text) IS
'Operator-only tenant activation, suspension, and closure RPC.';
