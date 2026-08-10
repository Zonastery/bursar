"""Static and database-level checks for the bundled SQL contract."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import psycopg2
import pytest

from bursar.sql import _get_sql_files

pytestmark = [pytest.mark.integration]
SQL_DIR = Path(__file__).parents[1] / "src" / "bursar" / "sql"


def test_migration_files_are_contiguous_and_self_contained() -> None:
    files = _get_sql_files()
    prefixes = [int(path.stem.split("_", 1)[0]) for path in files]

    assert prefixes == list(range(1, len(files) + 1))
    assert len({path.name for path in files}) == len(files)
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert content.strip(), path
        assert content.endswith("\n"), path


def test_checkout_rpc_owner_transfer_uses_a_transaction_scoped_schema_grant() -> None:
    migration_sql = (SQL_DIR / "030_checkout_operation_idempotency.sql").read_text(encoding="utf-8")

    grant_offset = migration_sql.index("GRANT CREATE ON SCHEMA bursar TO bursar_runtime;")
    owner_offset = migration_sql.index(
        "OWNER TO bursar_runtime;",
        migration_sql.index("ALTER FUNCTION bursar.create_checkout_intent("),
    )
    revoke_offset = migration_sql.index("REVOKE CREATE ON SCHEMA bursar FROM bursar_runtime;")

    assert grant_offset < owner_offset < revoke_offset
    assert migration_sql.count("GRANT CREATE ON SCHEMA bursar TO bursar_runtime;") == 1


def test_migration_ledger_exactly_matches_the_greenfield_baseline(
    pg_database_url: str,
) -> None:
    expected = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest()) for path in _get_sql_files()]

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version, checksum FROM bursar.schema_migrations ORDER BY version")
        assert cursor.fetchall() == expected


def test_bursar_caller_roles_are_least_privilege_and_public_is_revoked(
    pg_database_url: str,
) -> None:
    security_sql = (SQL_DIR / "029_multitenancy_security.sql").read_text(encoding="utf-8")
    client_block = re.search(
        r"v_client_functions constant text\[\] := ARRAY\[(.*?)\n\s*\];",
        security_sql,
        re.DOTALL,
    )
    assert client_block is not None
    client_signatures = sorted(set(re.findall(r"'(bursar\.[^']+\([^']*\))'", client_block.group(1))))
    # Migration 030 replaces the checkout RPC after the baseline security
    # migration has transferred function ownership. Verify the current
    # signature rather than requiring the intentionally dropped predecessor.
    checkout_signature_v1 = "bursar.create_checkout_intent(uuid,text,text,text,bytea,timestamptz,text,text,text)"
    checkout_signature_v2 = "bursar.create_checkout_intent(uuid,text,text,text,text,bytea,timestamptz,text,text,text)"
    client_signatures = sorted(
        checkout_signature_v2 if signature == checkout_signature_v1 else signature for signature in client_signatures
    )
    assert client_signatures

    operator_signatures = (
        "bursar.get_storage_settings()",
        (
            "bursar.configure_storage(integer,integer,integer,integer,integer,"
            "integer,integer,integer,integer,integer,integer,integer,integer,integer)"
        ),
        "bursar.claim_outbox_events(integer,integer,text[])",
        "bursar.claim_outbox_events(uuid,integer,integer,text[])",
        "bursar.export_usage_charge(uuid)",
        "bursar.export_billing_event_payload(uuid)",
        "bursar.complete_outbox_event(bigint,uuid)",
        "bursar.archive_billing_event_payload(uuid,text,text)",
        "bursar.fail_outbox_event(bigint,uuid,text,integer,integer)",
        "bursar.run_storage_partition_maintenance(text,timestamptz)",
        "bursar.run_storage_maintenance(timestamptz)",
        "bursar.maybe_run_storage_maintenance(timestamptz)",
        "bursar.create_tenant(uuid,text,text)",
        "bursar.set_tenant_status(uuid,text)",
    )

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        for signature in client_signatures:
            cursor.execute(
                "SELECT has_function_privilege('bursar_client', %s, 'EXECUTE')",
                (signature,),
            )
            assert cursor.fetchone() == (True,), signature

        for signature in operator_signatures:
            cursor.execute(
                """
                SELECT
                    has_function_privilege('bursar_operator', %s, 'EXECUTE'),
                    has_function_privilege('bursar_client', %s, 'EXECUTE'),
                    has_function_privilege('bursar_runtime', %s, 'EXECUTE')
                """,
                (signature, signature, signature),
            )
            assert cursor.fetchone() == (True, False, False), signature

        trigger_tenant_resolver = "bursar.resolve_active_tenant_for_trigger(text)"
        cursor.execute(
            """
            SELECT
                has_function_privilege('bursar_operator', %s, 'EXECUTE'),
                has_function_privilege('bursar_client', %s, 'EXECUTE'),
                has_function_privilege('bursar_runtime', %s, 'EXECUTE')
            """,
            (
                trigger_tenant_resolver,
                trigger_tenant_resolver,
                trigger_tenant_resolver,
            ),
        )
        assert cursor.fetchone() == (True, False, True)

        for signature in (
            "bursar.require_internal_mutation()",
            "bursar.bucket_expiry_at(uuid,uuid,text)",
            "bursar.provision_subject_account_on_insert()",
            "bursar.secure_tenant_partition(regclass)",
        ):
            cursor.execute(
                """
                SELECT
                    has_function_privilege('bursar_client', %s, 'EXECUTE'),
                    has_function_privilege('bursar_operator', %s, 'EXECUTE')
                """,
                (signature, signature),
            )
            assert cursor.fetchone() == (False, False), signature

        cursor.execute(
            """
            SELECT p.oid::regprocedure::text
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = 'bursar'
              AND has_function_privilege('public', p.oid, 'EXECUTE')
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT granted_role.rolname, member_role.rolname
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE granted_role.rolname IN (
                'bursar_runtime',
                'bursar_client',
                'bursar_operator'
            )
              AND member_role.rolname <> current_user
            ORDER BY 1, 2
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT c.relname, r.rolname, privilege_type
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            CROSS JOIN pg_roles AS r
            CROSS JOIN LATERAL (
                SELECT unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']) AS privilege_type
            ) AS privileges
            WHERE n.nspname = 'bursar'
              AND c.relkind IN ('r', 'p')
              AND r.rolname IN ('bursar_client', 'bursar_operator')
              AND has_table_privilege(r.rolname, c.oid, privileges.privilege_type)
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT table_info.relname
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = 'bursar'
              AND table_info.relkind IN ('r', 'p')
              AND (
                  NOT table_info.relrowsecurity
                  OR (
                      NOT table_info.relforcerowsecurity
                      AND (
                          table_info.relname = 'tenants'
                          OR EXISTS (
                              SELECT 1
                              FROM pg_attribute AS attribute_info
                              WHERE attribute_info.attrelid = table_info.oid
                                AND attribute_info.attname = 'tenant_id'
                                AND NOT attribute_info.attisdropped
                          )
                      )
                  )
              )
            ORDER BY table_info.relname
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT function_info.oid::regprocedure::text
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            WHERE namespace_info.nspname = 'bursar'
              AND function_info.prosecdef
              AND NOT coalesce(
                  function_info.proconfig,
                  ARRAY[]::text[]
              ) @> ARRAY['search_path=""']
            ORDER BY 1
            """
        )
        assert cursor.fetchall() == []


def test_schema_and_public_rpc_comments_are_present(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'bursar'
              AND c.relkind IN ('r', 'p')
              AND c.relname <> 'schema_migrations'
              AND obj_description(c.oid, 'pg_class') IS NULL
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT obj_description(p.oid, 'pg_proc')
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = 'bursar'
              AND p.proname IN (
                  'publish_and_activate_catalog',
                  'provision_subject_account_on_insert',
                  'post_credit',
                  'charge_usage_for_operation',
                  'create_lease_for_operation',
                  'settle_lease',
                  'upsert_auto_recharge_profile',
                  'resolve_catalog_offer',
                  'resolve_catalog_topup',
                  'resolve_catalog_plan',
                  'list_subject_quota_events'
              )
            """
        )
        comments = cursor.fetchall()
        assert comments
        assert all(comment[0] for comment in comments)


