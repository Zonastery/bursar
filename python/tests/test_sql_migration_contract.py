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
MIGRATION_METADATA_FIELDS = ("Migration", "Purpose", "Depends on", "Security")
METADATA_CONTINUATION_PATTERN = re.compile(r"^--   (?P<value>\S.*)$")


def _migration_metadata(content: str, path: Path) -> dict[str, str]:
    lines = content.splitlines()
    metadata: dict[str, str] = {}
    cursor = 0

    for label in MIGRATION_METADATA_FIELDS:
        pattern = re.compile(rf"^-- {re.escape(label)}: (?P<value>\S.*)$")
        assert cursor < len(lines), f"{path}: missing {label} metadata"
        match = pattern.fullmatch(lines[cursor])
        assert match is not None, f"{path}:{cursor + 1}: expected {label} metadata"

        value_parts = [match.group("value")]
        cursor += 1
        while cursor < len(lines) and lines[cursor].startswith("--   "):
            continuation = METADATA_CONTINUATION_PATTERN.fullmatch(lines[cursor])
            assert continuation is not None, (
                f"{path}:{cursor + 1}: metadata continuation must use nonempty '--   ...' alignment"
            )
            value_parts.append(continuation.group("value"))
            cursor += 1

        metadata[label] = " ".join(value_parts)

    assert cursor < len(lines) and lines[cursor] in {"", "--"}, (
        f"{path}:{cursor + 1}: metadata block must end with a blank separator"
    )
    return metadata


def test_migration_files_are_contiguous_documented_and_self_contained() -> None:
    files = _get_sql_files()
    prefixes = [int(path.stem.split("_", 1)[0]) for path in files]

    assert prefixes == list(range(1, len(files) + 1))
    assert len({path.name for path in files}) == len(files)
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert content.strip(), path
        assert content.endswith("\n"), path

        metadata = _migration_metadata(content, path)
        assert metadata["Migration"] == path.name, path


