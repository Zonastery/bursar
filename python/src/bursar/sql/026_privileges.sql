-- Migration: 026_privileges.sql
-- Purpose: Establish a fail-closed schema, object, RLS, and default-ACL baseline.
-- Depends on: Every Bursar table, sequence, and function defined through 025.
-- Security: PUBLIC loses schema, table, sequence, and function access; all tables
--   enable RLS before dedicated runtime roles and forced policies are installed.

-- Remove implicit PUBLIC reachability from the schema, tables, sequences, and functions.
-- Enum type USAGE keeps PostgreSQL's default ACL; PUBLIC cannot resolve those
-- types without schema USAGE, while later caller roles use them in RPC signatures.

REVOKE ALL ON SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL TABLES IN SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA bursar FROM PUBLIC;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA bursar FROM PUBLIC;

-- Enable RLS on every existing table so missing later policies fail closed.
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_tables WHERE schemaname='bursar'
    LOOP
        EXECUTE format(
            'ALTER TABLE bursar.%I ENABLE ROW LEVEL SECURITY',
            t.tablename
        );
    END LOOP;
END $$;

-- Prevent future migration-owner tables, sequences, and functions from silently
-- regaining PostgreSQL's PUBLIC defaults inside the Bursar schema.
ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON TABLES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON SEQUENCES FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA bursar
REVOKE ALL ON FUNCTIONS FROM PUBLIC;

-- Provision the two private owners before storage and tenant security are
-- defined so later migrations can create their definer boundaries once under
-- their final names. The migration principal receives SET-only membership for
-- ownership transfer; existing cluster-global roles are verified fail closed.
DO $$
DECLARE
    v_migration_role name := current_user;
    v_owner_role name;
BEGIN
    FOREACH v_owner_role IN ARRAY ARRAY[
        'bursar_operator_runtime',
        'bursar_partition_runtime'
    ]::name[]
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname = v_owner_role
        ) THEN
            EXECUTE format(
                'CREATE ROLE %I '
                'NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                'NOINHERIT NOREPLICATION NOBYPASSRLS',
                v_owner_role
            );
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_roles
            WHERE rolname = v_owner_role
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
            RAISE EXCEPTION '% has unsafe attributes', v_owner_role
                USING ERRCODE = '55000';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_auth_members AS membership
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE member_role.rolname = v_owner_role
        ) THEN
            RAISE EXCEPTION '% must not inherit another role', v_owner_role
                USING ERRCODE = '55000';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE granted_role.rolname = v_owner_role
              AND member_role.rolname <> v_migration_role
        ) THEN
            RAISE EXCEPTION '% has an unauthorized member', v_owner_role
                USING ERRCODE = '55000';
        END IF;

        EXECUTE format(
            'GRANT %I TO %I '
            'WITH ADMIN FALSE, INHERIT FALSE, SET TRUE',
            v_owner_role,
            v_migration_role
        );

        IF NOT EXISTS (
            SELECT 1
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE granted_role.rolname = v_owner_role
              AND member_role.rolname = v_migration_role
              AND NOT membership.admin_option
              AND NOT membership.inherit_option
              AND membership.set_option
        ) THEN
            RAISE EXCEPTION 'unsafe % membership options', v_owner_role
                USING ERRCODE = '55000';
        END IF;
    END LOOP;
END
$$;

-- Remove PostgreSQL's global PUBLIC function default from both private owners.
SET LOCAL ROLE bursar_operator_runtime;
ALTER DEFAULT PRIVILEGES
REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;

SET LOCAL ROLE bursar_partition_runtime;
ALTER DEFAULT PRIVILEGES
REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
RESET ROLE;
