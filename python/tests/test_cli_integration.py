"""DB-backed CLI coverage for operator-facing Bursar workflows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from bursar import __main__ as cli
from bursar.sql import _get_sql_files
from tests.conftest import _handle_unavailable_postgres, _start_testcontainer
from tests.test_store_integration import CONFIG

pytestmark = [pytest.mark.integration, pytest.mark.security]

CLI_TENANT_ID = "00000000-0000-4000-8000-000000000101"


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
                # Migration 029 creates and grants the cluster-global Bursar
                # roles. CREATEROLE is therefore required; SUPERUSER, CREATEDB,
                # INHERIT, and BYPASSRLS are not.
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD %s NOSUPERUSER "
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
        urls = CliDatabaseUrls(
            admin=admin_database_url,
            migration=make_dsn(
                admin_database_url,
                user=roles["migration"],
                password=passwords["migration"],
            ),
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
    """Create the operator and runtime logins as the migration owner."""
    with (
        closing(psycopg2.connect(urls.migration)) as connection,
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
                    cli_database_urls.migration_role,
                    "bursar_client",
                    False,
                    True,
                ),
                (
                    cli_database_urls.migration_role,
                    "bursar_operator",
                    False,
                    True,
                ),
                (
                    cli_database_urls.migration_role,
                    "bursar_runtime",
                    False,
                    True,
                ),
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
            (cli_database_urls.migration_role, (True, True, True, True)),
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