def test_relational_keys_have_supporting_indexes(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_info.relname
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = 'bursar'
              AND table_info.relkind IN ('r', 'p')
              AND NOT table_info.relispartition
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_constraint AS constraint_info
                  WHERE constraint_info.conrelid = table_info.oid
                    AND constraint_info.contype = 'p'
              )
            ORDER BY table_info.relname
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT
                constraint_info.conrelid::regclass::text,
                constraint_info.conname
            FROM pg_constraint AS constraint_info
            WHERE constraint_info.contype = 'f'
              AND constraint_info.connamespace = 'bursar'::regnamespace
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_index AS index_info
                  WHERE index_info.indrelid = constraint_info.conrelid
                    AND index_info.indisvalid
                    AND (
                        regexp_split_to_array(
                            trim(index_info.indkey::text),
                            ' +'
                        )
                    )[1:cardinality(constraint_info.conkey)]::smallint[]
                        = constraint_info.conkey
              )
            ORDER BY 1, 2
            """
        )
        assert cursor.fetchall() == []


def test_nonunique_indexes_are_not_covered_by_another_index(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                smaller.indexrelid::regclass::text,
                covering.indexrelid::regclass::text
            FROM pg_index AS smaller
            JOIN pg_class AS smaller_class
              ON smaller_class.oid = smaller.indexrelid
            JOIN pg_index AS covering
              ON covering.indrelid = smaller.indrelid
             AND covering.indexrelid <> smaller.indexrelid
            JOIN pg_class AS covering_class
              ON covering_class.oid = covering.indexrelid
            WHERE smaller_class.relnamespace = 'bursar'::regnamespace
              AND NOT smaller.indisunique
              AND smaller.indisvalid
              AND covering.indisvalid
              AND smaller_class.relam = covering_class.relam
              AND smaller.indnkeyatts <= covering.indnkeyatts
              AND (
                  regexp_split_to_array(trim(smaller.indkey::text), ' +')
              )[1:smaller.indnkeyatts]
                  = (
                      regexp_split_to_array(
                          trim(covering.indkey::text),
                          ' +'
                      )
                  )[1:smaller.indnkeyatts]
              AND pg_get_expr(smaller.indpred, smaller.indrelid)
                  IS NOT DISTINCT FROM
                  pg_get_expr(covering.indpred, covering.indrelid)
              AND pg_get_expr(smaller.indexprs, smaller.indrelid)
                  IS NOT DISTINCT FROM
                  pg_get_expr(covering.indexprs, covering.indrelid)
            ORDER BY 1, 2
            """
        )
        assert cursor.fetchall() == []


