"""CLI parsing and failure-mode tests that never require a database."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from bursar import __main__ as cli
from bursar.credits.store import StoreError, StoreUnavailableError


def test_load_config_file_supports_json_yaml_and_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    json_path = tmp_path / "bursar.json"
    json_path.write_text(
        json.dumps(
            {
                "version": 1,
                "credits": {
                    "accounting": {
                        "unit": "credit",
                        "scale": 6,
                        "rounding": "half_up",
                    }
                },
            }
        )
    )
    assert cli._load_config_file(str(json_path))["version"] == 1

    yaml_path = tmp_path / "bursar.yaml"
    yaml_path.write_text("version: 1\ncredits:\n  accounting:\n    unit: credit\n    scale: 6\n    rounding: half_up\n")
    assert cli._load_config_file(str(yaml_path))["version"] == 1

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("version: 1\n"))
    assert cli._load_config_file("-")["version"] == 1


@pytest.mark.parametrize(
    "raw, message", [("{", "Invalid JSON"), ("[]", "must be a JSON/YAML object"), ("{}", "is empty")]
)
def test_load_config_file_reports_clean_errors(
    tmp_path: Path, raw: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(raw)
    with pytest.raises(SystemExit) as exc:
        cli._load_config_file(str(path))
    assert exc.value.code == 1
    assert message in capsys.readouterr().err


def test_load_config_file_rejects_unsupported_extensions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "bursar.txt"
    path.write_text("{}")

    with pytest.raises(SystemExit) as exc:
        cli._load_config_file(str(path))

    assert exc.value.code == 1
    assert "Unsupported config file format" in capsys.readouterr().err


def test_config_validate_json_covers_file_and_parse_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.yaml"

    with pytest.raises(SystemExit) as exc:
        cli.main(["config", "validate", str(missing), "--json"])

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["valid"] is False
    assert payload["errors"][0]["type"] == "invalid_config"
    assert "Config file not found" in payload["errors"][0]["msg"]


def test_parser_requires_a_command_and_config_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit):
        cli.main(["config"])


def test_parser_has_no_legacy_store_backend_switch() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--store", "postgres", "config", "schema"])


def test_parser_exposes_tenant_bootstrap_as_one_operator_command() -> None:
    args = cli.build_parser().parse_args(
        [
            "tenant",
            "bootstrap",
            "acme",
            "bursar.yaml",
            "--display-name",
            "Acme",
        ]
    )

    assert args.func is cli._cmd_tenant_bootstrap
    assert args.slug == "acme"
    assert args.file == "bursar.yaml"
    assert args.id is None
    assert args.display_name == "Acme"


def test_parser_exposes_bounded_due_plan_change_worker() -> None:
    args = cli.build_parser().parse_args(["config", "apply-due", "--limit", "250"])

    assert args.func is cli._cmd_config_apply_due
    assert args.limit == 250


def test_migrate_requires_the_explicit_migration_dsn_and_ignores_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migration_url = "postgresql://migration.example.test/bursar"
    (tmp_path / ".env").write_text(f"BURSAR_MIGRATION_DATABASE_URL={migration_url}\n")
    calls: list[str] = []

    def run_migrations(database_url: str) -> None:
        calls.append(database_url)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://runtime.example.test/bursar")
    monkeypatch.delenv("BURSAR_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr("bursar.credits.postgres.store.run_migrations", run_migrations)

    with pytest.raises(SystemExit) as missing_dsn:
        cli.main(["migrate"])
    assert missing_dsn.value.code == 1
    assert calls == []
    assert "BURSAR_MIGRATION_DATABASE_URL is required" in capsys.readouterr().err

    monkeypatch.setenv("BURSAR_MIGRATION_DATABASE_URL", migration_url)
    cli.main(["migrate"])
    assert calls == [migration_url]
    assert capsys.readouterr().out == "Migrations applied successfully.\n"


def test_migrate_has_no_unledgered_host_sql_option() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["migrate", "--post-migrate-sql", "host.sql"])


def test_database_backed_commands_require_an_explicit_provider_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("BURSAR_PROVIDER_ENVIRONMENT", raising=False)
    with pytest.raises(SystemExit) as missing:
        cli._provider_environment_from_env()
    assert missing.value.code == 1
    assert "live, test, sandbox" in capsys.readouterr().err

    monkeypatch.setenv("BURSAR_PROVIDER_ENVIRONMENT", "production")
    with pytest.raises(SystemExit) as invalid:
        cli._provider_environment_from_env()
    assert invalid.value.code == 1
    assert "live, test, sandbox" in capsys.readouterr().err

    monkeypatch.setenv("BURSAR_PROVIDER_ENVIRONMENT", "test")
    assert cli._provider_environment_from_env() == "test"


def test_retry_store_operation_retries_only_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def transient() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise StoreUnavailableError("PostgreSQL is temporarily unavailable")
        return "ok"

    monkeypatch.setattr(cli, "_RETRY_INITIAL_DELAY", 0)
    assert cli._retry_store_operation(transient, what="test") == "ok"
    assert attempts == 3

    def permanent() -> None:
        raise StoreError("permission denied")

    with pytest.raises(SystemExit):
        cli._retry_store_operation(permanent, what="test")

    indeterminate_attempts = 0

    def indeterminate() -> None:
        nonlocal indeterminate_attempts
        indeterminate_attempts += 1
        raise StoreUnavailableError("Connection dropped during commit", indeterminate=True)

    with pytest.raises(SystemExit):
        cli._retry_store_operation(indeterminate, what="test")
    assert indeterminate_attempts == 1


def test_tenant_bootstrap_owns_provisioning_and_config_sequence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant_id = UUID("00000000-0000-4000-8000-000000000001")
    config = {"version": 1}
    calls: list[tuple[str, object]] = []

    monkeypatch.setenv("BURSAR_TENANT_ID", str(tenant_id))
    monkeypatch.setenv("DATABASE_URL", "postgresql://runtime.example.test/bursar")
    monkeypatch.setenv("BURSAR_PROVIDER_ENVIRONMENT", "test")
    monkeypatch.setattr(cli, "_validated_config", lambda filepath: config)

    def provision_tenant(
        *,
        tenant_id: UUID | None,
        slug: str,
        display_name: str | None,
    ) -> UUID:
        assert tenant_id is not None
        calls.append(
            (
                "tenant",
                (tenant_id, slug, display_name),
            )
        )
        return tenant_id

    def set_config(
        data: dict[str, object],
        *,
        tenant_id: str | None,
        label: str | None,
    ) -> bool:
        calls.append(
            (
                "config",
                (data, tenant_id, label),
            )
        )
        return True

    monkeypatch.setattr(cli, "_provision_tenant", provision_tenant)
    monkeypatch.setattr(cli, "_set_config", set_config)

    cli._cmd_tenant_bootstrap(
        Namespace(
            file="pricing.yaml",
            id=None,
            slug="acme",
            display_name="Acme",
            label="initial",
        )
    )

    assert calls == [
        ("tenant", (tenant_id, "acme", "Acme")),
        ("config", (config, str(tenant_id), "initial")),
    ]
    assert capsys.readouterr().out == f"Tenant {tenant_id} bootstrapped successfully (config applied).\n"


def test_tenant_bootstrap_requires_a_stable_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("BURSAR_TENANT_ID", raising=False)
    monkeypatch.setattr(cli, "_validated_config", lambda filepath: {"version": 1})
    monkeypatch.setattr(
        cli,
        "_provision_tenant",
        lambda **kwargs: pytest.fail("tenant must not be provisioned"),
    )

    with pytest.raises(SystemExit) as exc:
        cli._cmd_tenant_bootstrap(
            Namespace(
                file="pricing.yaml",
                id=None,
                slug="acme",
                display_name=None,
                label=None,
                store="postgres",
            )
        )

    assert exc.value.code == 1
    assert "BURSAR_TENANT_ID or --id is required" in capsys.readouterr().err


def test_tenant_bootstrap_resolves_runtime_target_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant_id = "00000000-0000-4000-8000-000000000001"
    monkeypatch.setenv("DATABASE_URL", "postgresql://runtime.example.test/bursar")
    monkeypatch.delenv("BURSAR_PROVIDER_ENVIRONMENT", raising=False)
    monkeypatch.setattr(cli, "_validated_config", lambda filepath: {"version": 1})
    monkeypatch.setattr(
        cli,
        "_provision_tenant",
        lambda **kwargs: pytest.fail("tenant must not be provisioned"),
    )

    with pytest.raises(SystemExit) as exc:
        cli._cmd_tenant_bootstrap(
            Namespace(
                file="pricing.yaml",
                id=tenant_id,
                slug="acme",
                display_name=None,
                label=None,
            )
        )

    assert exc.value.code == 1
    assert "BURSAR_PROVIDER_ENVIRONMENT" in capsys.readouterr().err


def test_validate_json_reports_normalization_conflict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "conflict.yaml"
    path.write_text("""\
