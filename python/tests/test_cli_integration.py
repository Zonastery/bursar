"""DB-backed CLI coverage for operator-facing Bursar workflows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from bursar import __main__ as cli
from bursar.credits.postgres.store import PostgresStore
from bursar.credits.service import CreditsService
from bursar.credits.store import CreateLeaseOptions
from bursar.sql import _get_sql_files
from tests.conftest import _handle_unavailable_postgres, _start_testcontainer
from tests.test_store_integration import CONFIG

pytestmark = [pytest.mark.integration, pytest.mark.security]

CLI_TENANT_ID = "00000000-0000-4000-8000-000000000101"
CLI_MAINTENANCE_SUBJECT_ID = "00000000-0000-4000-8000-000000000102"
OPERATOR_OWNER_ROLE = "bursar_operator_runtime"
PARTITION_OWNER_ROLE = "bursar_partition_runtime"
OPERATOR_FUNCTIONS = (
    "bursar.archive_billing_event_payload(uuid,text,text)",
    "bursar.claim_outbox_events(integer,integer,text[])",
    "bursar.claim_outbox_events(uuid,integer,integer,text[])",
    "bursar.complete_outbox_event(bigint,uuid)",
    "bursar.complete_tenant_outbox_event(uuid,bigint,uuid)",
    (
        "bursar.configure_storage(integer,integer,integer,integer,integer,"
        "integer,integer,integer,integer,integer,integer,integer,integer,integer)"
    ),
    "bursar.create_tenant(uuid,text,text)",
    "bursar.export_billing_event_payload(uuid)",
    "bursar.export_usage_charge(uuid)",
    "bursar.fail_outbox_event(bigint,uuid,text,integer,integer)",
    "bursar.fail_tenant_outbox_event(uuid,bigint,uuid,text,integer,integer)",
    "bursar.get_outbox_stats(uuid)",
    "bursar.get_storage_settings()",
    "bursar.list_outbox_dead_letters(uuid,timestamp with time zone,bigint,integer)",
    "bursar.maybe_run_storage_maintenance(timestamp with time zone)",
    "bursar.renew_tenant_outbox_claim(uuid,bigint,uuid,integer)",
    "bursar.requeue_outbox_dead_letter(uuid,bigint)",
    "bursar.resolve_active_tenant_for_trigger(text)",
    "bursar.run_storage_maintenance(timestamp with time zone)",
    "bursar.run_storage_partition_maintenance(text,timestamp with time zone)",
    "bursar.set_tenant_status(uuid,text)",
)
OPERATOR_TABLE_PRIVILEGES = {
    ("billing_event_payloads", "DELETE"),
    ("billing_event_payloads", "SELECT"),
    ("billing_event_payloads", "UPDATE"),
    ("billing_events", "SELECT"),
    ("billing_events", "UPDATE"),
    ("catalog_plan_quotas", "SELECT"),
    ("credit_accounts", "SELECT"),
    ("credit_leases", "SELECT"),
    ("credit_leases", "UPDATE"),
    ("credit_usage_charges", "DELETE"),
    ("credit_usage_charges", "SELECT"),
    ("credit_usage_charges", "UPDATE"),
    ("event_outbox", "DELETE"),
    ("event_outbox", "SELECT"),
    ("event_outbox", "UPDATE"),
    ("quota_events", "DELETE"),
    ("quota_events", "SELECT"),
    ("quota_events", "UPDATE"),
    ("quota_usage_events", "DELETE"),
    ("quota_usage_events", "SELECT"),
    ("quota_usage_events", "UPDATE"),
    ("storage_settings", "SELECT"),
    ("storage_settings", "UPDATE"),
    ("tenants", "INSERT"),
    ("tenants", "SELECT"),
    ("tenants", "UPDATE"),
    ("usage_charge_payloads", "DELETE"),
    ("usage_charge_payloads", "SELECT"),
    ("usage_charge_payloads", "UPDATE"),
    ("usage_daily_rollups", "DELETE"),
    ("usage_daily_rollups", "SELECT"),
    ("usage_daily_rollups", "UPDATE"),
}
OPERATOR_RLS_COMMANDS = {
    (table_name, {"SELECT": "r", "INSERT": "a", "UPDATE": "w", "DELETE": "d"}[privilege])
    for table_name, privilege in OPERATOR_TABLE_PRIVILEGES
}


@dataclass(frozen=True)
class CliDatabaseUrls:
    admin: str = field(repr=False)
    migration: str = field(repr=False)
    operator: str = field(repr=False)
    runtime: str = field(repr=False)
    migration_role: str
    operator_role: str
    runtime_role: str
    operator_password: str = field(repr=False)
    runtime_password: str = field(repr=False)


@pytest.fixture
def cli_database_urls() -> Iterator[CliDatabaseUrls]:
    """Create an isolated empty database and one non-superuser migration owner."""
    container = None
    admin_database_url: str | None = None
    leaked_sessions: list[tuple[str, int]] = []
    suffix = uuid4().hex[:12]
    database_name = f"bursar_cli_{suffix}"
    roles = {
        "migration": f"bursar_cli_migrate_{suffix}",
        "operator": f"bursar_cli_operator_{suffix}",
        "runtime": f"bursar_cli_runtime_{suffix}",
    }
    passwords = {name: uuid4().hex for name in roles}

    try:
        try:
            container_admin_url, container = _start_testcontainer()
        except RuntimeError as exc:
            _handle_unavailable_postgres(f"No isolated Postgres available for the CLI credential-routing test: {exc}")

        cluster_admin_url = make_dsn(container_admin_url, dbname="postgres")
        with closing(psycopg2.connect(cluster_admin_url)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                # SUPERUSER exists only for the external extension bootstrap
                # below and is removed before the fixture yields. The Bursar
                # migration itself keeps only CREATEROLE, the minimum needed
                # by migrations 029 and 030 to provision their NOLOGIN owners.
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD %s SUPERUSER "
                        "NOCREATEDB CREATEROLE NOINHERIT NOREPLICATION "
                        "NOBYPASSRLS"
                    ).format(sql.Identifier(roles["migration"])),
                    (passwords["migration"],),
                )
                cursor.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(database_name),
                        sql.Identifier(roles["migration"]),
                    )
                )

        admin_database_url = make_dsn(container_admin_url, dbname=database_name)
        migration_database_url = make_dsn(
            admin_database_url,
            user=roles["migration"],
            password=passwords["migration"],
        )
        # pg_jsonschema is a superuser-only external extension. Bootstrap the
        # three required extensions under the database owner, then remove that
        # temporary provider authority before the Bursar CLI runs.
        with (
            closing(psycopg2.connect(migration_database_url)) as connection,
            connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("CREATE SCHEMA extensions")
            cursor.execute("CREATE EXTENSION pgcrypto WITH SCHEMA extensions")
            cursor.execute("CREATE EXTENSION pg_jsonschema WITH SCHEMA extensions")
            cursor.execute("CREATE SCHEMA partman")
            cursor.execute("CREATE EXTENSION pg_partman WITH SCHEMA partman")
        with closing(psycopg2.connect(cluster_admin_url)) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER").format(sql.Identifier(roles["migration"])))

        urls = CliDatabaseUrls(
            admin=admin_database_url,
            migration=migration_database_url,
            operator=make_dsn(
                admin_database_url,
                user=roles["operator"],
                password=passwords["operator"],
            ),
            runtime=make_dsn(
                admin_database_url,
                user=roles["runtime"],
                password=passwords["runtime"],
            ),
            migration_role=roles["migration"],
            operator_role=roles["operator"],
            runtime_role=roles["runtime"],
            operator_password=passwords["operator"],
            runtime_password=passwords["runtime"],
        )
        yield urls
    finally:
        if container is not None:
            try:
                if admin_database_url is not None:
                    with (
                        closing(psycopg2.connect(admin_database_url)) as connection,
                        connection,
                        connection.cursor() as cursor,
                    ):
                        cursor.execute(
                            "SELECT usename, count(*) "
                            "FROM pg_stat_activity "
                            "WHERE usename = ANY(%s) AND pid <> pg_backend_pid() "
                            "GROUP BY usename ORDER BY usename",
                            (list(roles.values()),),
                        )
                        leaked_sessions = cursor.fetchall()
            finally:
                container.stop()
        assert leaked_sessions == []


def _provision_cli_callers(urls: CliDatabaseUrls) -> None:
    """Provision caller logins, then grant their Bursar roles as migrator."""
    with (
        closing(psycopg2.connect(urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        for role, password in (
            (urls.operator_role, urls.operator_password),
            (urls.runtime_role, urls.runtime_password),
        ):
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(role)),
                (password,),
            )

    with (
        closing(psycopg2.connect(urls.migration)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            sql.SQL("GRANT bursar_operator TO {} WITH INHERIT FALSE, SET TRUE").format(
                sql.Identifier(urls.operator_role)
            )
        )
        cursor.execute(
            sql.SQL("GRANT bursar_client TO {} WITH INHERIT FALSE, SET TRUE").format(sql.Identifier(urls.runtime_role))
        )


def _write_config(path: Path, *, display_name: str) -> None:
    config = deepcopy(CONFIG)
    config["plans"]["pro"]["display_name"] = display_name
    config["commerce"] = {
        "providers": {"stripe": {"type": "stripe"}},
        "offers": {
            "cli_topup": {
                "type": "topup",
                "display_name": "CLI top-up",
                "price": {"amount_minor": 100, "currency": "USD"},
                "providers": {
                    "stripe": {
                        "type": "stripe_price",
                        "price_id": "price_cli_test",
                    }
                },
                "credits_per_unit": "100",
                "bucket": "purchased",
                "quantity": {"minimum": 1, "maximum": 10, "default": 1},
            }
        },
    }
    path.write_text(json.dumps(config))


@pytest.mark.timeout(300)
def test_cli_manages_tenants_migrations_and_config_versions(
    cli_database_urls: CliDatabaseUrls,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", cli_database_urls.runtime)
    monkeypatch.setenv("BURSAR_MIGRATION_DATABASE_URL", cli_database_urls.migration)
    monkeypatch.setenv("BURSAR_OPERATOR_DATABASE_URL", cli_database_urls.operator)
    monkeypatch.setenv("BURSAR_TENANT_ID", CLI_TENANT_ID)
    monkeypatch.setenv("BURSAR_PROVIDER_ENVIRONMENT", "test")

    with (
        closing(psycopg2.connect(cli_database_urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            SELECT
                rolcanlogin,
                rolsuper,
                rolcreatedb,
                rolcreaterole,
                rolinherit,
                rolreplication,
                rolbypassrls
            FROM pg_roles
            WHERE rolname = %s
            """,
            (cli_database_urls.migration_role,),
        )
        assert cursor.fetchone() == (
            True,
            False,
            False,
            True,
            False,
            False,
            False,
        )
        cursor.execute(
            """
            SELECT
                to_regnamespace('bursar'),
                count(*) FILTER (
                    WHERE rolname IN (
                        'bursar_runtime',
                        'bursar_client',
                        'bursar_operator',
                        'bursar_operator_runtime',
                        'bursar_partition_runtime'
                    )
                )
            FROM pg_roles
            """
        )
        assert cursor.fetchone() == (None, 0)
        cursor.execute(
            """
            SELECT extension_info.extname, owner_info.rolname
            FROM pg_extension AS extension_info
            JOIN pg_roles AS owner_info
              ON owner_info.oid = extension_info.extowner
            WHERE extension_info.extname IN (
                'pgcrypto',
                'pg_jsonschema',
                'pg_partman'
            )
            ORDER BY extension_info.extname
            """
        )
        assert cursor.fetchall() == [
            ("pg_jsonschema", cli_database_urls.migration_role),
            ("pg_partman", cli_database_urls.migration_role),
            ("pgcrypto", cli_database_urls.migration_role),
        ]

    cli.main(["migrate"])
    assert capsys.readouterr().out == "Migrations applied successfully.\n"
    _provision_cli_callers(cli_database_urls)

    expected_manifest = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest()) for path in _get_sql_files()]
    with (
        closing(psycopg2.connect(cli_database_urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT version, checksum FROM bursar.schema_migrations ORDER BY version")
        assert cursor.fetchall() == expected_manifest

        cursor.execute(
            """
            SELECT
                pg_get_userbyid(namespace_info.nspowner),
                pg_get_userbyid(migration_table.relowner),
                pg_get_userbyid(tenant_table.relowner)
            FROM pg_namespace AS namespace_info
            JOIN pg_class AS migration_table
              ON migration_table.relnamespace = namespace_info.oid
             AND migration_table.relname = 'schema_migrations'
            JOIN pg_class AS tenant_table
              ON tenant_table.relnamespace = namespace_info.oid
             AND tenant_table.relname = 'tenants'
            WHERE namespace_info.nspname = 'bursar'
            """
        )
        assert cursor.fetchone() == (
            cli_database_urls.migration_role,
            cli_database_urls.migration_role,
            cli_database_urls.migration_role,
        )

        cursor.execute(
            """
            SELECT
                rolname,
                rolcanlogin,
                rolsuper,
                rolcreatedb,
                rolcreaterole,
                rolinherit,
                rolreplication,
                rolbypassrls
            FROM pg_roles
            WHERE rolname = ANY(%s)
            """,
            (
                [
                    cli_database_urls.migration_role,
                    cli_database_urls.operator_role,
                    cli_database_urls.runtime_role,
                    OPERATOR_OWNER_ROLE,
                    PARTITION_OWNER_ROLE,
                ],
            ),
        )
        role_attributes = {row[0]: row[1:] for row in cursor.fetchall()}
        assert role_attributes == {
            cli_database_urls.migration_role: (
                True,
                False,
                False,
                True,
                False,
                False,
                False,
            ),
            cli_database_urls.operator_role: (
                True,
                False,
                False,
                False,
                False,
                False,
                False,
            ),
            cli_database_urls.runtime_role: (
                True,
                False,
                False,
                False,
                False,
                False,
                False,
            ),
            OPERATOR_OWNER_ROLE: (
                False,
                False,
                False,
                False,
                False,
                False,
                False,
            ),
            PARTITION_OWNER_ROLE: (
                False,
                False,
                False,
                False,
                False,
                False,
                False,
            ),
        }

        cursor.execute(
            """
            SELECT
                member_role.rolname,
                granted_role.rolname,
                bool_or(membership.inherit_option),
                bool_or(membership.set_option),
                bool_or(membership.admin_option)
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE member_role.rolname = ANY(%s)
              AND granted_role.rolname IN (
                  'bursar_runtime',
                  'bursar_client',
                  'bursar_operator',
                  'bursar_operator_runtime',
                  'bursar_partition_runtime'
              )
            GROUP BY member_role.rolname, granted_role.rolname
            ORDER BY member_role.rolname, granted_role.rolname
            """,
            (
                [
                    cli_database_urls.migration_role,
                    cli_database_urls.operator_role,
                    cli_database_urls.runtime_role,
                    OPERATOR_OWNER_ROLE,
                    PARTITION_OWNER_ROLE,
                ],
            ),
        )
        assert cursor.fetchall() == sorted(
            [
                (
                    cli_database_urls.migration_role,
                    "bursar_client",
                    False,
                    True,
                    True,
                ),
                (
                    cli_database_urls.migration_role,
                    "bursar_operator",
                    False,
                    True,
                    True,
                ),
                (
                    cli_database_urls.migration_role,
                    OPERATOR_OWNER_ROLE,
                    False,
                    True,
                    True,
                ),
                (
                    cli_database_urls.migration_role,
                    PARTITION_OWNER_ROLE,
                    False,
                    True,
                    True,
                ),
                (
                    cli_database_urls.migration_role,
                    "bursar_runtime",
                    False,
                    True,
                    True,
                ),
                (
                    cli_database_urls.operator_role,
                    "bursar_operator",
                    False,
                    True,
                    False,
                ),
                (
                    cli_database_urls.runtime_role,
                    "bursar_client",
                    False,
                    True,
                    False,
                ),
            ]
        )

        for role, expected in (
            (cli_database_urls.migration_role, (True, True, True, True)),
            (cli_database_urls.operator_role, (False, False, False, False)),
            (cli_database_urls.runtime_role, (False, False, False, False)),
            (OPERATOR_OWNER_ROLE, (False, False, False, True)),
            (PARTITION_OWNER_ROLE, (False, True, False, False)),
        ):
            cursor.execute(
                """
                SELECT
                    has_database_privilege(%s, current_database(), 'CREATE'),
                    has_schema_privilege(%s, 'bursar', 'CREATE'),
                    has_table_privilege(
                        %s,
                        'bursar.schema_migrations',
                        'SELECT'
                    ),
                    has_table_privilege(%s, 'bursar.tenants', 'SELECT')
                """,
                (role, role, role, role),
            )
            assert cursor.fetchone() == expected

        cursor.execute(
            """
            SELECT
                function_info.oid::regprocedure::text,
                owner_info.rolname
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            JOIN pg_roles AS owner_info
              ON owner_info.oid = function_info.proowner
            WHERE namespace_info.nspname = 'bursar'
              AND has_function_privilege(
                  'bursar_operator',
                  function_info.oid,
                  'EXECUTE'
              )
            ORDER BY 1
            """
        )
        assert cursor.fetchall() == [(signature, OPERATOR_OWNER_ROLE) for signature in OPERATOR_FUNCTIONS]

        cursor.execute(
            """
            SELECT table_info.relname, privileges.privilege_type
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            CROSS JOIN LATERAL (
                SELECT unnest(
                    ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE']
                ) AS privilege_type
            ) AS privileges
            WHERE namespace_info.nspname = 'bursar'
              AND table_info.relkind IN ('r', 'p')
              AND NOT table_info.relispartition
              AND has_table_privilege(
                  %s,
                  table_info.oid,
                  privileges.privilege_type
              )
            """,
            (OPERATOR_OWNER_ROLE,),
        )
        assert set(cursor.fetchall()) == OPERATOR_TABLE_PRIVILEGES

        cursor.execute(
            """
            SELECT table_info.relname, policy_info.polcmd
            FROM pg_policy AS policy_info
            JOIN pg_class AS table_info
              ON table_info.oid = policy_info.polrelid
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = 'bursar'
              AND NOT table_info.relispartition
              AND policy_info.polroles = ARRAY[
                  %s::regrole::oid
              ]::oid[]
            """,
            (OPERATOR_OWNER_ROLE,),
        )
        assert set(cursor.fetchall()) == OPERATOR_RLS_COMMANDS

        operator_tables = sorted({table_name for table_name, _ in OPERATOR_TABLE_PRIVILEGES})
        cursor.execute(
            """
            SELECT
                table_info.relname,
                table_info.relrowsecurity,
                table_info.relforcerowsecurity
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = 'bursar'
              AND table_info.relname = ANY(%s)
              AND NOT table_info.relispartition
            ORDER BY 1
            """,
            (operator_tables,),
        )
        operator_rls = {row[0]: row[1:] for row in cursor.fetchall()}
        assert set(operator_rls) == set(operator_tables)
        assert operator_rls.pop("storage_settings") == (True, False)
        assert set(operator_rls.values()) == {(True, True)}

        cursor.execute(
            """
            SELECT
                has_schema_privilege(%s, 'bursar', 'USAGE'),
                has_schema_privilege(%s, 'bursar', 'CREATE'),
                has_schema_privilege(%s, 'partman', 'USAGE'),
                has_schema_privilege(%s, 'partman', 'USAGE'),
                has_schema_privilege(%s, 'partman', 'CREATE'),
                has_function_privilege(
                    'bursar_operator',
                    'bursar.secure_tenant_partition(regclass)',
                    'EXECUTE'
                ),
                has_function_privilege(
                    %s,
                    'bursar.secure_tenant_partition(regclass)',
                    'EXECUTE'
                ),
                has_function_privilege(
                    'bursar_operator',
                    'bursar.run_storage_partition_maintenance_base('
                    'text,timestamp with time zone)',
                    'EXECUTE'
                ),
                has_function_privilege(
                    %s,
                    'bursar.run_storage_partition_maintenance_base('
                    'text,timestamp with time zone)',
                    'EXECUTE'
                )
            """,
            (
                OPERATOR_OWNER_ROLE,
                OPERATOR_OWNER_ROLE,
                OPERATOR_OWNER_ROLE,
                PARTITION_OWNER_ROLE,
                PARTITION_OWNER_ROLE,
                OPERATOR_OWNER_ROLE,
                OPERATOR_OWNER_ROLE,
            ),
        )
        assert cursor.fetchone() == (
            True,
            False,
            False,
            True,
            False,
            False,
            False,
            False,
            True,
        )

        cursor.execute(
            """
            SELECT function_info.oid::regprocedure::text, owner_info.rolname
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            JOIN pg_roles AS owner_info
              ON owner_info.oid = function_info.proowner
            WHERE function_info.oid IN (
                'bursar.secure_tenant_partition(regclass)'::regprocedure,
                'bursar.secure_tenant_partition_base(regclass)'::regprocedure,
                'bursar.run_storage_partition_maintenance_base('
                'text,timestamp with time zone)'::regprocedure
            )
            ORDER BY 1
            """
        )
        assert set(cursor.fetchall()) == {
            (
                "bursar.run_storage_partition_maintenance_base(text,timestamp with time zone)",
                PARTITION_OWNER_ROLE,
            ),
            ("bursar.secure_tenant_partition_base(regclass)", PARTITION_OWNER_ROLE),
            ("bursar.secure_tenant_partition(regclass)", PARTITION_OWNER_ROLE),
        }

        cursor.execute(
            """
            WITH checked_roles(role_name, role_oid) AS (
                VALUES
                    ('PUBLIC', 0::oid),
                    ('bursar_client', 'bursar_client'::regrole::oid),
                    ('bursar_runtime', 'bursar_runtime'::regrole::oid),
                    ('bursar_operator', 'bursar_operator'::regrole::oid),
                    (%s, %s::regrole::oid),
                    (%s, %s::regrole::oid)
            ),
            checked_functions(function_oid) AS (
                VALUES
                    (
                        'bursar.run_storage_partition_maintenance('
                        'text,timestamp with time zone)'::regprocedure
                    ),
                    (
                        'bursar.run_storage_partition_maintenance_base('
                        'text,timestamp with time zone)'::regprocedure
                    ),
                    (
                        'bursar.secure_tenant_partition('
                        'regclass)'::regprocedure
                    ),
                    (
                        'bursar.secure_tenant_partition_base('
                        'regclass)'::regprocedure
                    )
            )
            SELECT
                checked_roles.role_name,
                checked_functions.function_oid::regprocedure::text
            FROM checked_roles
            CROSS JOIN checked_functions
            WHERE has_function_privilege(
                checked_roles.role_oid,
                checked_functions.function_oid,
                'EXECUTE'
            )
            ORDER BY 1, 2
            """,
            (
                OPERATOR_OWNER_ROLE,
                OPERATOR_OWNER_ROLE,
                PARTITION_OWNER_ROLE,
                PARTITION_OWNER_ROLE,
            ),
        )
        assert set(cursor.fetchall()) == {
            (
                "bursar_operator",
                "bursar.run_storage_partition_maintenance(text,timestamp with time zone)",
            ),
            (
                OPERATOR_OWNER_ROLE,
                "bursar.run_storage_partition_maintenance(text,timestamp with time zone)",
            ),
            (
                OPERATOR_OWNER_ROLE,
                "bursar.run_storage_partition_maintenance_base(text,timestamp with time zone)",
            ),
            (
                PARTITION_OWNER_ROLE,
                "bursar.run_storage_partition_maintenance_base(text,timestamp with time zone)",
            ),
            (
                PARTITION_OWNER_ROLE,
                "bursar.secure_tenant_partition(regclass)",
            ),
            (
                PARTITION_OWNER_ROLE,
                "bursar.secure_tenant_partition_base(regclass)",
            ),
        }

        cursor.execute(
            """
            SELECT function_info.oid::regprocedure::text
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            JOIN pg_roles AS owner_info
              ON owner_info.oid = function_info.proowner
            WHERE namespace_info.nspname = 'bursar'
              AND function_info.prosecdef
              AND owner_info.rolname <> %s
              AND has_function_privilege(
                  %s,
                  function_info.oid,
                  'EXECUTE'
              )
            ORDER BY 1
            """,
            (OPERATOR_OWNER_ROLE, OPERATOR_OWNER_ROLE),
        )
        assert cursor.fetchall() == [("bursar.run_storage_partition_maintenance_base(text,timestamp with time zone)",)]

        cursor.execute(
            """
            SELECT function_info.oid::regprocedure::text
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            WHERE namespace_info.nspname = 'bursar'
              AND NOT function_info.prosecdef
              AND NOT has_function_privilege(
                  %s,
                  function_info.oid,
                  'EXECUTE'
              )
            ORDER BY 1
            """,
            (OPERATOR_OWNER_ROLE,),
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT function_info.oid::regprocedure::text
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            CROSS JOIN LATERAL aclexplode(
                coalesce(
                    function_info.proacl,
                    acldefault('f', function_info.proowner)
                )
            ) AS privilege_info
            WHERE namespace_info.nspname = 'extensions'
              AND privilege_info.grantee = %s::regrole::oid
              AND privilege_info.privilege_type = 'EXECUTE'
            ORDER BY 1
            """,
            (OPERATOR_OWNER_ROLE,),
        )
        assert cursor.fetchall() == [
            ("extensions.digest(bytea,text)",),
            ("extensions.gen_random_bytes(integer)",),
            ("extensions.jsonb_matches_schema(json,jsonb)",),
            ("extensions.jsonschema_is_valid(json)",),
            ("extensions.jsonschema_validation_errors(json,json)",),
        ]

        cursor.execute(
            """
            SELECT table_info.relname, privilege_info.privilege_type
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            CROSS JOIN LATERAL aclexplode(table_info.relacl)
            AS privilege_info
            WHERE namespace_info.nspname = 'partman'
              AND privilege_info.grantee = %s::regrole::oid
            ORDER BY 1, 2
            """,
            (PARTITION_OWNER_ROLE,),
        )
        assert cursor.fetchall() == [
            ("part_config", "DELETE"),
            ("part_config", "SELECT"),
            ("part_config", "UPDATE"),
            ("part_config_sub", "DELETE"),
            ("part_config_sub", "SELECT"),
            ("part_config_sub", "UPDATE"),
        ]

        cursor.execute(
            """
            SELECT
                count(*) FILTER (
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM aclexplode(function_info.proacl)
                            AS privilege_info
                        WHERE privilege_info.grantee = %s::regrole::oid
                          AND privilege_info.privilege_type = 'EXECUTE'
                    )
                ),
                count(*)
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            WHERE namespace_info.nspname = 'partman'
              AND function_info.prokind IN ('f', 'p')
            """,
            (PARTITION_OWNER_ROLE,),
        )
        missing_partman_grants, partman_function_count = cursor.fetchone()
        assert missing_partman_grants == 0
        assert partman_function_count > 0

        cursor.execute(
            """
            SELECT table_info.relname, pg_get_userbyid(table_info.relowner)
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = 'partman'
              AND table_info.relname IN (
                  'template_bursar_usage_charge_payloads',
                  'template_bursar_billing_event_payloads'
              )
            ORDER BY 1
            """
        )
        assert cursor.fetchall() == [
            (
                "template_bursar_billing_event_payloads",
                PARTITION_OWNER_ROLE,
            ),
            ("template_bursar_usage_charge_payloads", PARTITION_OWNER_ROLE),
        ]

        cursor.execute(
            """
            SELECT table_info.relname, privilege_info.privilege_type
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            CROSS JOIN LATERAL aclexplode(table_info.relacl)
            AS privilege_info
            WHERE namespace_info.nspname = 'bursar'
              AND NOT table_info.relispartition
              AND table_info.relowner <> %s::regrole::oid
              AND privilege_info.grantee = %s::regrole::oid
            ORDER BY 1, 2
            """,
            (PARTITION_OWNER_ROLE, PARTITION_OWNER_ROLE),
        )
        assert cursor.fetchall() == [("storage_settings", "SELECT")]

        cursor.execute(
            """
            SELECT table_info.relname, policy_info.polcmd
            FROM pg_policy AS policy_info
            JOIN pg_class AS table_info
              ON table_info.oid = policy_info.polrelid
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = 'bursar'
              AND NOT table_info.relispartition
              AND policy_info.polroles = ARRAY[
                  %s::regrole::oid
              ]::oid[]
            ORDER BY 1, 2
            """,
            (PARTITION_OWNER_ROLE,),
        )
        assert cursor.fetchall() == [("storage_settings", "r")]

        cursor.execute(
            """
            SELECT table_info.oid::regclass::text
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            LEFT JOIN pg_inherits AS inheritance
              ON inheritance.inhrelid = table_info.oid
            LEFT JOIN pg_class AS parent
              ON parent.oid = inheritance.inhparent
            WHERE namespace_info.nspname = 'bursar'
              AND (
                  table_info.relname IN (
                      'usage_charge_payloads',
                      'billing_event_payloads'
                  )
                  OR parent.relname IN (
                      'usage_charge_payloads',
                      'billing_event_payloads'
                  )
              )
              AND pg_get_userbyid(table_info.relowner) <> %s
            ORDER BY 1
            """,
            (PARTITION_OWNER_ROLE,),
        )
        assert cursor.fetchall() == []

        cursor.execute(
            """
            SELECT
                child_schema.nspname,
                child.relname,
                pg_get_userbyid(parent.relowner),
                pg_get_userbyid(child.relowner),
                has_table_privilege(%s, child.oid, 'SELECT')
            FROM pg_inherits AS inheritance
            JOIN pg_class AS child
              ON child.oid = inheritance.inhrelid
            JOIN pg_namespace AS child_schema
              ON child_schema.oid = child.relnamespace
            JOIN pg_class AS parent
              ON parent.oid = inheritance.inhparent
            JOIN pg_namespace AS parent_schema
              ON parent_schema.oid = parent.relnamespace
            WHERE parent_schema.nspname = 'bursar'
              AND parent.relname = 'usage_charge_payloads'
            ORDER BY child.relname
            LIMIT 1
            """,
            (cli_database_urls.migration_role,),
        )
        payload_partition = cursor.fetchone()
        assert payload_partition is not None
        assert payload_partition[2:] == (
            PARTITION_OWNER_ROLE,
            PARTITION_OWNER_ROLE,
            False,
        )

    with (
        closing(psycopg2.connect(cli_database_urls.migration)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
            cursor.execute(
                sql.SQL("SELECT 1 FROM {}.{} LIMIT 1").format(
                    sql.Identifier(payload_partition[0]),
                    sql.Identifier(payload_partition[1]),
                )
            )
        assert exc_info.value.pgcode == "42501"
        connection.rollback()

    with (
        closing(psycopg2.connect(cli_database_urls.migration)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT session_user, current_user")
        assert cursor.fetchone() == (
            cli_database_urls.migration_role,
            cli_database_urls.migration_role,
        )
        for granted_role in (
            "bursar_client",
            "bursar_operator",
            OPERATOR_OWNER_ROLE,
            PARTITION_OWNER_ROLE,
            "bursar_runtime",
        ):
            cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(granted_role)))
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == (granted_role,)
            cursor.execute("RESET ROLE")

    for caller_role in (
        cli_database_urls.operator_role,
        cli_database_urls.runtime_role,
    ):
        with (
            closing(psycopg2.connect(cli_database_urls.migration)) as connection,
            connection,
            connection.cursor() as cursor,
        ):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
                cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(caller_role)))
            assert exc_info.value.pgcode == "42501"
            connection.rollback()

    for database_url, expected_role, forbidden_roles in (
        (
            cli_database_urls.operator,
            cli_database_urls.operator_role,
            (
                "bursar_client",
                OPERATOR_OWNER_ROLE,
                PARTITION_OWNER_ROLE,
                "bursar_runtime",
            ),
        ),
        (
            cli_database_urls.runtime,
            cli_database_urls.runtime_role,
            (
                "bursar_operator",
                OPERATOR_OWNER_ROLE,
                PARTITION_OWNER_ROLE,
                "bursar_runtime",
            ),
        ),
    ):
        for forbidden_role in forbidden_roles:
            with (
                closing(psycopg2.connect(database_url)) as connection,
                connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SELECT session_user, current_user")
                assert cursor.fetchone() == (expected_role, expected_role)
                with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc_info:
                    cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(forbidden_role)))
                assert exc_info.value.pgcode == "42501"
                connection.rollback()

    cli.main(["migrate"])
    assert capsys.readouterr().out == "Migrations applied successfully.\n"
    with (
        closing(psycopg2.connect(cli_database_urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT version, checksum FROM bursar.schema_migrations ORDER BY version")
        assert cursor.fetchall() == expected_manifest

    cli.main(["tenant", "create", "generated-cli-tenant"])
    generated_tenant_id = capsys.readouterr().out.strip()
    cli.main(["tenant", "create", "generated-cli-tenant"])
    assert capsys.readouterr().out.strip() == generated_tenant_id

    cli.main(["tenant", "create", "cli-tenant", "--id", CLI_TENANT_ID, "--display-name", "CLI Tenant"])
    assert capsys.readouterr().out.strip() == CLI_TENANT_ID
    cli.main(["tenant", "status", CLI_TENANT_ID, "suspended"])
    assert capsys.readouterr().out.strip() == CLI_TENANT_ID
    cli.main(["tenant", "status", CLI_TENANT_ID, "active"])
    assert capsys.readouterr().out.strip() == CLI_TENANT_ID

    with (
        closing(psycopg2.connect(cli_database_urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        # Prove the private partition owner has the complete pg_partman
        # SECURITY INVOKER closure instead of succeeding through defaults.
        cursor.execute("REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA partman FROM PUBLIC")
        cursor.execute(
            """
            SELECT count(*) FILTER (
                WHERE has_function_privilege(
                    'public',
                    function_info.oid,
                    'EXECUTE'
                )
            )
            FROM pg_proc AS function_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = function_info.pronamespace
            WHERE namespace_info.nspname = 'partman'
              AND function_info.prokind IN ('f', 'p')
            """
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            """
            INSERT INTO bursar.event_outbox(
                tenant_id,
                topic,
                aggregate_type,
                aggregate_id,
                idempotency_key,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                CLI_TENANT_ID,
                "credential-route-probe",
                "credential-route-probe",
                CLI_TENANT_ID,
                "credential-route-probe",
                '{"delivery_required": true}',
            ),
        )
        seeded_outbox_id = cursor.fetchone()
        assert seeded_outbox_id is not None
        cursor.execute(
            """
            SELECT child_schema.nspname, child.relname
            FROM pg_inherits AS inheritance
            JOIN pg_class AS child
              ON child.oid = inheritance.inhrelid
            JOIN pg_namespace AS child_schema
              ON child_schema.oid = child.relnamespace
            JOIN pg_class AS parent
              ON parent.oid = inheritance.inhparent
            JOIN pg_namespace AS parent_schema
              ON parent_schema.oid = parent.relnamespace
            WHERE parent_schema.nspname = 'bursar'
              AND parent.relname = 'usage_charge_payloads'
              AND pg_get_expr(child.relpartbound, child.oid) <> 'DEFAULT'
            ORDER BY child.relname DESC
            LIMIT 1
            """
        )
        dropped_partition = cursor.fetchone()
        assert dropped_partition is not None
        cursor.execute(
            sql.SQL("DROP TABLE {}.{}").format(
                sql.Identifier(dropped_partition[0]),
                sql.Identifier(dropped_partition[1]),
            )
        )

    # Exercise representative global worker paths through the real operator
    # login.  These touch the dedicated definer's RLS policies instead of
    # succeeding through the migration owner's former superuser authority.
    with (
        closing(psycopg2.connect(cli_database_urls.operator)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE bursar_operator")
        cursor.execute("SELECT (bursar.configure_storage()).singleton")
        assert cursor.fetchone() == (True,)
        cursor.execute(
            """
            SELECT event_id, claim_token
            FROM bursar.claim_outbox_events(
                1::integer,
                60::integer,
                ARRAY['credential-route-probe']::text[]
            )
            """
        )
        claimed_outbox = cursor.fetchone()
        assert claimed_outbox is not None
        assert claimed_outbox[0] == seeded_outbox_id[0]
        cursor.execute(
            "SELECT bursar.fail_outbox_event(%s, %s, %s, 0, 1)",
            (
                claimed_outbox[0],
                claimed_outbox[1],
                "CredentialRoute:Probe",
            ),
        )
        assert cursor.fetchone() == (True,)
        cursor.execute(
            "SELECT bursar.run_storage_partition_maintenance(%s, now())",
            ("usage_charge_payloads",),
        )
        partition_result = cursor.fetchone()
        assert partition_result is not None
        assert partition_result[0]["status"] == "completed"
        assert partition_result[0]["partitions_created"] >= 1
        cursor.execute("SELECT bursar.run_storage_maintenance(now())")
        maintenance_result = cursor.fetchone()
        assert maintenance_result is not None
        assert maintenance_result[0]["status"] == "completed"

    with (
        closing(psycopg2.connect(cli_database_urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            SELECT
                pg_get_userbyid(table_info.relowner),
                table_info.relrowsecurity,
                table_info.relforcerowsecurity,
                array_agg(DISTINCT role_info.rolname ORDER BY role_info.rolname),
                has_table_privilege(
                    'bursar_operator_runtime',
                    table_info.oid,
                    'SELECT, UPDATE, DELETE'
                )
            FROM pg_class AS table_info
            JOIN pg_namespace AS namespace_info
              ON namespace_info.oid = table_info.relnamespace
            JOIN pg_policy AS policy_info
              ON policy_info.polrelid = table_info.oid
            CROSS JOIN LATERAL unnest(policy_info.polroles) AS policy_role(role_oid)
            JOIN pg_roles AS role_info
              ON role_info.oid = policy_role.role_oid
            WHERE namespace_info.nspname = %s
              AND table_info.relname = %s
            GROUP BY table_info.oid
            """,
            dropped_partition,
        )
        assert cursor.fetchone() == (
            PARTITION_OWNER_ROLE,
            True,
            True,
            [OPERATOR_OWNER_ROLE, PARTITION_OWNER_ROLE, "bursar_runtime"],
            True,
        )

    first_config = tmp_path / "pricing-v1.json"
    second_config = tmp_path / "pricing-v2.json"
    yaml_config = tmp_path / "pricing.yaml"
    _write_config(first_config, display_name="Pro")
    _write_config(second_config, display_name="Pro v2")
    yaml_config.write_text(first_config.read_text())

    cli.main(["config", "validate", str(first_config)])
    assert capsys.readouterr().out == "Bursar config is valid.\n"
    cli.main(["config", "validate", str(yaml_config), "--json"])
    assert json.loads(capsys.readouterr().out) == {"valid": True, "errors": []}

    cli.main(
        [
            "tenant",
            "bootstrap",
            "cli-tenant",
            str(first_config),
            "--id",
            CLI_TENANT_ID,
            "--label",
            "initial",
        ]
    )
    assert f"Tenant {CLI_TENANT_ID} bootstrapped successfully" in capsys.readouterr().out

    with PostgresStore(
        cli_database_urls.runtime,
        tenant_id=CLI_TENANT_ID,
        provider_environment="test",
    ) as runtime_store:
        credits = CreditsService(store=runtime_store)
        credits.add_credits(
            CLI_MAINTENANCE_SUBJECT_ID,
            Decimal("10"),
            entry_type="purchase",
            idempotency_key="credential-route-maintenance-funding",
        )
        lease = runtime_store.create_lease(
            CLI_MAINTENANCE_SUBJECT_ID,
            Decimal("1"),
            "completion",
            CreateLeaseOptions(
                idempotency_key="credential-route-maintenance-lease",
                dimensions={"model": "standard"},
                measures={"input_tokens": "1"},
            ),
        )
        assert lease.lease_id is not None
        released = runtime_store.release_lease(
            CLI_MAINTENANCE_SUBJECT_ID,
            lease.lease_id,
        )
        assert released.released is True

    with (
        closing(psycopg2.connect(cli_database_urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL session_replication_role = replica")
        cursor.execute(
            """
            UPDATE bursar.credit_leases
            SET updated_at = now() - interval '400 days'
            WHERE id = %s
            """,
            (lease.lease_id,),
        )
        assert cursor.rowcount == 1

    with (
        closing(psycopg2.connect(cli_database_urls.operator)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SET LOCAL ROLE bursar_operator")
        cursor.execute("SELECT bursar.run_storage_maintenance(now())")
        compacted = cursor.fetchone()
        assert compacted is not None
        assert compacted[0]["terminal_leases_compacted"] == 1

    with (
        closing(psycopg2.connect(cli_database_urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT dimensions, metadata FROM bursar.credit_leases WHERE id = %s",
            (lease.lease_id,),
        )
        assert cursor.fetchone() == ({}, {})

    cli.main(["config", "get"])
    active = json.loads(capsys.readouterr().out)
    assert active["version"] == 1
    assert active["config"]["plans"]["pro"]["display_name"] == "Pro"
    with (
        closing(psycopg2.connect(cli_database_urls.admin)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT DISTINCT provider_environment FROM bursar.catalog_provider_refs WHERE tenant_id = %s",
            (CLI_TENANT_ID,),
        )
        assert cursor.fetchall() == [("test",)]

    cli.main(["config", "set", str(second_config), "--label", "updated"])
    assert capsys.readouterr().out == "Bursar config set successfully.\n"
    cli.main(["config", "set", str(second_config), "--label", "unchanged"])
    assert capsys.readouterr().out == "No changes — config is identical to the active version.\n"

    cli.main(["config", "list"])
    listed = capsys.readouterr().out
    assert "* v2" in listed
    assert "  v1" in listed

    cli.main(["config", "export", "1"])
    exported = json.loads(capsys.readouterr().out)
    assert exported["plans"]["pro"]["display_name"] == "Pro"

    cli.main(["config", "diff", "1", "2"])
    diff = capsys.readouterr().out
    assert "--- v1" in diff
    assert "+++ v2" in diff
    assert "Pro v2" in diff

    cli.main(["config", "activate", "1"])
    assert capsys.readouterr().out == "Catalog revision 1 activated.\n"

    with pytest.raises(SystemExit) as missing_export:
        cli.main(["config", "export", "99"])
    assert missing_export.value.code == 1
    assert "Version 99 not found." in capsys.readouterr().err

    with pytest.raises(SystemExit) as bad_tenant:
        cli.main(["tenant", "status", "not-a-uuid", "active"])
    assert bad_tenant.value.code == 1
    assert "Tenant ID must be a UUID" in capsys.readouterr().err

    bad_config = tmp_path / "bad.json"
    bad_config.write_text('{"version": 1, "credits": {"accounting": {"rounding": "invalid"}}}')
    with pytest.raises(SystemExit) as invalid_config:
        cli.main(["config", "validate", str(bad_config), "--json"])
    assert invalid_config.value.code == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
