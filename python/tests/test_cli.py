"""CLI parsing and failure-mode tests that never require a database."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
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
