"""DB-backed CLI coverage for operator-facing Bursar workflows."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from bursar import __main__ as cli
from tests.test_store_integration import CONFIG

pytestmark = [pytest.mark.integration, pytest.mark.security]

CLI_TENANT_ID = "00000000-0000-4000-8000-000000000101"


@dataclass(frozen=True)
class CliDatabaseUrls:
    migration: str
    operator: str
    runtime: str
    migration_role: str
    operator_role: str
    runtime_role: str


@pytest.fixture
def cli_database_urls(pg_database_url: str) -> Iterator[CliDatabaseUrls]:
    """Provision three non-superuser logins with non-overlapping authority."""
    suffix = uuid4().hex[:12]
    roles = {
        "migration": f"bursar_cli_migrate_{suffix}",
        "operator": f"bursar_cli_operator_{suffix}",
        "runtime": f"bursar_cli_runtime_{suffix}",
    }
    passwords = {name: uuid4().hex for name in roles}
    created_roles: list[str] = []

    with (
        closing(psycopg2.connect(pg_database_url)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            SELECT
                current_database(),
                pg_get_userbyid(namespace_info.nspowner),
                pg_get_userbyid(table_info.relowner)
            FROM pg_namespace AS namespace_info
            JOIN pg_class AS table_info
              ON table_info.relnamespace = namespace_info.oid
            WHERE namespace_info.nspname = 'bursar'
              AND table_info.relname = 'schema_migrations'
            """
        )
        database_row = cursor.fetchone()
        assert database_row is not None
        database_name, schema_owner, migration_table_owner = database_row

        for purpose, role in roles.items():
            create_role = sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD %s NOSUPERUSER NOCREATEDB {} NOINHERIT NOREPLICATION NOBYPASSRLS"
            ).format(
                sql.Identifier(role),
                sql.SQL("CREATEROLE" if purpose == "migration" else "NOCREATEROLE"),
            )
            cursor.execute(create_role, (passwords[purpose],))
            created_roles.append(role)

        cursor.execute(
            sql.SQL("GRANT bursar_operator TO {} WITH INHERIT FALSE, SET TRUE").format(
                sql.Identifier(roles["operator"])
            )
        )
        cursor.execute(
            sql.SQL("GRANT bursar_client TO {} WITH INHERIT FALSE, SET TRUE").format(sql.Identifier(roles["runtime"]))
        )
        cursor.execute(
            sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
                sql.Identifier(database_name),
                sql.Identifier(roles["migration"]),
            )
        )
        cursor.execute(sql.SQL("ALTER SCHEMA bursar OWNER TO {}").format(sql.Identifier(roles["migration"])))
        cursor.execute(
            sql.SQL("ALTER TABLE bursar.schema_migrations OWNER TO {}").format(sql.Identifier(roles["migration"]))
        )
        cursor.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA bursar TO {}").format(sql.Identifier(roles["migration"])))
        cursor.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON bursar.schema_migrations TO {}").format(
                sql.Identifier(roles["migration"])
            )
        )

    urls = CliDatabaseUrls(
        migration=make_dsn(
            pg_database_url,
            user=roles["migration"],
            password=passwords["migration"],
        ),
        operator=make_dsn(
            pg_database_url,
            user=roles["operator"],
            password=passwords["operator"],
        ),
        runtime=make_dsn(
            pg_database_url,
            user=roles["runtime"],
            password=passwords["runtime"],
        ),
        migration_role=roles["migration"],
        operator_role=roles["operator"],
        runtime_role=roles["runtime"],
    )

    try:
        yield urls
    finally:
        leaked_sessions: list[tuple[str, int]] = []
        cleanup = psycopg2.connect(pg_database_url)
        try:
            cleanup.autocommit = True
            with cleanup.cursor() as cursor:
                cursor.execute(
                    "SELECT usename, count(*) "
                    "FROM pg_stat_activity "
                    "WHERE usename = ANY(%s) AND pid <> pg_backend_pid() "
                    "GROUP BY usename ORDER BY usename",
                    (created_roles,),
                )
                leaked_sessions = cursor.fetchall()
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE usename = ANY(%s) AND pid <> pg_backend_pid()",
                    (created_roles,),
                )
                cursor.execute(
                    sql.SQL("ALTER TABLE bursar.schema_migrations OWNER TO {}").format(
                        sql.Identifier(migration_table_owner)
                    )
                )
                cursor.execute(sql.SQL("ALTER SCHEMA bursar OWNER TO {}").format(sql.Identifier(schema_owner)))
                for purpose, role in roles.items():
                    if role not in created_roles:
                        continue
                    if purpose == "operator":
                        cursor.execute(sql.SQL("REVOKE bursar_operator FROM {}").format(sql.Identifier(role)))
                    elif purpose == "runtime":
                        cursor.execute(sql.SQL("REVOKE bursar_client FROM {}").format(sql.Identifier(role)))
                    cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                    cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        finally:
            cleanup.close()
        assert leaked_sessions == []


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


def test_cli_manages_tenants_migrations_and_config_versions(
    pg_database_url: str,
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
        closing(psycopg2.connect(pg_database_url)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
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
        }

        cursor.execute(
            """
            SELECT
                member_role.rolname,
                granted_role.rolname,
                membership.inherit_option,
                membership.set_option
            FROM pg_auth_members AS membership
            JOIN pg_roles AS granted_role
              ON granted_role.oid = membership.roleid
            JOIN pg_roles AS member_role
              ON member_role.oid = membership.member
            WHERE member_role.rolname = ANY(%s)
            ORDER BY member_role.rolname, granted_role.rolname
            """,
            (
                [
                    cli_database_urls.migration_role,
                    cli_database_urls.operator_role,
                    cli_database_urls.runtime_role,
                ],
            ),
        )
        assert cursor.fetchall() == sorted(
            [
                (
                    cli_database_urls.operator_role,
                    "bursar_operator",
                    False,
                    True,
                ),
                (
                    cli_database_urls.runtime_role,
                    "bursar_client",
                    False,
                    True,
                ),
            ]
        )

        for role, expected in (
            (cli_database_urls.migration_role, (True, True, True, False)),
            (cli_database_urls.operator_role, (False, False, False, False)),
            (cli_database_urls.runtime_role, (False, False, False, False)),
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

    for database_url, expected_role, forbidden_role in (
        (
            cli_database_urls.migration,
            cli_database_urls.migration_role,
            "bursar_client",
        ),
        (
            cli_database_urls.operator,
            cli_database_urls.operator_role,
            "bursar_client",
        ),
        (
            cli_database_urls.runtime,
            cli_database_urls.runtime_role,
            "bursar_operator",
        ),
    ):
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
    assert "Migrations applied successfully." in capsys.readouterr().out
    with (
        closing(psycopg2.connect(pg_database_url)) as connection,
        connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT count(*) FROM bursar.schema_migrations")
        row = cursor.fetchone()
        assert row is not None and row[0] > 0

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

    cli.main(["config", "get"])
    active = json.loads(capsys.readouterr().out)
    assert active["version"] == 1
    assert active["config"]["plans"]["pro"]["display_name"] == "Pro"
    with (
        closing(psycopg2.connect(pg_database_url)) as connection,
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
