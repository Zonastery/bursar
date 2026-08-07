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

    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"version": 1}'))
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


def test_parser_requires_a_command_and_config_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
    with pytest.raises(SystemExit):
        cli.main(["config"])


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


def test_migrate_accepts_ordered_post_migration_sql_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = tmp_path / "first.sql"
    second = tmp_path / "second.sql"
    first.write_text("SELECT 1;\n")
    second.write_text("SELECT 2;\n")
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    def run_migrations(
        database_url: str,
        *,
        post_migration_sql: list[tuple[str, str]],
    ) -> None:
        calls.append((database_url, post_migration_sql))

    monkeypatch.setenv("DATABASE_URL", "postgresql://example.test/bursar")
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr("bursar.credits.postgres.store.run_migrations", run_migrations)

    args = Namespace(post_migrate_sql=[str(first), str(second)])
    cli._cmd_migrate(args)

    assert calls == [
        (
            "postgresql://example.test/bursar",
            [(str(first), "SELECT 1;\n"), (str(second), "SELECT 2;\n")],
        )
    ]
    assert capsys.readouterr().out == "Migrations applied successfully.\n"


@pytest.mark.parametrize("contents", [None, " \n"])
def test_migrate_rejects_unreadable_or_empty_post_migration_sql(
    tmp_path: Path,
    contents: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "host.sql"
    if contents is not None:
        path.write_text(contents)
    called = False

    def run_migrations(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setenv("DATABASE_URL", "postgresql://example.test/bursar")
    monkeypatch.setattr(cli, "_require_extra", lambda _extra: None)
    monkeypatch.setattr("bursar.credits.postgres.store.run_migrations", run_migrations)

    with pytest.raises(SystemExit) as exc:
        cli._cmd_migrate(Namespace(post_migrate_sql=[str(path)]))

    assert exc.value.code == 1
    assert not called
    assert str(path) in capsys.readouterr().err


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
    monkeypatch.setattr(cli, "_validated_config", lambda filepath: config)

    def provision_tenant(
        *,
        tenant_id: UUID,
        slug: str,
        display_name: str | None,
    ) -> UUID:
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
        store_type: str,
        tenant_id: str | None,
        label: str | None,
    ) -> bool:
        calls.append(
            (
                "config",
                (data, store_type, tenant_id, label),
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
            store="postgres",
        )
    )

    assert calls == [
        ("tenant", (tenant_id, "acme", "Acme")),
        ("config", (config, "postgres", str(tenant_id), "initial")),
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