version: 1
credits:
  on_signup: '20'
  grant_programs:
    signup:
      trigger: account_created
      awards: [{amount: '10', bucket: default}]
""")
    with pytest.raises(SystemExit):
        cli.main(["config", "validate", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert "conflicts" in payload["errors"][0]["msg"]


def test_validate_json_rejects_non_string_environment_keys_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("""\
version: 1
commerce:
  offers:
    buy:
      type: topup
      display_name: Buy
      price: {amount_minor: 100, currency: USD}
      credits_per_unit: '10'
      bucket: default
      providers:
        stripe:
          type: stripe_price
          price_id: {test: price_test, 1: price_invalid}
""")
    with pytest.raises(SystemExit):
        cli.main(["config", "validate", str(path), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["valid"] is False
    assert payload["errors"]
    assert captured.err == ""


def test_validate_json_accepts_partial_provider_environment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "missing-binding.yaml"
    path.write_text("""\
version: 1
commerce:
  providers:
    stripe: {type: stripe}
    dodo: {type: dodo}
  offers:
    buy:
      type: topup
      display_name: Buy
      price: {amount_minor: 100, currency: USD}
      credits_per_unit: '10'
      bucket: default
      providers:
        stripe:
          type: stripe_price
          price_id: {test: price_test}
        dodo:
          type: dodo_product
          product_id: {live: product_live}