def test_greenfield_idempotency_contracts_are_defined_at_their_origins() -> None:
    checkout_table_sql = (SQL_DIR / "004_billing_tables.sql").read_text(encoding="utf-8")
    checkout_rpc_sql = (SQL_DIR / "023_billing_document_rpc.sql").read_text(encoding="utf-8")
    team_table_sql = (SQL_DIR / "003_financial_policy_tables.sql").read_text(encoding="utf-8")
    team_rpc_sql = (SQL_DIR / "018_team_rpc.sql").read_text(encoding="utf-8")
    outbox_index_sql = (SQL_DIR / "007_indexes.sql").read_text(encoding="utf-8")
    outbox_rpc_sql = (SQL_DIR / "028_storage_lifecycle_rpc.sql").read_text(encoding="utf-8")

    assert "operation_key text NOT NULL" in checkout_table_sql
    assert "UNIQUE (\n        tenant_id,\n        subject_id,\n        provider," in checkout_table_sql
    assert "p_operation_key text" in checkout_rpc_sql
    assert "AND existing.operation_key = p_operation_key" in checkout_rpc_sql
    assert "AND existing.request_digest = p_request_digest" not in checkout_rpc_sql
    assert "checkout intent idempotency conflict" not in checkout_rpc_sql
    assert "Commerce callers compare the returned stored" in checkout_rpc_sql
    assert "creation_idempotency_key text NOT NULL" in team_table_sql
    assert "creation_request_digest bytea NOT NULL" in team_table_sql
    assert "ON CONFLICT (tenant_id, creation_idempotency_key)" in team_rpc_sql
    assert "'idempotency_conflict'::text" in team_rpc_sql
    assert "event_outbox_tenant_claimable_idx" in outbox_index_sql
    assert "CREATE FUNCTION bursar.renew_tenant_outbox_claim(" in outbox_rpc_sql
    assert "CREATE FUNCTION bursar.requeue_outbox_dead_letter(" in outbox_rpc_sql


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
    assert client_signatures

    operator_signatures = (
        "bursar.get_storage_settings()",
        (
            "bursar.configure_storage(integer,integer,integer,integer,integer,"
            "integer,integer,integer,integer,integer,integer,integer,integer,integer)"
        ),
        "bursar.claim_outbox_events(integer,integer,text[])",
        "bursar.claim_outbox_events(uuid,integer,integer,text[])",
        "bursar.renew_tenant_outbox_claim(uuid,bigint,uuid,integer)",
        "bursar.complete_tenant_outbox_event(uuid,bigint,uuid)",
        "bursar.fail_tenant_outbox_event(uuid,bigint,uuid,text,integer,integer)",
        "bursar.get_outbox_stats(uuid)",
        "bursar.list_outbox_dead_letters(uuid,timestamptz,bigint,integer)",
        "bursar.requeue_outbox_dead_letter(uuid,bigint)",
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
              AND (
                  granted_role.rolname = 'bursar_runtime'
                  OR NOT member_role.rolcanlogin
                  OR member_role.rolsuper
                  OR member_role.rolcreatedb
                  OR member_role.rolcreaterole
                  OR member_role.rolreplication
                  OR member_role.rolbypassrls
                  OR membership.admin_option
                  OR membership.inherit_option
                  OR NOT membership.set_option
                  OR pg_has_role(
                      member_role.oid,
                      current_user::regrole::oid,
                      'MEMBER'
                  )
              )
            ORDER BY 1, 2
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT member_role.rolname
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE granted_role.rolname IN ('bursar_client', 'bursar_operator')
              AND member_role.rolname <> current_user
            GROUP BY member_role.rolname
            HAVING count(*) > 1
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
        cursor.execute("SELECT obj_description('bursar'::regnamespace, 'pg_namespace')")
        schema_comment = cursor.fetchone()
        assert schema_comment is not None
        assert schema_comment[0]

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
            SELECT p.oid::regprocedure::text
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = 'bursar'
              AND (
                  has_function_privilege('bursar_client', p.oid, 'EXECUTE')
                  OR has_function_privilege(
                      'bursar_operator',
                      p.oid,
                      'EXECUTE'
                  )
              )
              AND obj_description(p.oid, 'pg_proc') IS NULL
            ORDER BY 1
            """
        )
        assert cursor.fetchall() == []


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


def test_exact_credit_and_finite_temporal_table_boundaries(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_info.relname, attribute_info.attname
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            JOIN pg_attribute AS attribute_info
              ON attribute_info.attrelid = table_info.oid
            WHERE namespace_info.nspname = 'bursar'
              AND table_info.relkind IN ('r', 'p')
              AND NOT table_info.relispartition
              AND attribute_info.attnum > 0
              AND NOT attribute_info.attisdropped
              AND attribute_info.atttypid = 'numeric'::regtype
            ORDER BY 1, 2
            """
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT count(*)
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            JOIN pg_attribute AS attribute_info
              ON attribute_info.attrelid = table_info.oid
            WHERE namespace_info.nspname = 'bursar'
              AND table_info.relkind IN ('r', 'p')
              AND NOT table_info.relispartition
              AND attribute_info.attnum > 0
              AND NOT attribute_info.attisdropped
              AND attribute_info.atttypid = 'bursar.credit_numeric'::regtype
            """
        )
        credit_columns = cursor.fetchone()
        assert credit_columns is not None
        assert credit_columns[0] > 0

        for value in (
            "1.0000001",
            "100000000000000",
            "NaN",
            "Infinity",
            "-Infinity",
        ):
            cursor.execute("SAVEPOINT invalid_credit_numeric")
            with pytest.raises(psycopg2.errors.CheckViolation):
                cursor.execute(
                    "SELECT %s::numeric::bursar.credit_numeric",
                    (value,),
                )
            cursor.execute("ROLLBACK TO SAVEPOINT invalid_credit_numeric")
            cursor.execute("RELEASE SAVEPOINT invalid_credit_numeric")

        cursor.execute(
            """
            SELECT
                table_info.relname,
                attribute_info.attname
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            JOIN pg_attribute AS attribute_info
              ON attribute_info.attrelid = table_info.oid
            WHERE namespace_info.nspname = 'bursar'
              AND table_info.relkind IN ('r', 'p')
              AND NOT table_info.relispartition
              AND attribute_info.attnum > 0
              AND NOT attribute_info.attisdropped
              AND attribute_info.atttypid IN (
                  'timestamp with time zone'::regtype,
                  'date'::regtype
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM pg_constraint AS constraint_info
                  WHERE constraint_info.conrelid = table_info.oid
                    AND constraint_info.contype = 'c'
                    AND constraint_info.conkey @>
                        ARRAY[attribute_info.attnum]::smallint[]
                    AND pg_get_constraintdef(constraint_info.oid) LIKE
                        CASE attribute_info.atttypid
                            WHEN 'date'::regtype
                                THEN '%%is_finite_date%%'
                            ELSE '%%is_finite_timestamptz%%'
                        END
              )
            ORDER BY 1, 2
            """
        )
        assert cursor.fetchall() == []

        for value in ("infinity", "-infinity"):
            cursor.execute("SAVEPOINT invalid_migration_timestamp")
            with pytest.raises(psycopg2.errors.CheckViolation):
                cursor.execute(
                    """
                    UPDATE bursar.schema_migrations
                    SET applied_at = %s::timestamptz
                    WHERE version = (
                        SELECT min(version) FROM bursar.schema_migrations
                    )
                    """,
                    (value,),
                )
            cursor.execute("ROLLBACK TO SAVEPOINT invalid_migration_timestamp")
            cursor.execute("RELEASE SAVEPOINT invalid_migration_timestamp")


def test_projected_catalog_arrays_are_canonical_at_the_table_boundary(
    pg_database_url: str,
) -> None:
    with psycopg2.connect(pg_database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                bursar.is_canonical_identifier_array(ARRAY['chat', 'image']),
                bursar.is_canonical_identifier_array(ARRAY['chat', NULL]),
                bursar.is_canonical_identifier_array(ARRAY['chat', 'chat']),
                bursar.is_canonical_identifier_array(
                    '[0:1]={chat,image}'::text[]
                ),
                bursar.is_canonical_threshold_array(ARRAY[50, 100]),
                bursar.is_canonical_threshold_array(ARRAY[50, NULL]),
                bursar.is_canonical_threshold_array(ARRAY[50, 50]),
                bursar.is_canonical_threshold_array(ARRAY[100, 50])
            """
        )
        assert cursor.fetchone() == (
            True,
            False,
            False,
            False,
            True,
            False,
            False,
            False,
        )

        cursor.execute(
            """
            SELECT table_name, column_name
            FROM (
                VALUES
                    ('catalog_plans', 'allowed_operations',
                     'is_canonical_identifier_array'),
                    ('catalog_plan_quotas', 'emit_at_percent',
                     'is_canonical_threshold_array'),
                    ('catalog_auto_recharge_policies', 'eligible_topup_keys',
                     'is_canonical_identifier_array')
            ) AS expected(table_name, column_name, helper_name)
            WHERE NOT EXISTS (
                SELECT 1
                FROM pg_constraint AS constraint_info
                JOIN pg_class AS table_info
                  ON table_info.oid = constraint_info.conrelid
                JOIN pg_attribute AS attribute_info
                  ON attribute_info.attrelid = table_info.oid
                 AND attribute_info.attname = expected.column_name
                WHERE table_info.relnamespace = 'bursar'::regnamespace
                  AND table_info.relname = expected.table_name
                  AND constraint_info.contype = 'c'
                  AND constraint_info.conkey @>
                      ARRAY[attribute_info.attnum]::smallint[]
                  AND pg_get_constraintdef(constraint_info.oid)
                      LIKE '%%' || expected.helper_name || '%%'
            )
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