def test_database_generated_ids_are_uuid_v7(pg_database_url: str) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                substring(generated.id::text, 15, 1),
                get_byte(uuid_send(generated.id), 8) >> 6
            FROM (SELECT bursar.uuid_v7() AS id) AS generated
            """
        )
        assert cursor.fetchone() == ("7", 2)

        cursor.execute(
            """
            SELECT table_info.relname, attribute_info.attname
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            JOIN pg_attribute AS attribute_info
              ON attribute_info.attrelid = table_info.oid
            JOIN pg_attrdef AS default_info
              ON default_info.adrelid = table_info.oid
             AND default_info.adnum = attribute_info.attnum
            WHERE namespace_info.nspname = 'bursar'
              AND attribute_info.atttypid = 'uuid'::regtype
              AND pg_get_expr(default_info.adbin, default_info.adrelid)
                  = 'gen_random_uuid()'::text
            ORDER BY 1, 2
            """
        )
        assert cursor.fetchall() == []


def test_internal_history_ids_use_bigint_identity(pg_database_url: str) -> None:
    table_names = [
        "account_plan_assignment_history",
        "billing_subscription_changes",
        "billing_subscription_conflicts",
        "plan_assignment_changes",
    ]

    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                table_name,
                data_type,
                is_identity,
                identity_generation
            FROM information_schema.columns
            WHERE table_schema = 'bursar'
              AND column_name = 'id'
              AND table_name = ANY(%s)
            ORDER BY table_name
            """,
            (table_names,),
        )
        assert cursor.fetchall() == [(table_name, "bigint", "YES", "ALWAYS") for table_name in table_names]