""")
    cli.main(["config", "validate", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"valid": True, "errors": []}


def test_usage_cutover_cli_runs_with_bounded_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    class ClickHouse:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    clickhouse = ClickHouse()
    calls: list[tuple[object, ...]] = []

    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr(cli, "_database_url_from_env", lambda _name: "postgresql://operator")
    monkeypatch.setattr(cli, "_provider_environment_from_env", lambda: "test")
    monkeypatch.setattr("psycopg2.connect", lambda _url: connection)
    monkeypatch.setitem(sys.modules, "clickhouse_connect", SimpleNamespace(get_client=lambda **_kwargs: clickhouse))

    def run_cutover(db, client, tenant, target, cutoff, table, environment):
        calls.append((db, client, tenant, target, cutoff, table, environment))
        return 7

    monkeypatch.setattr("bursar.storage.cutover.run_cutover", run_cutover)
    cli.main(
        [
            "usage-cutover",
            "clickhouse",
            "--tenant-id",
            "00000000-0000-4000-8000-000000000101",
            "--cutoff",
            "2026-01-01T00:00:00+00:00",
        ]
    )

    assert calls[0][:4] == (
        connection,
        clickhouse,
        "00000000-0000-4000-8000-000000000101",
        "clickhouse",
    )
    assert calls[0][5:] == ("bursar_usage_events", "test")
    assert connection.closed and clickhouse.closed
    assert capsys.readouterr().out == "Usage cutover complete: 7 retained records verified.\n"


def test_usage_cutover_cli_sanitizes_driver_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    class ClickHouse:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    clickhouse = ClickHouse()
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr(cli, "_database_url_from_env", lambda _name: "postgresql://secret")
    monkeypatch.setattr(cli, "_provider_environment_from_env", lambda: "test")
    monkeypatch.setattr("psycopg2.connect", lambda _url: connection)
    monkeypatch.setitem(sys.modules, "clickhouse_connect", SimpleNamespace(get_client=lambda **_kwargs: clickhouse))

    def fail_cutover(*_args: object) -> int:
        raise RuntimeError("password=secret")

    monkeypatch.setattr("bursar.storage.cutover.run_cutover", fail_cutover)
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "usage-cutover",
                "clickhouse",
                "--tenant-id",
                "00000000-0000-4000-8000-000000000101",
                "--cutoff",
                "2026-01-01T00:00:00+00:00",
            ]
        )

    assert exc.value.code == 1
    assert connection.closed and clickhouse.closed
    error = capsys.readouterr().err
    assert "RuntimeError" in error
    assert "secret" not in error


def test_usage_cutover_cli_requires_tenant_and_timezone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.delenv("BURSAR_TENANT_ID", raising=False)
    with pytest.raises(SystemExit) as missing_tenant:
        cli.main(["usage-cutover", "clickhouse", "--cutoff", "2026-01-01T00:00:00+00:00"])
    assert str(missing_tenant.value) == "BURSAR_TENANT_ID or --tenant-id is required"
    assert capsys.readouterr().err == ""

    with pytest.raises(SystemExit) as missing_timezone:
        cli.main(
            [
                "usage-cutover",
                "clickhouse",
                "--tenant-id",
                "00000000-0000-4000-8000-000000000101",
                "--cutoff",
                "2026-01-01T00:00:00",
            ]
        )
    assert missing_timezone.value.code == 1
    assert "ValueError" in capsys.readouterr().err


def test_usage_cutover_cli_allows_only_safe_diagnostic_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Connection:
        def close(self) -> None:
            return None

    class ClickHouse:
        def close(self) -> None:
            return None

    connection = Connection()
    clickhouse = ClickHouse()
    errors: list[RuntimeError] = []
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr(cli, "_database_url_from_env", lambda _name: "postgresql://secret")
    monkeypatch.setattr(cli, "_provider_environment_from_env", lambda: "test")
    monkeypatch.setattr("psycopg2.connect", lambda _url: connection)
    monkeypatch.setitem(sys.modules, "clickhouse_connect", SimpleNamespace(get_client=lambda **_kwargs: clickhouse))

    def fail_cutover(*_args: object) -> int:
        raise errors[0]

    monkeypatch.setattr("bursar.storage.cutover.run_cutover", fail_cutover)
    diagnostics = SimpleNamespace(message_primary="usage backend is already selected")
    postgres_error = RuntimeError("password=secret")
    postgres_error.diag = diagnostics
    cases = [
        (
            RuntimeError("another cutover driver is running for this tenant"),
            "another cutover driver is running for this tenant",
        ),
        (RuntimeError("usage verification failed for charge secret-id"), "source and destination usage records differ"),
        (RuntimeError("ClickHouse table secret_table"), "ClickHouse projection table is missing or incompatible"),
        (postgres_error, "usage backend is already selected"),
    ]
    for error, detail in cases:
        errors[:] = [error]
        with pytest.raises(SystemExit) as exc:
            cli._cmd_usage_cutover(
                Namespace(
                    target="clickhouse",
                    tenant_id="00000000-0000-4000-8000-000000000101",
                    cutoff="2026-01-01T00:00:00+00:00",
                    table="bursar_usage_events",
                )
            )
        assert exc.value.code == 1
        stderr = capsys.readouterr().err
        assert detail in stderr
        assert "secret" not in stderr


def test_usage_cutover_abort_cli_uses_operator_role_and_releases_connection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Cursor:
        def __init__(self) -> None:
            self.commands: list[tuple[str, object]] = []

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, query: str, params: object = None) -> None:
            self.commands.append((query, params))

        def fetchone(self) -> tuple[bool]:
            return (True,)

    class Connection:
        def __init__(self) -> None:
            self.autocommit = False
            self.closed = False
            self.db_cursor = Cursor()

        def cursor(self) -> Cursor:
            return self.db_cursor

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr(cli, "_database_url_from_env", lambda _name: "postgresql://operator")
    monkeypatch.setattr("psycopg2.connect", lambda _url: connection)
    cli.main(["usage-cutover-abort", "--tenant-id", "00000000-0000-4000-8000-000000000101"])

    assert connection.autocommit and connection.closed
    assert [command for command, _params in connection.db_cursor.commands] == [
        "SET ROLE bursar_operator",
        "SELECT pg_try_advisory_lock(hashtextextended('bursar.usage.cutover.driver:' || %s, 0))",
        "SELECT bursar.abort_usage_backend_cutover(%s::uuid)",
    ]
    assert capsys.readouterr().out == "Usage cutover aborted; original routing remains selected.\n"


def test_usage_cutover_abort_cli_rejects_missing_tenant_and_busy_lock(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.delenv("BURSAR_TENANT_ID", raising=False)
    with pytest.raises(SystemExit) as missing_tenant:
        cli.main(["usage-cutover-abort"])
    assert str(missing_tenant.value) == "BURSAR_TENANT_ID or --tenant-id is required"

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _query: str, _params: object = None) -> None:
            return None

        def fetchone(self) -> tuple[bool]:
            return (False,)

    class Connection:
        autocommit = False

        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            return None

    monkeypatch.setattr(cli, "_database_url_from_env", lambda _name: "postgresql://operator")
    monkeypatch.setattr("psycopg2.connect", lambda _url: Connection())
    with pytest.raises(SystemExit) as busy_lock:
        cli.main(["usage-cutover-abort", "--tenant-id", "00000000-0000-4000-8000-000000000101"])
    assert busy_lock.value.code == 1
    assert capsys.readouterr().err == "Usage cutover abort failed (RuntimeError).\n"
